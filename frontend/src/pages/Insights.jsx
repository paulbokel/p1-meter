import { useEffect, useMemo, useState } from "react";
import { api, qs } from "../api.js";
import { fmt, useLocale, useT } from "../i18n.js";
import { iso, startOfDay } from "../time.js";
import EChart, { baseOption, cssVar } from "../components/EChart.jsx";
import { Stat } from "./Live.jsx";

export default function Insights() {
  const t = useT();
  const loc = useLocale();
  const [bl, setBl] = useState(null);
  const [proj, setProj] = useState(null);
  const [prices, setPrices] = useState(null);
  const [cheap, setCheap] = useState(null);
  const [hours, setHours] = useState(2);

  useEffect(() => {
    api("/api/baseload?days=365").then(setBl).catch(() => {});
    api("/api/costs/projection").then(setProj).catch(() => {});
    const s = startOfDay(new Date());
    const e = new Date(s.getFullYear(), s.getMonth(), s.getDate() + 2);
    api(`/api/prices?${qs({ start: iso(s), end: iso(e) })}`).then(setPrices).catch(() => {});
  }, []);
  useEffect(() => { api(`/api/prices/cheapest?hours=${hours}`).then(setCheap).catch(() => {}); }, [hours]);

  const blOption = useMemo(() => {
    if (!bl?.rows?.length) return null;
    const o = baseOption();
    return {
      ...o, legend: { show: false }, grid: { ...o.grid, top: 16 },
      tooltip: { ...o.tooltip, valueFormatter: (v) => `${fmt(loc, v)} W` },
      xAxis: { ...o.xAxis, type: "time" },
      yAxis: { ...o.yAxis, type: "value", min: 0, axisLabel: { ...o.yAxis.axisLabel, formatter: (v) => `${fmt(loc, v)} W` } },
      series: [{
        type: "line", name: t("baseload"), showSymbol: false,
        data: withGaps(bl.rows.map((r) => [Date.parse(r.day), r.watts]), 86400000), lineStyle: { color: cssVar("--accent"), width: 1.5 },
        areaStyle: { color: cssVar("--accent"), opacity: 0.08 },
        markLine: {
          symbol: "none", lineStyle: { color: cssVar("--warn"), type: "dashed" },
          label: { formatter: (p) => `${t("stepChange")} ${p.data.txt}`, color: cssVar("--muted"), fontSize: 11 },
          data: bl.steps.map((s) => ({ xAxis: Date.parse(s.day), txt: `${s.after_w > s.before_w ? "+" : ""}${fmt(loc, s.after_w - s.before_w)} W` })),
        },
      }],
    };
  }, [bl, loc, t]);

  const priceOption = useMemo(() => {
    if (!prices?.rows?.length) return null;
    const o = baseOption();
    const hasAllIn = prices.rows.some((r) => r.all_in != null);
    const now = Date.now();
    return {
      ...o, legend: { show: hasAllIn, top: 0, textStyle: o.legend.textStyle },
      tooltip: { ...o.tooltip, valueFormatter: (v) => `€ ${fmt(loc, v, 3)}` },
      xAxis: { ...o.xAxis, type: "time" },
      yAxis: { ...o.yAxis, type: "value", axisLabel: { ...o.yAxis.axisLabel, formatter: (v) => `€${fmt(loc, v, 2)}` } },
      series: [
        { type: "line", step: "start", name: "Day-ahead", showSymbol: false, data: prices.rows.map((r) => [Date.parse(r.ts), r.spot]),
          lineStyle: { color: cssVar("--muted") },
          markLine: { symbol: "none", silent: true, data: [{ xAxis: now }], lineStyle: { color: cssVar("--accent") }, label: { show: false } },
          markArea: cheap?.block ? { itemStyle: { color: cssVar("--exp"), opacity: 0.12 },
            data: [[{ xAxis: Date.parse(cheap.block.start) }, { xAxis: Date.parse(cheap.block.end) }]] } : undefined },
        ...(hasAllIn ? [{ type: "line", step: "start", name: "All-in", showSymbol: false,
          data: prices.rows.map((r) => [Date.parse(r.ts), r.all_in]), lineStyle: { color: cssVar("--imp") } }] : []),
      ],
    };
  }, [prices, cheap, loc]);

  const eur = (v) => (v == null ? "–" : `€ ${fmt(loc, v, 0)}`);
  const timeFmt = (s) => new Date(s).toLocaleString(loc, { weekday: "short", hour: "2-digit", minute: "2-digit" });

  return (
    <div className="stack">
      <section className="card">
        <h2>{t("baseload")}</h2>
        <div className="grid">
          <Stat label={t("now")} value={fmt(loc, bl?.current_w)} unit="W" />
          <Stat label={t("perYear")} value={fmt(loc, bl?.kwh_year)} unit="kWh" />
          <Stat label={t("costs") + " " + t("perYear")} value={eur(bl?.eur_year)} unit="" />
          <Stat label={t("shareImport")} value={bl?.share_of_import == null ? "–" : fmt(loc, bl.share_of_import * 100)} unit="%" />
        </div>
        {blOption && <><h2 style={{ marginTop: 14 }}>{t("trend")}</h2><EChart option={blOption} /></>}
      </section>

      {proj && (
        <section className="card">
          <h2>{t("projection")} {proj.year}</h2>
          <div className="grid">
            <Stat label={t("ytd")} value={eur(proj.ytd_eur)} unit="" />
            <Stat label={t("projected")} value={eur(proj.projected_eur)} unit="" />
            <Stat label={t("advance")} value={eur(proj.advance_year_eur)} unit="" />
            {proj.difference_eur != null && (
              <Stat label={proj.difference_eur >= 0 ? t("expectedRefund") : t("expectedExtra")}
                    value={eur(Math.abs(proj.difference_eur))} unit="" cls={proj.difference_eur >= 0 ? "exp" : "imp"} />
            )}
          </div>
        </section>
      )}

      <section className="card">
        <h2>{t("prices")}</h2>
        {priceOption ? (
          <>
            {cheap?.block && (
              <div className="row small">
                <span>{t("cheapest")}</span>
                <select className="in" value={hours} onChange={(e) => setHours(Number(e.target.value))}>
                  {[1, 2, 3, 4, 6].map((h) => <option key={h} value={h}>{h} {t("hours")}</option>)}
                </select>
                <strong>{timeFmt(cheap.block.start)} – {new Date(cheap.block.end).toLocaleTimeString(loc, { hour: "2-digit", minute: "2-digit" })}</strong>
                <span className="muted">({t("avg")} € {fmt(loc, cheap.block.avg_price, 3)}/kWh)</span>
              </div>
            )}
            <EChart option={priceOption} />
          </>
        ) : <p className="muted">{t("noPrices")}</p>}
      </section>
    </div>
  );
}

// Insert a null where consecutive points are further apart than `step`, so lines break at gaps.
function withGaps(points, step) {
  const out = [];
  for (const p of points) {
    if (out.length && p[0] - out[out.length - 1][0] > step * 1.5) out.push([out[out.length - 1][0] + step, null]);
    out.push(p);
  }
  return out;
}
