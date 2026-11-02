import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api.js";
import { fmt, useLocale, useT } from "../i18n.js";
import EChart, { baseOption, cssVar } from "../components/EChart.jsx";

const WINDOW_MS = 3600 * 1000;

export default function Live() {
  const t = useT();
  const loc = useLocale();
  const [cur, setCur] = useState(null);
  const [today, setToday] = useState(null);
  const [series, setSeries] = useState([]); // [[ms, W]]
  const buf = useRef([]);

  // Rolling hour: load history once, then append from the live stream.
  useEffect(() => {
    let es, closed = false;
    api("/api/live/recent?seconds=3600").then((d) => {
      if (closed) return;
      buf.current = d.rows.map((r) => [Date.parse(r[0]), r[1]]);
      setSeries(buf.current.slice());
      es = new EventSource("/api/live/stream");
      es.onmessage = (e) => {
        const r = JSON.parse(e.data);
        setCur(r);
        const ts = Date.parse(r.ts);
        const b = buf.current;
        if (!b.length || ts > b[b.length - 1][0]) b.push([ts, r.power_w]);
        while (b.length && b[0][0] < ts - WINDOW_MS) b.shift();
        setSeries(b.slice());
      };
    }).catch(() => {});
    return () => { closed = true; es?.close(); };
  }, []);

  useEffect(() => {
    const load = () => api("/api/today").then(setToday).catch(() => {});
    load();
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, []);

  const option = useMemo(() => {
    if (!series.length) return null;
    const o = baseOption();
    const imp = cssVar("--imp"), exp = cssVar("--exp");
    return {
      ...o,
      legend: { show: false },
      tooltip: { ...o.tooltip, valueFormatter: (v) => `${fmt(loc, Math.abs(v))} W` },
      xAxis: { ...o.xAxis, type: "time" },
      yAxis: { ...o.yAxis, type: "value", axisLabel: { ...o.yAxis.axisLabel, formatter: (v) => `${fmt(loc, v)} W` } },
      // Import above zero and export below zero as two areas. A visualMap
      // colour split crashes ECharts 6 (getVisualGradient) on this series.
      series: [
        { type: "line", name: t("import"), data: series.map(([x, v]) => [x, v == null ? null : Math.max(v, 0)]),
          showSymbol: false, sampling: "lttb", lineStyle: { width: 1.2, color: imp }, areaStyle: { opacity: 0.15, color: imp } },
        { type: "line", name: t("export"), data: series.map(([x, v]) => [x, v == null ? null : Math.min(v, 0)]),
          showSymbol: false, sampling: "lttb", lineStyle: { width: 1.2, color: exp }, areaStyle: { opacity: 0.15, color: exp } },
      ],
    };
  }, [series, loc, t]);

  const p = cur?.power_w;
  const exporting = p != null && p < 0;
  return (
    <div className="stack">
      <div className="grid">
        <section className="card">
          <h2>{t("now")}</h2>
          <div className={"big " + (exporting ? "exp" : "imp")}>{fmt(loc, p == null ? null : Math.abs(p))} W</div>
          <div className="muted">{p == null ? "…" : exporting ? t("exporting") : t("importing")}
            {cur && <> · {t("tariff")} {cur.active_tariff}</>}</div>
        </section>
        <section className="card">
          <h2>{t("phases")}</h2>
          <div className="phases">
            {[1, 2, 3].map((i) => {
              const w = cur?.[`power_l${i}_w`];
              return (
                <div className="phase" key={i}>
                  <div className="muted small">L{i}</div>
                  <div className={"w num " + (w < 0 ? "exp" : "")}>{fmt(loc, w)} W</div>
                  <div className="muted small num">{fmt(loc, cur?.[`voltage_l${i}_v`], 1)} V</div>
                  <div className="muted small num">{fmt(loc, cur?.[`current_l${i}_a`], 1)} A</div>
                </div>
              );
            })}
          </div>
        </section>
      </div>
      <section className="card">
        <h2>{t("today")}</h2>
        <div className="grid">
          <Stat label={t("import")} value={fmt(loc, today?.import_kwh, 2)} unit="kWh" cls="imp" />
          <Stat label={t("export")} value={fmt(loc, today?.export_kwh, 2)} unit="kWh" cls="exp" />
          <Stat label={t("net")} value={fmt(loc, today?.net_kwh, 2)} unit="kWh" />
          <Stat label={t("gas")} value={fmt(loc, today?.gas_m3, 3)} unit="m³" cls="gasc" />
        </div>
      </section>
      <section className="card">
        <h2>{t("lastHour")}</h2>
        {option ? <EChart option={option} /> : <p className="muted">{t("noData")}</p>}
      </section>
    </div>
  );
}

export function Stat({ label, value, unit, cls = "", sub }) {
  return (
    <div className="stat">
      <div className="muted small">{label}</div>
      <div className={"v " + cls}>{value}<span className="u">{unit}</span></div>
      {sub && <div className="small">{sub}</div>}
    </div>
  );
}
