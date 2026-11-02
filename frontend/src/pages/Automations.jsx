import { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";
import { fmt, useLocale, useT } from "../i18n.js";
import { TZ } from "../time.js";

const TRIGGER_FIELDS = {
  surplus: [["watts", "number", "W"], ["minutes", "number", "min"]],
  price_below: [["eur_kwh", "number", "€/kWh"], ["basis", ["all_in", "spot"]]],
  cheapest_hours: [["hours", "number", "h"], ["from", "time"], ["to", "time"], ["contiguous", "checkbox"]],
};
const ACTION_FIELDS = {
  mqtt: [["topic", "text"], ["payload", "text"], ["retain", "checkbox"]],
  homeassistant: [["url", "url"], ["token", "password"], ["service", "text"], ["data", "json"]],
  webhook: [["url", "url"], ["method", ["POST", "PUT", "GET"]], ["body", "json"], ["secret", "password"]],
};
const ACTION_DEFAULTS = {
  mqtt: { kind: "mqtt", topic: "", payload: "ON" },
  homeassistant: { kind: "homeassistant", url: "http://homeassistant.local:8123", token: "", service: "switch.turn_on", data: { entity_id: "" } },
  webhook: { kind: "webhook", url: "", method: "POST" },
};
const offFor = (a) => (a.kind === "mqtt" ? { ...a, payload: "OFF" }
  : a.kind === "homeassistant" ? { ...a, service: a.service.replace(/turn_on$/, "turn_off") } : { ...a });

const EMPTY = {
  name: "", enabled: true, trigger_kind: "surplus", trigger_params: { watts: 1500, minutes: 5 },
  action: ACTION_DEFAULTS.mqtt, off_action: { ...ACTION_DEFAULTS.mqtt, payload: "OFF" }, min_on_minutes: 0,
};

export default function Automations({ user }) {
  const t = useT();
  const loc = useLocale();
  const admin = user.role === "admin";
  const [items, setItems] = useState([]);
  const [kinds, setKinds] = useState(null);
  const [edit, setEdit] = useState(null);
  const [plans, setPlans] = useState({});
  const [msg, setMsg] = useState("");
  const load = useCallback(() => api("/api/automations").then((list) => {
    setItems(list);
    for (const a of list.filter((x) => x.trigger_kind === "cheapest_hours")) {
      api(`/api/automations/${a.id}/plan`).then((p) => setPlans((o) => ({ ...o, [a.id]: p }))).catch(() => {});
    }
  }).catch((e) => setMsg(e.message)), []);
  useEffect(() => { load(); api("/api/automations/kinds").then(setKinds).catch(() => {}); }, [load]);

  const run = async (fn, ok) => { setMsg(""); try { await fn(); if (ok) setMsg(ok); await load(); } catch (e) { setMsg(e.message); } };
  const when = (v) => (v ? new Date(v).toLocaleString(loc, { timeZone: TZ, dateStyle: "short", timeStyle: "short" }) : "–");
  const hm = (v) => new Date(v).toLocaleTimeString(loc, { timeZone: TZ, hour: "2-digit", minute: "2-digit" });
  const describe = (a) => {
    const p = a.trigger_params;
    if (a.trigger_kind === "surplus") return t("descSurplus").replace("{w}", fmt(loc, p.watts)).replace("{m}", p.minutes);
    if (a.trigger_kind === "price_below") return t("descPrice").replace("{p}", fmt(loc, p.eur_kwh, 3)) + (p.basis === "spot" ? ` (${t("spot")})` : "");
    return t("descCheapest").replace("{h}", fmt(loc, p.hours, p.hours % 1 ? 1 : 0)).replace("{f}", p.from).replace("{t}", p.to);
  };

  return (
    <div className="stack">
      <section className="card">
        <h2>{t("automations")}</h2>
        <p className="small muted" style={{ marginTop: 0 }}>{t("automationsHint")}</p>
        {items.length === 0 && <p className="muted">{t("noAutomations")}</p>}
        {items.map((a) => (
          <div key={a.id} className="auto">
            <div className="row">
              <span className="dot" style={{ background: a.active ? "var(--exp)" : undefined }} title={a.active ? t("on") : t("off")} />
              <strong>{a.name}</strong>
              <span className="small muted">{describe(a)} → {t(`act_${a.action.kind}`)}</span>
              <span className="spacer" />
              {admin && <>
                <label className="small row"><input type="checkbox" checked={a.enabled}
                  onChange={(e) => run(() => api(`/api/automations/${a.id}`, { method: "PUT", body: { ...pick(a), enabled: e.target.checked } }))} />{t("enabled")}</label>
                <button className="btn" onClick={() => run(() => api(`/api/automations/${a.id}/test?state=on`, { method: "POST" }), t("testSent"))}>{t("test")} {t("on")}</button>
                {a.off_action && <button className="btn" onClick={() => run(() => api(`/api/automations/${a.id}/test?state=off`, { method: "POST" }), t("testSent"))}>{t("test")} {t("off")}</button>}
                <button className="btn" onClick={() => setEdit(pick(a, true))}>{t("edit")}</button>
                <button className="btn" aria-label={t("delete")} onClick={() => confirm(`${t("delete")} ${a.name}?`) && run(() => api(`/api/automations/${a.id}`, { method: "DELETE" }))}>✕</button>
              </>}
            </div>
            <div className="small muted">
              {a.active ? t("on") : t("off")}{a.changed_at && <> {t("since")} {when(a.changed_at)}</>}
              {a.last_result && a.last_result !== "ok" && <span className={a.last_result.startsWith("ok") ? "" : "err"}> · {a.last_result}</span>}
              {plans[a.id] && (plans[a.id].slots?.length
                ? <> · {t("planned")}: {plans[a.id].slots.map(([s, e]) => `${hm(s)}–${hm(e)}`).join(", ")}</>
                : <> · {t("planPending")}</>)}
            </div>
          </div>
        ))}
        {admin && !edit && <button className="btn" style={{ marginTop: 12 }} onClick={() => setEdit({ ...EMPTY })}>{t("addAutomation")}</button>}
        {msg && <p className="small">{msg}</p>}
      </section>
      {edit && kinds && <Editor value={edit} kinds={kinds} t={t} onCancel={() => setEdit(null)}
        onSave={(body) => run(async () => {
          await api(edit.id ? `/api/automations/${edit.id}` : "/api/automations", { method: edit.id ? "PUT" : "POST", body });
          setEdit(null);
        }, t("saved"))} />}
    </div>
  );
}

function pick(a, withId = false) {
  const o = { name: a.name, enabled: a.enabled, trigger_kind: a.trigger_kind, trigger_params: a.trigger_params,
    action: a.action, off_action: a.off_action, min_on_minutes: a.min_on_minutes };
  return withId ? { ...o, id: a.id } : o;
}

function Field({ name, type, unit, value, onChange, t }) {
  const label = t(`f_${name}`);
  if (Array.isArray(type)) {
    return <label className="fld"><span>{label}</span>
      <select className="in" value={value ?? type[0]} onChange={(e) => onChange(e.target.value)}>
        {type.map((o) => <option key={o} value={o}>{t(`o_${o}`) === `o_${o}` ? o : t(`o_${o}`)}</option>)}
      </select></label>;
  }
  if (type === "checkbox") {
    return <label className="fld row"><input type="checkbox" checked={!!value} onChange={(e) => onChange(e.target.checked)} /><span>{label}</span></label>;
  }
  if (type === "json") {
    return <JsonField label={label} value={value} onChange={onChange} />;
  }
  return <label className="fld"><span>{label}{unit ? ` (${unit})` : ""}</span>
    <input className="in" type={type} step={type === "number" ? "any" : undefined} value={value ?? ""}
           onChange={(e) => onChange(type === "number" ? (e.target.value === "" ? "" : Number(e.target.value)) : e.target.value)} /></label>;
}

function JsonField({ label, value, onChange }) {
  const [text, setText] = useState(value ? JSON.stringify(value) : "");
  const [bad, setBad] = useState(false);
  return <label className="fld wide"><span>{label} (JSON)</span>
    <input className={"in" + (bad ? " invalid" : "")} value={text} placeholder='{"entity_id": "switch.boiler"}'
      onChange={(e) => {
        setText(e.target.value);
        try { onChange(e.target.value.trim() ? JSON.parse(e.target.value) : undefined); setBad(false); } catch { setBad(true); }
      }} /></label>;
}

function ActionEditor({ value, onChange, t }) {
  return (
    <div className="row" style={{ alignItems: "flex-end" }}>
      <label className="fld"><span>{t("actionKind")}</span>
        <select className="in" value={value.kind} onChange={(e) => onChange({ ...ACTION_DEFAULTS[e.target.value] })}>
          {Object.keys(ACTION_FIELDS).map((k) => <option key={k} value={k}>{t(`act_${k}`)}</option>)}
        </select></label>
      {ACTION_FIELDS[value.kind].map(([f, type]) => (
        <Field key={f} name={f} type={type} value={value[f]} t={t} onChange={(v) => onChange({ ...value, [f]: v })} />
      ))}
    </div>
  );
}

function Editor({ value, kinds, t, onSave, onCancel }) {
  const [f, setF] = useState(value);
  const [withOff, setWithOff] = useState(!!value.off_action);
  const setParam = (k, v) => setF({ ...f, trigger_params: { ...f.trigger_params, [k]: v } });
  const submit = (e) => {
    e.preventDefault();
    onSave({ ...f, off_action: withOff ? (f.off_action || offFor(f.action)) : null, id: undefined });
  };
  return (
    <form className="card" onSubmit={submit}>
      <h2>{value.id ? t("edit") : t("addAutomation")}</h2>
      <div className="row" style={{ alignItems: "flex-end" }}>
        <label className="fld wide"><span>{t("name")}</span>
          <input className="in" required maxLength={80} value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></label>
        <label className="fld"><span>{t("trigger")}</span>
          <select className="in" value={f.trigger_kind}
                  onChange={(e) => setF({ ...f, trigger_kind: e.target.value, trigger_params: { ...kinds.triggers[e.target.value] } })}>
            {Object.keys(TRIGGER_FIELDS).map((k) => <option key={k} value={k}>{t(`trig_${k}`)}</option>)}
          </select></label>
        {TRIGGER_FIELDS[f.trigger_kind].map(([k, type, unit]) => (
          <Field key={k} name={k} type={type} unit={unit} value={f.trigger_params[k]} t={t} onChange={(v) => setParam(k, v)} />
        ))}
      </div>
      <h3 className="small muted">{t("whenOn")}</h3>
      <ActionEditor value={f.action} t={t} onChange={(a) => setF({ ...f, action: a, off_action: withOff ? offFor(a) : f.off_action })} />
      <label className="row small" style={{ marginTop: 10 }}>
        <input type="checkbox" checked={withOff} onChange={(e) => { setWithOff(e.target.checked); if (e.target.checked && !f.off_action) setF({ ...f, off_action: offFor(f.action) }); }} />
        {t("withOff")}
      </label>
      {withOff && f.off_action && <ActionEditor value={f.off_action} t={t} onChange={(a) => setF({ ...f, off_action: a })} />}
      <div className="row" style={{ marginTop: 10, alignItems: "flex-end" }}>
        <Field name="min_on_minutes" type="number" unit="min" value={f.min_on_minutes} t={t}
               onChange={(v) => setF({ ...f, min_on_minutes: v === "" ? 0 : v })} />
        <span className="spacer" />
        <button type="button" className="btn" onClick={onCancel}>{t("cancel")}</button>
        <button className="btn primary">{t("save")}</button>
      </div>
    </form>
  );
}
