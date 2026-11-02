import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api.js";
import { fmt, useLocale, useT } from "../i18n.js";
import { TZ } from "../time.js";
import EChart, { baseOption, cssVar } from "../components/EChart.jsx";

const WINDOW_MS = 3600 * 1000;

// Fullscreen wall display (F-LIVE-05): open #/kiosk on a tablet logged in as a viewer.
export default function Kiosk() {
  const t = useT();
  const loc = useLocale();
  const [cur, setCur] = useState(null);
  const [today, setToday] = useState(null);
  const [cost, setCost] = useState(null);
  const [clock, setClock] = useState(new Date());
  const [series, setSeries] = useState([]);
  const buf = useRef([]);

  useEffect(() => {
    let es, closed = false, retry;
    const connect = () => {
      api("/api/live/recent?seconds=3600").then((d) => {
        if (closed) return;
        buf.current = d.rows.map((r) => [Date.parse(r[0]), r[1]]);
        setSeries(buf.current.slice());
        es = new EventSource("/api/live/stream");
        es.onmessage = (e) => {
          const r = JSON.parse(e.data);
          setCur(r);
          const ts = Date.parse(r.ts), b = buf.current;
          if (!b.length || ts > b[b.length - 1][0]) b.push([ts, r.power_w]);
          while (b.length && b[0][0] < ts - WINDOW_MS) b.shift();
          setSeries(b.slice());
        };
        es.onerror = () => { es.close(); retry = setTimeout(connect, 10000); }; // a wall display must recover alone
      }).catch(() => { retry = setTimeout(connect, 10000); });
    };
    connect();
    return () => { closed = true; es?.close(); clearTimeout(retry); };
  }, []);

  useEffect(() => {
    const load = () => {
      api("/api/today").then(setToday).catch(() => {});
      api("/api/costs/today").then(setCost).catch(() => {});
      setClock(new Date());
    };
    load();
    const a = setInterval(load, 30000), b = setInterval(() => setClock(new Date()), 1000);
    return () => { clearInterval(a); clearInterval(b); };
  }, []);

  // Wake lock keeps tablets from sleeping where supported.
  useEffect(() => {
    let lock;
    const get = () => navigator.wakeLock?.request("screen").then((l) => { lock = l; }).catch(() => {});
    get();
    const vis = () => document.visibilityState === "visible" && get();
    document.addEventListener("visibilitychange", vis);
    return () => { document.removeEventListener("visibilitychange", vis); lock?.release().catch(() => {}); };
  }, []);

  const option = useMemo(() => {
    if (!series.length) return null;
    const o = baseOption();
    return {
      ...o, legend: { show: false }, tooltip: { show: false }, grid: { ...o.grid, top: 8 },
      xAxis: { ...o.xAxis, type: "time", axisLabel: { ...o.xAxis.axisLabel, fontSize: 14 } },
      yAxis: { ...o.yAxis, type: "value", axisLabel: { ...o.yAxis.axisLabel, fontSize: 14, formatter: (v) => fmt(loc, v) } },
      series: [
        { type: "line", data: series.map(([x, v]) => [x, v == null ? null : Math.max(v, 0)]), showSymbol: false,
          lineStyle: { color: cssVar("--imp"), width: 2 }, areaStyle: { color: cssVar("--imp"), opacity: 0.15 } },
        { type: "line", data: series.map(([x, v]) => [x, v == null ? null : Math.min(v, 0)]), showSymbol: false,
          lineStyle: { color: cssVar("--exp"), width: 2 }, areaStyle: { color: cssVar("--exp"), opacity: 0.15 } },
      ],
    };
  }, [series, loc]);

  const p = cur?.power_w;
  const stale = cur && Date.now() - Date.parse(cur.ts) > 120000;
  const fs = () => (document.fullscreenElement ? document.exitFullscreen() : document.documentElement.requestFullscreen?.());
  return (
    <div className="kiosk" onDoubleClick={fs}>
      <div className="k-top">
        <span className="k-clock">{clock.toLocaleTimeString(loc, { hour: "2-digit", minute: "2-digit", timeZone: TZ })}</span>
        <span className="muted">{clock.toLocaleDateString(loc, { weekday: "long", day: "numeric", month: "long", timeZone: TZ })}</span>
        <span className="spacer" />
        {stale && <span className="err">{t("noRecentData")}</span>}
        <a className="btn" href="#/live">✕</a>
      </div>
      <div className={"k-power " + (p != null && p < 0 ? "exp" : "imp")}>
        <span className="k-label">{p != null && p < 0 ? t("exporting") : t("importing")}</span>
        <span className="k-value">{p == null ? "–" : fmt(loc, Math.abs(p))}<small> W</small></span>
      </div>
      <div className="k-stats">
        <div><span>{t("import")}</span><b className="imp">{fmt(loc, today?.import_kwh, 1)}<small> kWh</small></b></div>
        <div><span>{t("export")}</span><b className="exp">{fmt(loc, today?.export_kwh, 1)}<small> kWh</small></b></div>
        <div><span>{t("gas")}</span><b className="gasc">{fmt(loc, today?.gas_m3, 2)}<small> m³</small></b></div>
        {cost?.has_contract && <div><span>{t("costsToday")}</span><b>€ {fmt(loc, cost.total_eur, 2)}</b></div>}
      </div>
      <div className="k-chart">{option && <EChart option={option} className="chart k" />}</div>
    </div>
  );
}
