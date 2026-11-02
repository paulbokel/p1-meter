import { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";
import { useT } from "../i18n.js";

export default function Settings({ user, setUser }) {
  const t = useT();
  return (
    <div className="stack">
      <Account user={user} setUser={setUser} t={t} />
      {user.role === "admin" && <Users me={user} t={t} />}
    </div>
  );
}

function Account({ user, setUser, t }) {
  const [pw, setPw] = useState("");
  const [msg, setMsg] = useState("");
  const save = async (body) => {
    setMsg("");
    try { setUser(await api("/api/auth/me", { method: "PATCH", body })); setMsg(t("saved")); setPw(""); }
    catch (e) { setMsg(e.message); }
  };
  return (
    <section className="card"><h2>{t("account")} · {user.username}</h2>
      <div className="row">
        <label className="muted">{t("language")}</label>
        <select className="in" value={user.language} onChange={(e) => save({ language: e.target.value })}>
          <option value="nl">Nederlands</option><option value="en">English</option>
        </select>
      </div>
      <form className="row" style={{ marginTop: 10 }} onSubmit={(e) => { e.preventDefault(); save({ password: pw }); }}>
        <input className="in" type="password" placeholder={t("newPassword")} minLength={8} value={pw}
               autoComplete="new-password" onChange={(e) => setPw(e.target.value)} required />
        <button className="btn">{t("changePassword")}</button>
        {msg && <span className="small muted">{msg}</span>}
      </form>
    </section>
  );
}

function Users({ me, t }) {
  const [users, setUsers] = useState([]);
  const [form, setForm] = useState({ username: "", password: "", role: "viewer" });
  const [err, setErr] = useState("");
  const load = useCallback(() => api("/api/users").then(setUsers).catch((e) => setErr(e.message)), []);
  useEffect(() => { load(); }, [load]);

  const run = async (fn) => { setErr(""); try { await fn(); await load(); } catch (e) { setErr(e.message); } };
  const add = (e) => { e.preventDefault(); run(async () => { await api("/api/users", { method: "POST", body: form }); setForm({ username: "", password: "", role: "viewer" }); }); };

  return (
    <section className="card"><h2>{t("users")}</h2>
      <table className="t">
        <thead><tr><th>{t("username")}</th><th>{t("role")}</th><th /></tr></thead>
        <tbody>
          {users.map((u) => (
            <tr key={u.id}>
              <td>{u.username}</td>
              <td>
                <select className="in" value={u.role} disabled={u.id === me.id}
                        onChange={(e) => run(() => api(`/api/users/${u.id}`, { method: "PATCH", body: { role: e.target.value } }))}>
                  <option value="admin">{t("admin")}</option><option value="viewer">{t("viewer")}</option>
                </select>
              </td>
              <td style={{ textAlign: "right" }}>
                {u.id !== me.id && <button className="btn" onClick={() => confirm(`${t("delete")} ${u.username}?`) &&
                  run(() => api(`/api/users/${u.id}`, { method: "DELETE" }))}>{t("delete")}</button>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <form className="row" style={{ marginTop: 12 }} onSubmit={add}>
        <input className="in" placeholder={t("username")} value={form.username} required
               onChange={(e) => setForm({ ...form, username: e.target.value })} />
        <input className="in" type="password" placeholder={t("password")} minLength={8} value={form.password} required
               autoComplete="new-password" onChange={(e) => setForm({ ...form, password: e.target.value })} />
        <select className="in" value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value })}>
          <option value="viewer">{t("viewer")}</option><option value="admin">{t("admin")}</option>
        </select>
        <button className="btn primary">{t("addUser")}</button>
      </form>
      {err && <p className="err small">{err}</p>}
    </section>
  );
}
