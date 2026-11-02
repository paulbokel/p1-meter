"""Local accounts with roles admin/viewer (F-USER-01) and cookie sessions."""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal

import asyncpg
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

COOKIE = "p1_session"
SESSION_DAYS = 90
ph = PasswordHasher()
router = APIRouter(prefix="/api", tags=["auth"])

Role = Literal["admin", "viewer"]
Lang = Literal["nl", "en"]
USERNAME = r"^[A-Za-z0-9_.@-]{1,64}$"


class User(BaseModel):
    id: int
    username: str
    role: Role
    language: Lang = "nl"


class Credentials(BaseModel):
    username: str = Field(pattern=USERNAME)
    password: str = Field(min_length=1, max_length=256)


class NewUser(BaseModel):
    username: str = Field(pattern=USERNAME)
    password: str = Field(min_length=8, max_length=256)
    role: Role = "viewer"
    language: Lang = "nl"


class UserPatch(BaseModel):
    password: str | None = Field(default=None, min_length=8, max_length=256)
    role: Role | None = None
    language: Lang | None = None


def _hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


async def _start_session(request: Request, response: Response, user_id: int) -> None:
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
    await request.app.state.pool.execute(
        "INSERT INTO user_session (token_hash, user_id, expires_at, user_agent) VALUES ($1, $2, $3, $4)",
        _hash_token(token), user_id, expires, (request.headers.get("user-agent") or "")[:200])
    response.set_cookie(COOKIE, token, max_age=SESSION_DAYS * 86400, httponly=True, samesite="strict",
                        secure=request.url.scheme == "https", path="/")


async def current_user(request: Request) -> User:
    token = request.cookies.get(COOKIE)
    if token:
        row = await request.app.state.pool.fetchrow(
            "SELECT u.id, u.username, u.role, u.language FROM user_session s JOIN app_user u ON u.id = s.user_id "
            "WHERE s.token_hash = $1 AND s.expires_at > now()", _hash_token(token))
        if row:
            return User(**dict(row))
    raise HTTPException(401, "login required")


