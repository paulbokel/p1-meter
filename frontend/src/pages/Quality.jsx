import { useEffect, useMemo, useState } from "react";
import { api, qs } from "../api.js";
import { fmt, useLocale, useT } from "../i18n.js";
import { iso, period, TZ, VIEWS } from "../time.js";
import EChart, { baseOption, cssVar } from "../components/EChart.jsx";
import { title } from "./History.jsx";

const VIEW_LIST = ["day", "week", "month", "year"];
const PHASE_COLORS = ["--t1", "--t2", "--gas"];
const LIMITS = [207, 253]; // NEN-EN 50160: 230 V ± 10 %

export default function Quality() {
  const t = useT();
  const loc = useLocale();
  const [view, setView] = useState("day");
  const [anchor, setAnchor] = useState(() => new Date());
  const [volt, setVolt] = useState(null);
  const [daily, setDaily] = useState(null);
  const [events, setEvents] = useState([]);

  const per = useMemo(() => period(view, anchor), [view, anchor]);
  const isCurrent = per.end > new Date();
  const move = (n) => setAnchor(VIEWS[view].next(per.start, n));

  useEffect(() => {
    let off = false;
    const q = qs({ start: iso(per.start), end: iso(per.end) });
    setVolt(null); setDaily(null);
    api(`/api/quality/voltage?${q}`).then((d) => !off && setVolt(d)).catch(() => !off && setVolt({ rows: [], phases: [] }));
    if (view !== "day") api(`/api/quality/daily?${q}`).then((d) => !off && setDaily(d.rows)).catch(() => {});
    return () => { off = true; };
  }, [per, view]);
  useEffect(() => { api("/api/quality/events?limit=50").then(setEvents).catch(() => {}); }, []);

  const option = useMemo(() => {
    if (!volt?.rows?.length) return null;
    const o = baseOption();
    const x = (r) => Date.parse(r.t);
    const series = [];
    volt.phases.forEach((p, i) => {
      const color = cssVar(PHASE_COLORS[i]);
      // min/max band: invisible base at min, stacked (max - min) filled area
      series.push({ type: "line", name: `_min${p}`, stack: `band${p}`, symbol: "none", lineStyle: { opacity: 0 },
        data: volt.rows.map((r) => [x(r), r[`l${p}_min_v`]]), tooltip: { show: false } });
      series.push({ type: "line", name: `_band${p}`, stack: `band${p}`, symbol: "none", lineStyle: { opacity: 0 },
        areaStyle: { color, opacity: 0.08 },
        data: volt.rows.map((r) => [x(r), r[`l${p}_max_v`] == null ? null : r[`l${p}_max_v`] - r[`l${p}_min_v`]]) });
      series.push({ type: "line", name: `L${p}`, symbol: "none", lineStyle: { color, width: 1.5 }, itemStyle: { color },
        data: volt.rows.map((r) => [x(r), r[`l${p}_avg_v`]]),
        ...(i === 0 ? { markLine: { symbol: "none", silent: true, label: { color: cssVar("--muted"), fontSize: 11 },
          lineStyle: { color: cssVar("--warn"), type: "dashed" }, data: LIMITS.map((v) => ({ yAxis: v })) } } : {}) });
    });
    const byTs = new Map(volt.rows.map((r) => [x(r), r]));
    return {
      ...o,
      legend: { ...o.legend, data: volt.phases.map((p) => `L${p}`) },
      tooltip: {
        ...o.tooltip,
        formatter: (ps) => {
          const r = byTs.get(ps[0]?.value?.[0]);
          if (!r) return "";
          const when = new Date(r.t).toLocaleString(loc, { timeZone: TZ, dateStyle: "short", timeStyle: "short" });
          return [when, ...volt.phases.map((p) =>
            `L${p}: ${fmt(loc, r[`l${p}_avg_v`], 1)} V <span style="opacity:.6">(${fmt(loc, r[`l${p}_min_v`], 1)}–${fmt(loc, r[`l${p}_max_v`], 1)})</span>`)].join("<br>");
        },
      },
      xAxis: { ...o.xAxis, type: "time" },
      yAxis: { ...o.yAxis, type: "value", scale: true, axisLabel: { ...o.yAxis.axisLabel, formatter: (v) => `${v} V` } },
      series,
    };
  }, [volt, loc]);

  const extremes = useMemo(() => {
    if (!volt?.rows?.length) return null;
    const vals = (k) => volt.rows.flatMap((r) => volt.phases.map((p) => r[`l${p}_${k}_v`])).filter((v) => v != null);
    const lo = Math.min(...vals("min")), hi = Math.max(...vals("max"));
    return { lo, hi, out: lo < LIMITS[0] || hi > LIMITS[1] };
  }, [volt]);

  const dayLabel = (s) => new Date(s).toLocaleDateString(loc, { weekday: "short", day: "numeric", month: "short", timeZone: TZ });
  const range = (r, p) => (r[`l${p}_min_v`] == null ? "–" : `${fmt(loc, r[`l${p}_min_v`], 1)}–${fmt(loc, r[`l${p}_max_v`], 1)}`);

  return (
    <div className="stack">
      <div className="row">
        <nav className="tabs">
          {VIEW_LIST.map((v) => <button key={v} className={v === view ? "on" : ""} onClick={() => setView(v)}>{t(v)}</button>)}
        </nav>
        <div className="row">
          <button className="btn" onClick={() => move(-1)} aria-label={t("prev")}>‹</button>
          <button className="btn" onClick={() => setAnchor(new Date())} disabled={isCurrent}>{t("thisPeriod")}</button>
          <button className="btn" onClick={() => move(1)} disabled={isCurrent} aria-label={t("next")}>›</button>
        </div>
        <strong>{title(loc, view, per)}</strong>
      </div>

      <section className="card">
        <h2>{t("voltage")} {volt && <span className="muted">· {volt.resolution}</span>}</h2>
        {extremes && (
          <p className="small muted" style={{ marginTop: 0 }}>
            {t("lowest")} <strong className={extremes.lo < LIMITS[0] ? "err" : ""}>{fmt(loc, extremes.lo, 1)} V</strong> ·{" "}
            {t("highest")} <strong className={extremes.hi > LIMITS[1] ? "err" : ""}>{fmt(loc, extremes.hi, 1)} V</strong> ·{" "}
            {extremes.out ? t("outOfRange") : t("withinRange")}
          </p>
        )}
        {option ? <EChart option={option} /> : <p className="muted">{volt ? t("noData") : "…"}</p>}
      </section>

      {daily?.length > 0 && (
        <section className="card">
          <h2>{t("perDay")}</h2>
          <div style={{ overflowX: "auto" }}>
            <table className="t">
              <thead><tr><th>{t("day")}</th>{volt?.phases.map((p) => <th key={p}>L{p} (V)</th>)}
                <th>{t("sags")}</th><th>{t("swells")}</th><th>{t("outages")}</th></tr></thead>
              <tbody>
                {daily.map((r) => (
                  <tr key={r.t}>
                    <td style={{ whiteSpace: "nowrap" }}>{dayLabel(r.t)}</td>
                    {volt?.phases.map((p) => <td key={p} className="num" style={{ whiteSpace: "nowrap" }}>{range(r, p)}</td>)}
                    <td className="num">{r.sags || ""}</td><td className="num">{r.swells || ""}</td>
                    <td className="num">{(r.power_fail || 0) + (r.long_power_fail || 0) || ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      <section className="card">
        <h2>{t("qualityLog")}</h2>
        {events.length === 0 ? <p className="muted">{t("noEvents")}</p> : (
          <table className="t">
            <tbody>
              {events.map((e) => (
                <tr key={e.id}>
                  <td className="small muted" style={{ whiteSpace: "nowrap" }}>
                    {new Date(e.ts).toLocaleString(loc, { timeZone: TZ, dateStyle: "short", timeStyle: "short" })}
                  </td>
                  <td className={`sev-${e.severity}`}>{t(`ev_${e.kind}`)}</td>
                  <td className="small">{e.message}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}
