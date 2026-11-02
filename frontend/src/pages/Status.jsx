import { Fragment, useEffect, useState } from "react";
import { api } from "../api.js";
import { fmt, useLocale, useT } from "../i18n.js";

export default function Status() {
  const t = useT();
  const loc = useLocale();
  const [s, setS] = useState(null);
  const [err, setErr] = useState(false);

  useEffect(() => {
    const load = () => api("/api/status").then((d) => { setS(d); setErr(false); }).catch(() => setErr(true));
    load();
    const id = setInterval(load, 5000);
    return () => clearInterval(id);
  }, []);

  if (!s) return <p className="muted">{err ? "backend unreachable" : "…"}</p>;
  const c = s.collector, db = s.database, m = s.meter || {}, b = s.backup;
  const when = (v) => (v ? new Date(v).toLocaleString(loc) : "–");
  const ago = (sec) => sec == null ? "–" : sec < 90 ? `${Math.round(sec)} s` : sec < 5400 ? `${Math.round(sec / 60)} min` : `${Math.round(sec / 3600)} h`;
  const bytes = (n) => n == null ? "–" : n > 1e9 ? `${fmt(loc, n / 1e9, 2)} GB` : `${fmt(loc, n / 1e6, 1)} MB`;
  const KV = ({ rows }) => (
    <dl className="kv">{rows.map(([k, v]) => <Fragment key={k}><dt>{k}</dt><dd>{v ?? "–"}</dd></Fragment>)}</dl>
  );

  return (
    <div className="stack">
      <div className="row">
        <span className={"dot " + (s.healthy ? "ok" : "bad")} />
        <strong>{s.healthy ? t("collecting") : t("notCollecting")}</strong>
      </div>
      <div className="grid">
        <section className="card"><h2>{t("collector")}</h2>
          <KV rows={[["Host", c.meter_host], ["Driver", c.driver], [t("online"), c.online ? t("yes") : t("no")],
            [t("lastReading"), ago(c.last_reading_age_s)], [t("readingsLastHour"), fmt(loc, db.readings_last_hour)],
            [t("failures"), c.consecutive_failures], [t("buffered"), c.buffered_readings],
            [t("runningSince"), when(c.started_at)], [t("lastError"), c.last_error || "–"]]} />
        </section>
        <section className="card"><h2>{t("meter")}</h2>
          <KV rows={[[t("model"), m.meter_model], ["DSMR", m.smr_version], ["Product", m.product_name || m.product_type],
            [t("firmware"), m.firmware_version], ["API", m.api_version],
            ["Wi-Fi", m.wifi_ssid ? `${m.wifi_ssid} (${m.wifi_strength}%)` : null]]} />
        </section>
        <section className="card"><h2>{t("database")}</h2>
          <KV rows={[[t("size"), bytes(db.size_bytes)], [t("historySince"), when(db.history_since)],
            [t("rawSince"), when(db.oldest_raw)], [t("epoch"), db.epoch], [t("schema"), db.migrations.at(-1)],
            [t("lastBackup"), b ? `${when(b.finished_at || b.started_at)} ${b.ok ? "✓" : "✗"}` : t("never")],
            ["Backup", b ? bytes(b.size_bytes) : "–"]]} />
        </section>
      </div>
      <section className="card"><h2>{t("events")}</h2>
        {s.events.length ? (
          <table className="t"><tbody>
            {s.events.map((e, i) => (
              <tr key={i}><td className="muted small">{when(e.ts)}</td><td className={"sev-" + e.severity}>{e.kind}</td><td>{e.message}</td></tr>
            ))}
          </tbody></table>
        ) : <p className="muted">{t("noEvents")}</p>}
      </section>
      <p className="muted small"><a href="/api/docs" target="_blank" rel="noreferrer">API docs (OpenAPI)</a></p>
    </div>
  );
}