async def require_admin(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(403, "admin role required")
    return user


@router.get("/auth/state", summary="Whether first-run setup is needed")
async def auth_state(request: Request):
    n = await request.app.state.pool.fetchval("SELECT count(*) FROM app_user")
    return {"setup_required": n == 0}


@router.post("/auth/setup", response_model=User, summary="Create the first admin (only when no users exist)")
async def setup(body: Credentials, request: Request, response: Response):
    if len(body.password) < 8:
        raise HTTPException(422, "password must be at least 8 characters")
    pool = request.app.state.pool
    async with pool.acquire() as con, con.transaction():
        await con.execute("LOCK TABLE app_user IN EXCLUSIVE MODE")
        if await con.fetchval("SELECT count(*) FROM app_user"):
            raise HTTPException(409, "setup already done")
        row = await con.fetchrow(
            "INSERT INTO app_user (username, password_hash, role) VALUES ($1, $2, 'admin') "
            "RETURNING id, username, role, language", body.username, ph.hash(body.password))
    await _start_session(request, response, row["id"])
    return User(**dict(row))


@router.post("/auth/login", response_model=User)
async def login(body: Credentials, request: Request, response: Response):
    row = await request.app.state.pool.fetchrow(
        "SELECT id, username, role, language, password_hash FROM app_user WHERE username = $1", body.username)
    try:
        if not row:
            ph.hash(body.password)  # equalise timing
            raise VerifyMismatchError
        ph.verify(row["password_hash"], body.password)
    except (VerifyMismatchError, InvalidHashError):
        raise HTTPException(401, "invalid username or password")
    if ph.check_needs_rehash(row["password_hash"]):
        await request.app.state.pool.execute("UPDATE app_user SET password_hash = $1 WHERE id = $2",
                                             ph.hash(body.password), row["id"])
    await _start_session(request, response, row["id"])
    return User(**{k: row[k] for k in ("id", "username", "role", "language")})


@router.post("/auth/logout", status_code=204)
async def logout(request: Request, response: Response):
    token = request.cookies.get(COOKIE)
    if token:
        await request.app.state.pool.execute("DELETE FROM user_session WHERE token_hash = $1", _hash_token(token))
    response.delete_cookie(COOKIE, path="/")


@router.get("/auth/me", response_model=User)
async def me(user: User = Depends(current_user)):
    return user


@router.patch("/auth/me", response_model=User, summary="Change own password or language")
async def patch_me(body: UserPatch, request: Request, user: User = Depends(current_user)):
    if body.role is not None and body.role != user.role:
        raise HTTPException(403, "cannot change own role")
    return await _patch(request, user.id, UserPatch(password=body.password, language=body.language))


@router.get("/users", response_model=list[User], tags=["users"])
async def list_users(request: Request, _: User = Depends(require_admin)):
    rows = await request.app.state.pool.fetch("SELECT id, username, role, language FROM app_user ORDER BY id")
    return [User(**dict(r)) for r in rows]


@router.post("/users", response_model=User, status_code=201, tags=["users"])
async def create_user(body: NewUser, request: Request, _: User = Depends(require_admin)):
    try:
        row = await request.app.state.pool.fetchrow(
            "INSERT INTO app_user (username, password_hash, role, language) VALUES ($1, $2, $3, $4) "
            "RETURNING id, username, role, language", body.username, ph.hash(body.password), body.role, body.language)
    except asyncpg.UniqueViolationError:
        raise HTTPException(409, "username already exists")
    return User(**dict(row))


async def _patch(request: Request, user_id: int, body: UserPatch) -> User:
    pool = request.app.state.pool
    async with pool.acquire() as con, con.transaction():
        # Serialise admin-count checks so two concurrent demotions/deletes cannot remove the last admin.
        await con.execute("LOCK TABLE app_user IN SHARE ROW EXCLUSIVE MODE")
        if body.role == "viewer":
            admins = await con.fetchval("SELECT count(*) FROM app_user WHERE role = 'admin' AND id <> $1", user_id)
            if admins == 0:
                raise HTTPException(409, "at least one admin must remain")
        row = await con.fetchrow(
            "UPDATE app_user SET password_hash = coalesce($2, password_hash), role = coalesce($3, role), "
            "language = coalesce($4, language) WHERE id = $1 RETURNING id, username, role, language",
            user_id, ph.hash(body.password) if body.password else None, body.role, body.language)
        if row is None:
            raise HTTPException(404, "user not found")
        if body.password:  # log out that user's other sessions, keep the one making this request
            token = request.cookies.get(COOKIE)
            await con.execute("DELETE FROM user_session WHERE user_id = $1 AND token_hash IS DISTINCT FROM $2",
                              user_id, _hash_token(token) if token else None)
    return User(**dict(row))


@router.patch("/users/{user_id}", response_model=User, tags=["users"])
async def patch_user(user_id: int, body: UserPatch, request: Request, _: User = Depends(require_admin)):
    return await _patch(request, user_id, body)


@router.delete("/users/{user_id}", status_code=204, tags=["users"])
async def delete_user(user_id: int, request: Request, admin: User = Depends(require_admin)):
    if user_id == admin.id:
        raise HTTPException(409, "cannot delete yourself")
    pool = request.app.state.pool
    async with pool.acquire() as con, con.transaction():
        await con.execute("LOCK TABLE app_user IN SHARE ROW EXCLUSIVE MODE")
        role = await con.fetchval("SELECT role FROM app_user WHERE id = $1", user_id)
        if role is None:
            raise HTTPException(404, "user not found")
        if role == "admin" and await con.fetchval("SELECT count(*) FROM app_user WHERE role = 'admin'") <= 1:
            raise HTTPException(409, "at least one admin must remain")
        await con.execute("DELETE FROM app_user WHERE id = $1", user_id)
