import { useEffect, useState } from "react";
import { api } from "../api.js";
import { useT } from "../i18n.js";

export default function Login({ onLogin }) {
  const t = useT();
  const [setup, setSetup] = useState(false);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => { api("/api/auth/state").then((s) => setSetup(s.setup_required)).catch(() => {}); }, []);

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true); setError("");
    try {
      onLogin(await api(setup ? "/api/auth/setup" : "/api/auth/login", { method: "POST", body: { username, password } }));
    } catch (err) { setError(err.message); } finally { setBusy(false); }
  };

  return (
    <div className="login card">
      <div className="row"><img src="/icon.svg" width="28" height="28" alt="" /><strong>P1 Monitor</strong></div>
      <form onSubmit={submit}>
        {setup && <p className="muted small">{t("setupTitle")}</p>}
        <input className="in" placeholder={t("username")} autoComplete="username" value={username}
               onChange={(e) => setUsername(e.target.value)} required />
        <input className="in" type="password" placeholder={t("password")} value={password}
               autoComplete={setup ? "new-password" : "current-password"} minLength={setup ? 8 : 1}
               onChange={(e) => setPassword(e.target.value)} required />
        {error && <div className="err small">{error}</div>}
        <button className="btn primary" disabled={busy}>{setup ? t("setupBtn") : t("login")}</button>
      </form>
    </div>
  );
}
