import { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";
import { useLocale, useT } from "../i18n.js";

export default function Alerts({ user }) {
  const t = useT();
  const loc = useLocale();
  const admin = user.role === "admin";
  const [events, setEvents] = useState([]);
  const [onlyAlerts, setOnlyAlerts] = useState(true);
  const [rules, setRules] = useState([]);
  const [channels, setChannels] = useState([]);
  const [err, setErr] = useState("");

  const load = useCallback(() => {
    api(`/api/events?limit=100${onlyAlerts ? "&kind=alert" : ""}`).then(setEvents).catch(() => {});
    api("/api/alerts/rules").then(setRules).catch(() => {});
    api("/api/channels").then(setChannels).catch(() => {});
  }, [onlyAlerts]);
  useEffect(() => { load(); }, [load]);

  const run = async (fn) => { setErr(""); try { await fn(); load(); } catch (e) { setErr(e.message); } };
  const when = (v) => (v ? new Date(v).toLocaleString(loc) : "–");

  return (
    <div className="stack">
      <PushToggle t={t} />
      <section className="card">
        <div className="row">
          <h2 style={{ margin: 0 }}>{t("alerts")}</h2><span className="spacer" />
          <label className="small muted"><input type="checkbox" checked={!onlyAlerts} onChange={(e) => setOnlyAlerts(!e.target.checked)} /> {t("allEvents")}</label>
        </div>
        {events.length ? (
          <table className="t"><tbody>
            {events.map((e) => (
              <tr key={e.id} style={{ opacity: e.acknowledged ? 0.55 : 1 }}>
                <td className="muted small" style={{ whiteSpace: "nowrap" }}>{when(e.ts)}</td>
                <td className={"sev-" + e.severity}>{e.rule_name || e.kind}</td>
                <td>{e.message}{e.notify_result && <div className="muted small">→ {e.notify_result}</div>}</td>
                <td style={{ textAlign: "right" }}>{!e.acknowledged &&
                  <button className="btn" onClick={() => run(() => api(`/api/events/${e.id}/ack`, { method: "POST" }))}>{t("acknowledge")}</button>}</td>
              </tr>
            ))}
          </tbody></table>
        ) : <p className="muted">{t("noEvents")}</p>}
      </section>

      <section className="card">
        <h2>{t("rules")}</h2>
        <table className="t"><tbody>
          {rules.map((r) => <RuleRow key={r.id} r={r} t={t} loc={loc} admin={admin} channels={channels} run={run} />)}
        </tbody></table>
        {admin && <NewRule t={t} run={run} />}
      </section>

      {admin && <Channels channels={channels} t={t} run={run} />}
      {err && <p className="err">{err}</p>}
    </div>
  );
}

const PARAM_LABELS = {
  sigma: "σ", weeks: "weeks", min_kwh: "min kWh", watts: "W", days: "days", minutes: "min", kwh: "kWh",
  from: "from", to: "to", m3: "m³", min: "min V", max: "max V",
};

function RuleRow({ r, t, loc, admin, channels, run }) {
  const [edit, setEdit] = useState(false);
  const [form, setForm] = useState(r);
  useEffect(() => setForm(r), [r]);
  const save = (patch) => run(() => api(`/api/alerts/rules/${r.id}`, { method: "PUT", body: { ...strip(form), ...patch } }));
  const snoozed = r.snooze_until && new Date(r.snooze_until) > new Date();
  const params = Object.entries(r.params).filter(([k]) => k !== "weekdays");
  return (
    <tr>
      <td>
        <strong>{r.name}</strong>
        <div className="muted small">
          {params.map(([k, v]) => `${PARAM_LABELS[k] || k} ${v}`).join(" · ")}
          {r.quiet_start && ` · ${t("quietHours")} ${r.quiet_start.slice(0, 5)}–${r.quiet_end.slice(0, 5)}`}
          {snoozed && ` · ${t("snoozedUntil")} ${new Date(r.snooze_until).toLocaleString(loc)}`}
        </div>
        {edit && (
          <div className="stack" style={{ marginTop: 8 }}>
            <div className="row">
              {params.map(([k, v]) => (
                <label key={k} className="small">{PARAM_LABELS[k] || k}{" "}
                  <input className="in" style={{ width: 90 }} value={form.params[k]}
                         onChange={(e) => setForm({ ...form, params: { ...form.params, [k]: isNaN(+e.target.value) || e.target.value === "" ? e.target.value : +e.target.value } })} />
                </label>
              ))}
            </div>
            <div className="row small">
              {t("quietHours")}
              <input className="in" type="time" value={form.quiet_start?.slice(0, 5) || ""} onChange={(e) => setForm({ ...form, quiet_start: e.target.value || null })} />–
              <input className="in" type="time" value={form.quiet_end?.slice(0, 5) || ""} onChange={(e) => setForm({ ...form, quiet_end: e.target.value || null })} />
              {t("cooldown")} <input className="in" style={{ width: 80 }} type="number" min="0" value={form.cooldown_min}
                                     onChange={(e) => setForm({ ...form, cooldown_min: +e.target.value })} />
            </div>
            <div className="row small">
              {t("channels")}:
              {channels.length === 0 && <span className="muted">–</span>}
              {channels.map((c) => (
                <label key={c.id}><input type="checkbox" checked={form.channel_ids.includes(c.id)}
                  onChange={(e) => setForm({ ...form, channel_ids: e.target.checked ? [...form.channel_ids, c.id] : form.channel_ids.filter((x) => x !== c.id) })} /> {c.name}</label>
              ))}
              {form.channel_ids.length === 0 && <span className="muted">({t("allChannels")})</span>}
            </div>
            <div className="row">
              <button className="btn primary" onClick={() => { save({}); setEdit(false); }}>{t("save")}</button>
              <button className="btn" onClick={() => { setForm(r); setEdit(false); }}>{t("cancel")}</button>
              <span className="spacer" />
              <button className="btn" onClick={() => confirm(`${t("delete")} ${r.name}?`) && run(() => api(`/api/alerts/rules/${r.id}`, { method: "DELETE" }))}>{t("delete")}</button>
            </div>
          </div>
        )}
      </td>
      <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
        <select className="in" value="" onChange={(e) => e.target.value !== "" && run(() => api(`/api/alerts/rules/${r.id}/snooze`, { method: "POST", body: { minutes: +e.target.value } }))}>
          <option value="">{t("snooze")}</option>
          <option value="60">1 h</option><option value="480">8 h</option><option value="1440">24 h</option>
          <option value="10080">7 d</option>{snoozed && <option value="0">✕</option>}
        </select>{" "}
        {admin && <>
          <label className="small"><input type="checkbox" checked={r.enabled} onChange={(e) => save({ enabled: e.target.checked })} /> {t("enabled")}</label>{" "}
          <button className="btn" onClick={() => setEdit(!edit)}>{t("edit")}</button>
        </>}
      </td>
    </tr>
  );
}

const strip = (r) => ({ kind: r.kind, name: r.name, enabled: r.enabled, params: r.params, channel_ids: r.channel_ids,
  quiet_start: r.quiet_start || null, quiet_end: r.quiet_end || null, cooldown_min: r.cooldown_min });

function NewRule({ t, run }) {
  const [kinds, setKinds] = useState({});
  const [kind, setKind] = useState("");
  useEffect(() => { api("/api/alerts/kinds").then(setKinds).catch(() => {}); }, []);
  return (
    <div className="row" style={{ marginTop: 12 }}>
      <select className="in" value={kind} onChange={(e) => setKind(e.target.value)}>
        <option value="">{t("add")}…</option>
        {Object.keys(kinds).map((k) => <option key={k} value={k}>{k.replaceAll("_", " ")}</option>)}
      </select>
      <button className="btn" disabled={!kind} onClick={() => run(async () => {
        await api("/api/alerts/rules", { method: "POST", body: { kind, name: kind.replaceAll("_", " "), params: kinds[kind] } });
        setKind("");
      })}>{t("add")}</button>
    </div>
  );
}

function Channels({ channels, t, run }) {
  const [fields, setFields] = useState({});
  const [form, setForm] = useState(null);
  const [msg, setMsg] = useState({});
  useEffect(() => { api("/api/channels/fields").then(setFields).catch(() => {}); }, []);
  const test = async (c) => {
    setMsg({ ...msg, [c.id]: "…" });
    try { await api(`/api/channels/${c.id}/test`, { method: "POST" }); setMsg((m) => ({ ...m, [c.id]: "✓" })); }
    catch (e) { setMsg((m) => ({ ...m, [c.id]: e.message })); }
  };
  const submit = (e) => {
    e.preventDefault();
    run(async () => {
      const body = { kind: form.kind, name: form.name, config: form.config, enabled: form.enabled ?? true };
      await api(form.id ? `/api/channels/${form.id}` : "/api/channels", { method: form.id ? "PUT" : "POST", body });
      setForm(null);
    });
  };
  return (
    <section className="card">
      <h2>{t("channels")}</h2>
      <table className="t"><tbody>
        {channels.map((c) => (
          <tr key={c.id}>
            <td><strong>{c.name}</strong> <span className="muted small">{c.kind}</span>
              {msg[c.id] && <div className="small">{msg[c.id]}</div>}</td>
            <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
              <button className="btn" onClick={() => test(c)}>{t("test")}</button>{" "}
              <button className="btn" onClick={() => setForm({ ...c })}>{t("edit")}</button>{" "}
              <button className="btn" onClick={() => confirm(`${t("delete")} ${c.name}?`) && run(() => api(`/api/channels/${c.id}`, { method: "DELETE" }))}>✕</button>
            </td>
          </tr>
        ))}
      </tbody></table>
      {form ? (
        <form className="stack" style={{ marginTop: 12 }} onSubmit={submit}>
          <div className="row">
            <select className="in" value={form.kind} disabled={!!form.id} onChange={(e) => setForm({ ...form, kind: e.target.value, config: {} })}>
              {Object.keys(fields).map((k) => <option key={k} value={k}>{k}</option>)}
            </select>
            <input className="in" placeholder={t("name")} value={form.name} required onChange={(e) => setForm({ ...form, name: e.target.value })} />
          </div>
          <div className="row">
            {(fields[form.kind] || []).map((f) => (
              <input key={f} className="in" placeholder={f} value={form.config[f] ?? ""}
                     type={["password", "token", "bot_token", "secret"].includes(f) ? "password" : "text"}
                     onChange={(e) => setForm({ ...form, config: { ...form.config, [f]: e.target.value } })} />
            ))}
          </div>
          <div className="row"><button className="btn primary">{t("save")}</button>
            <button type="button" className="btn" onClick={() => setForm(null)}>{t("cancel")}</button></div>
        </form>
      ) : (
        <button className="btn" style={{ marginTop: 12 }} onClick={() => setForm({ kind: "ntfy", name: "", config: {} })}>{t("add")}</button>
      )}
    </section>
  );
}

function urlB64ToUint8Array(s) {
  const pad = "=".repeat((4 - (s.length % 4)) % 4);
  const raw = atob((s + pad).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from([...raw].map((c) => c.charCodeAt(0)));
}

function PushToggle({ t }) {
  const supported = "serviceWorker" in navigator && "PushManager" in window && window.isSecureContext;
  const [on, setOn] = useState(false);
  const [err, setErr] = useState("");
  useEffect(() => {
    if (!supported) return;
    navigator.serviceWorker.ready.then((reg) => reg.pushManager.getSubscription()).then((s) => setOn(!!s)).catch(() => {});
  }, [supported]);
  const enable = async () => {
    setErr("");
    try {
      const perm = await Notification.requestPermission();
      if (perm !== "granted") throw new Error(perm);
      const { public_key } = await api("/api/push/key");
      const reg = await navigator.serviceWorker.ready;
      const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: urlB64ToUint8Array(public_key) });
      await api("/api/push/subscribe", { method: "POST", body: sub.toJSON() });
      setOn(true);
    } catch (e) { setErr(e.message); }
  };
  const disable = async () => {
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.getSubscription();
    if (sub) { await api("/api/push/unsubscribe", { method: "POST", body: sub.toJSON() }).catch(() => {}); await sub.unsubscribe(); }
    setOn(false);
  };
  return (
    <section className="card row">
      <span>{supported ? (on ? t("pushOn") : t("enablePush")) : t("pushUnsupported")}</span>
      <span className="spacer" />
      {supported && (on ? <button className="btn" onClick={disable}>✕</button> : <button className="btn primary" onClick={enable}>{t("enabled")}</button>)}
      {err && <span className="err small">{err}</span>}
    </section>
  );
}
