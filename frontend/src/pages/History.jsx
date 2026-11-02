import { useCallback, useEffect, useMemo, useState } from "react";
import { api, qs } from "../api.js";
import { fmt, useLocale, useT } from "../i18n.js";
import { iso, period, sameLastYear, VIEWS } from "../time.js";
import EChart, { baseOption, cssVar } from "../components/EChart.jsx";
import { Stat } from "./Live.jsx";

const VIEW_KEYS = ["day", "week", "month", "year", "all"];
const DRILL = { year: "month", month: "day", week: "day", all: "year" };

function label(loc, view, d) {
  switch (view) {
    case "day": return d.toLocaleTimeString(loc, { hour: "2-digit", minute: "2-digit" });
    case "week": return d.toLocaleDateString(loc, { weekday: "short", day: "numeric" });
    case "month": return String(d.getDate());
    case "year": return d.toLocaleDateString(loc, { month: "short" });
    default: return String(d.getFullYear());
  }
}

function title(loc, view, { start, end }) {
  switch (view) {
    case "day": return start.toLocaleDateString(loc, { weekday: "long", day: "numeric", month: "long", year: "numeric" });
    case "week": {
      const last = new Date(end.getTime() - 86400000);
      return `${start.toLocaleDateString(loc, { day: "numeric", month: "short" })} – ${last.toLocaleDateString(loc, { day: "numeric", month: "short", year: "numeric" })}`;
    }
    case "month": return start.toLocaleDateString(loc, { month: "long", year: "numeric" });
    case "year": return String(start.getFullYear());
    default: return "";
  }
}

function pct(cur, prev) {
  if (!prev) return null;
  return ((cur - prev) / prev) * 100;
}

