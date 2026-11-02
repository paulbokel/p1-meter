-- Phase 2: local user accounts and login sessions (F-USER-01).
CREATE TABLE app_user (
    id            bigserial PRIMARY KEY,
    username      text        NOT NULL UNIQUE CHECK (username ~ '^[A-Za-z0-9_.@-]{1,64}$'),
    password_hash text        NOT NULL,
    role          text        NOT NULL CHECK (role IN ('admin', 'viewer')),
    language      text        NOT NULL DEFAULT 'nl' CHECK (language IN ('nl', 'en')),
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- Only a SHA-256 of the session token is stored.
CREATE TABLE user_session (
    token_hash  bytea       PRIMARY KEY,
    user_id     bigint      NOT NULL REFERENCES app_user (id) ON DELETE CASCADE,
    created_at  timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL,
    user_agent  text
);
CREATE INDEX user_session_user_idx ON user_session (user_id);
