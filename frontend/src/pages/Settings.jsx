import { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";
import { useT } from "../i18n.js";

export default function Settings({ user, setUser }) {
  const t = useT();
  return (
    <div className="stack">
      <Account user={user} setUser={setUser} t={t} />
      <Contracts admin={user.role === "admin"} t={t} />
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

const EMPTY_CONTRACT = {
  name: "", supplier: "", kind: "fixed", valid_from: new Date().toISOString().slice(0, 10), valid_to: "",
  elec_import_t1: 0.25, elec_import_t2: 0.25, elec_export_t1: 0.05, elec_export_t2: 0.05, netting: true,
  gas_price: 1.3, gas_dynamic: false, vat_pct: 21, fixed_monthly_elec: 0, fixed_monthly_gas: 0,
  energy_tax_rebate_year: 0, advance_monthly: 0,
};
const NUM = ["elec_import_t1", "elec_import_t2", "elec_export_t1", "elec_export_t2", "gas_price", "vat_pct",
  "fixed_monthly_elec", "fixed_monthly_gas", "energy_tax_rebate_year", "advance_monthly"];

function Contracts({ admin, t }) {
  const [list, setList] = useState([]);
  const [form, setForm] = useState(null);
  const [err, setErr] = useState("");
  const load = useCallback(() => api("/api/contracts").then(setList).catch((e) => setErr(e.message)), []);
  useEffect(() => { load(); }, [load]);

  const submit = async (e) => {
    e.preventDefault(); setErr("");
    const body = { ...form, valid_to: form.valid_to || null, supplier: form.supplier || null };
    for (const k of NUM) body[k] = Number(body[k]);
    if (body.kind === "fixed") { body.elec_import_t2 = body.elec_import_t1; body.elec_export_t2 = body.elec_export_t1; }
    try {
      await api(form.id ? `/api/contracts/${form.id}` : "/api/contracts", { method: form.id ? "PUT" : "POST", body });
      setForm(null); load();
    } catch (e2) { setErr(e2.message); }
  };
  const f = (k, label, type = "number", extra = {}) => (
    <label className="small" style={{ display: "grid", gap: 2 }}>{label}
      <input className="in" type={type} step="any" value={form[k] ?? ""} {...extra}
             onChange={(e) => setForm({ ...form, [k]: e.target.value })} /></label>
  );
  const two = form && form.kind !== "fixed";
  const sur = form?.kind === "dynamic" ? ` (${t("surcharge")})` : "";

  return (
    <section className="card"><h2>{t("contracts")}</h2>
      <table className="t"><tbody>
        {list.map((c) => (
          <tr key={c.id}>
            <td><strong>{c.name}</strong> <span className="muted small">{c.supplier} · {t(c.kind)}</span>
              <div className="muted small">{c.valid_from} → {c.valid_to || "…"} · € {c.elec_import_t1}{c.kind !== "fixed" && ` / ${c.elec_import_t2}`} /kWh · € {c.gas_price} /m³</div></td>
            <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>{admin && <>
              <button className="btn" onClick={() => setForm({ ...EMPTY_CONTRACT, ...c, valid_to: c.valid_to || "", supplier: c.supplier || "" })}>{t("edit")}</button>{" "}
              <button className="btn" onClick={() => confirm(`${t("delete")} ${c.name}?`) && api(`/api/contracts/${c.id}`, { method: "DELETE" }).then(load)}>✕</button>
            </>}</td>
          </tr>
        ))}
      </tbody></table>
      {admin && !form && <button className="btn" style={{ marginTop: 10 }} onClick={() => setForm({ ...EMPTY_CONTRACT })}>{t("addContract")}</button>}
      {form && (
        <form onSubmit={submit} className="stack" style={{ marginTop: 12 }}>
          <div className="grid">
            {f("name", t("name"), "text", { required: true })}
            {f("supplier", t("supplier"), "text")}
            <label className="small" style={{ display: "grid", gap: 2 }}>{t("kind")}
              <select className="in" value={form.kind} onChange={(e) => setForm({ ...form, kind: e.target.value })}>
                <option value="fixed">{t("fixed")}</option><option value="daynight">{t("daynight")}</option><option value="dynamic">{t("dynamic")}</option>
              </select></label>
            {f("valid_from", t("validFrom"), "date", { required: true })}
            {f("valid_to", t("validTo"), "date")}
          </div>
          {form.kind === "dynamic" && <p className="muted small">{t("dynamicHint")}</p>}
          <div className="grid">
            {f("elec_import_t1", t("importPrice") + (two ? " T1" : "") + sur)}
            {two && f("elec_import_t2", t("importPrice") + " T2" + sur)}
            {f("elec_export_t1", t("exportPrice") + (two ? " T1" : ""))}
            {two && f("elec_export_t2", t("exportPrice") + " T2")}
            {f("gas_price", t("gasPrice") + (form.gas_dynamic ? ` (${t("surcharge")})` : ""))}
            {f("fixed_monthly_elec", t("monthlyElec"))}
            {f("fixed_monthly_gas", t("monthlyGas"))}
            {f("energy_tax_rebate_year", t("rebate"))}
            {f("advance_monthly", t("advanceMonthly"))}
            {form.kind === "dynamic" && f("vat_pct", t("vat"))}
          </div>
          <div className="row small">
            <label><input type="checkbox" checked={form.netting} onChange={(e) => setForm({ ...form, netting: e.target.checked })} /> {t("netting")}</label>
            <label><input type="checkbox" checked={form.gas_dynamic} onChange={(e) => setForm({ ...form, gas_dynamic: e.target.checked })} /> {t("gas")} {t("dynamic").toLowerCase()}</label>
          </div>
          <div className="row"><button className="btn primary">{t("save")}</button>
            <button type="button" className="btn" onClick={() => setForm(null)}>{t("cancel")}</button></div>
        </form>
      )}
      {err && <p className="err small">{err}</p>}
    </section>
  );
}