export default function History() {
  const t = useT();
  const loc = useLocale();
  const [view, setView] = useState("day");
  const [anchor, setAnchor] = useState(() => new Date());
  const [data, setData] = useState(null);
  const [cmp, setCmp] = useState({});
  const [power, setPower] = useState(null);
  const [zoom, setZoom] = useState(null);

  const per = useMemo(() => period(view, anchor), [view, anchor]);
  const res = VIEWS[view].resolution;

  useEffect(() => {
    let off = false;
    setData(null); setZoom(null);
    api(`/api/history?${qs({ resolution: res, start: iso(per.start), end: iso(per.end) })}`)
      .then((d) => !off && setData(d)).catch(() => !off && setData({ rows: [], totals: {} }));
    if (view !== "all") {
      // For the running period, compare against the same elapsed part of the reference period.
      const elapsed = Math.min(Date.now(), per.end.getTime()) - per.start.getTime();
      const cut = (p) => ({ start: p.start, end: new Date(Math.min(p.end.getTime(), p.start.getTime() + elapsed)) });
      const prev = cut({ start: VIEWS[view].next(per.start, -1), end: per.start });
      const ly = cut(sameLastYear(per));
      const get = (p) => api(`/api/history/totals?${qs({ start: iso(p.start), end: iso(p.end) })}`);
      Promise.all([get(prev), view === "year" ? null : get(ly)])
        .then(([a, b]) => !off && setCmp({ prev: a, ly: b })).catch(() => setCmp({}));
    } else setCmp({});
    return () => { off = true; };
  }, [view, per, res]);

  // Power detail for the day view (and zoomed ranges): F-HIST-05
  const powerRange = zoom || (view === "day" ? per : null);
  useEffect(() => {
    if (!powerRange) { setPower(null); return; }
    let off = false;
    api(`/api/power?${qs({ start: iso(powerRange.start), end: iso(powerRange.end) })}`)
      .then((d) => !off && setPower(d)).catch(() => {});
    return () => { off = true; };
  }, [powerRange?.start?.getTime(), powerRange?.end?.getTime()]); // eslint-disable-line react-hooks/exhaustive-deps

  const move = (n) => setAnchor(VIEWS[view].next(per.start, n));
  const isCurrent = per.end > new Date();

  const energyOption = useMemo(() => {
    if (!data) return null;
    const o = baseOption();
    const rows = data.rows;
    const cats = rows.map((r) => label(loc, view, new Date(r.t)));
    const neg = (k) => rows.map((r) => (r[k] == null ? null : -r[k]));
    const bar = (name, vals, color, stack) => ({ type: "bar", name, data: vals, stack, itemStyle: { color }, barMaxWidth: 28 });
    return {
      ...o,
      tooltip: { ...o.tooltip, valueFormatter: (v) => `${fmt(loc, Math.abs(v ?? 0), 2)} kWh` },
      xAxis: { ...o.xAxis, type: "category", data: cats },
      grid: { ...o.grid, top: 48 },
      yAxis: { ...o.yAxis, type: "value", axisLabel: { ...o.yAxis.axisLabel, formatter: (v) => `${fmt(loc, v, 1)}` } },
      series: [
        bar(`${t("import")} T1`, rows.map((r) => r.import_t1_kwh), cssVar("--t1"), "imp"),
        bar(`${t("import")} T2`, rows.map((r) => r.import_t2_kwh), cssVar("--t2"), "imp"),
        bar(`${t("export")} T1`, neg("export_t1_kwh"), "#69db7c", "exp"),
        bar(`${t("export")} T2`, neg("export_t2_kwh"), cssVar("--exp"), "exp"),
      ],
    };
  }, [data, loc, view, t]);

  const gasOption = useMemo(() => {
    if (!data) return null;
    const o = baseOption();
    return {
      ...o, legend: { show: false },
      tooltip: { ...o.tooltip, valueFormatter: (v) => `${fmt(loc, v ?? 0, 3)} m³` },
      xAxis: { ...o.xAxis, type: "category", data: data.rows.map((r) => label(loc, view, new Date(r.t))) },
      grid: { ...o.grid, top: 12 },
      yAxis: { ...o.yAxis, type: "value" },
      series: [{ type: "bar", name: t("gas"), data: data.rows.map((r) => r.gas_m3), itemStyle: { color: cssVar("--gas") }, barMaxWidth: 28 }],
    };
  }, [data, loc, view, t]);

  const powerOption = useMemo(() => {
    if (!power) return null;
    const o = baseOption();
    const hasMinMax = power.resolution === "1m";
    const pts = (i) => power.rows.map((r) => [Date.parse(r[0]), r[i]]);
    return {
      ...o, legend: { show: false },
      tooltip: { ...o.tooltip, valueFormatter: (v) => `${fmt(loc, v)} W` },
      xAxis: { ...o.xAxis, type: "time" },
      yAxis: { ...o.yAxis, type: "value", axisLabel: { ...o.yAxis.axisLabel, formatter: (v) => `${fmt(loc, v)} W` } },
      grid: { ...o.grid, top: 12, bottom: 40 },
      dataZoom: [{ type: "inside", filterMode: "none" }, { type: "slider", filterMode: "none", height: 18, bottom: 6 }],
      series: [
        ...(hasMinMax ? [
          { type: "line", name: "max", data: pts(3), showSymbol: false, lineStyle: { width: 0 }, areaStyle: { opacity: 0.08, color: cssVar("--imp") }, tooltip: { show: false } },
          { type: "line", name: "min", data: pts(2), showSymbol: false, lineStyle: { width: 0 }, tooltip: { show: false } },
        ] : []),
        { type: "line", name: t("power"), data: pts(1), showSymbol: false, sampling: "lttb", lineStyle: { width: 1.2, color: cssVar("--imp") } },
      ],
    };
  }, [power, loc, t]);

  // Re-query at finer resolution after zooming (debounced).
  const powerEvents = useMemo(() => ({
    datazoom: (e) => {
      if (!power?.rows?.length) return;
      const first = Date.parse(power.rows[0][0]);
      const last = Date.parse(power.rows[power.rows.length - 1][0]);
      const b = e.batch?.[0] ?? e;
      if (b.start == null) return;
      const s = first + ((last - first) * b.start) / 100;
      const en = first + ((last - first) * b.end) / 100;
      clearTimeout(powerEvents._t);
      powerEvents._t = setTimeout(() => setZoom({ start: new Date(s), end: new Date(en) }), 400);
    },
  }), [power]);

  const drill = useCallback((e) => {
    const next = DRILL[view];
    if (!next || !data?.rows?.[e.dataIndex]) return;
    setAnchor(new Date(data.rows[e.dataIndex].t));
    setView(next);
  }, [view, data]);
  const barEvents = useMemo(() => ({ click: drill }), [drill]);

  const tot = data?.totals || {};
  const delta = (cur, ref, k) => {
    const v = pct(cur, ref);
    if (v == null || !Number.isFinite(v)) return null;
    const good = k === "export_kwh" ? v > 0 : v < 0; // more export or less use is good
    return <span className={"delta " + (good ? "down" : "up")}>{v > 0 ? "+" : ""}{fmt(loc, v)}%</span>;
  };
  const cmpLine = (k) => (cmp.prev || cmp.ly) && (
    <span className="muted">
      {cmp.prev && <>{delta(tot[k], cmp.prev[k], k) ?? "–"} {t("vsPrev")}</>}
      {cmp.ly && <><br />{delta(tot[k], cmp.ly[k], k) ?? "–"} {t("vsLastYear")}</>}
    </span>
  );

  const csv = `/api/export.csv?${qs({ resolution: view === "day" ? "15m" : res, start: iso(per.start), end: iso(per.end) })}`;

  return (
    <div className="stack">
      <div className="row">
        <nav className="tabs">
          {VIEW_KEYS.map((v) => (
            <button key={v} className={v === view ? "on" : ""} onClick={() => setView(v)}>{t(v)}</button>
          ))}
        </nav>
        {view !== "all" && <>
          <button className="btn" onClick={() => move(-1)} aria-label={t("prev")}>‹</button>
          <button className="btn" onClick={() => setAnchor(new Date())} disabled={isCurrent}>{t("thisPeriod")}</button>
          <button className="btn" onClick={() => move(1)} disabled={isCurrent} aria-label={t("next")}>›</button>
        </>}
        <strong>{title(loc, view, per)}</strong>
        <span className="spacer" />
        <a className="btn" href={csv} download>{t("downloadCsv")}</a>
      </div>

      <section className="card">
        <div className="grid">
          <Stat label={t("import")} value={fmt(loc, tot.import_kwh, 1)} unit="kWh" cls="imp" sub={cmpLine("import_kwh")} />
          <Stat label={t("export")} value={fmt(loc, tot.export_kwh, 1)} unit="kWh" cls="exp" sub={cmpLine("export_kwh")} />
          <Stat label={t("net")} value={fmt(loc, tot.net_kwh, 1)} unit="kWh" sub={cmpLine("net_kwh")} />
          <Stat label={t("gas")} value={fmt(loc, tot.gas_m3, 2)} unit="m³" cls="gasc" sub={cmpLine("gas_m3")} />
        </div>
        <div className="muted small" style={{ marginTop: 8 }}>
          {t("t1")}: {fmt(loc, tot.import_t1_kwh, 1)} / {fmt(loc, tot.export_t1_kwh, 1)} kWh ·{" "}
          {t("t2")}: {fmt(loc, tot.import_t2_kwh, 1)} / {fmt(loc, tot.export_t2_kwh, 1)} kWh
        </div>
      </section>

      <section className="card">
        <h2>{t("import")} / {t("export")} (kWh)</h2>
        {data && !data.rows.length ? <p className="muted">{t("noData")}</p>
          : <EChart option={energyOption} className="chart tall" onEvents={barEvents} />}
      </section>

      {powerOption && (
        <section className="card">
          <div className="row"><h2 style={{ margin: 0 }}>{t("powerDetail")}</h2>
            <span className="muted small">({power.resolution}) · {t("zoomHint")}</span>
            <span className="spacer" />
            {zoom && <button className="btn" onClick={() => setZoom(null)}>↺</button>}
          </div>
          <EChart option={powerOption} onEvents={powerEvents} />
        </section>
      )}

      <section className="card">
        <h2>{t("gas")} (m³)</h2>
        {data && <EChart option={gasOption} />}
      </section>
    </div>
  );
}
