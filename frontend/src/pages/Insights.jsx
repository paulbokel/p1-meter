import { useEffect, useMemo, useState } from "react";
import { api, qs } from "../api.js";
import { fmt, useLocale, useT } from "../i18n.js";
import { TZ, ams, iso, parts } from "../time.js";
import EChart, { baseOption, cssVar } from "../components/EChart.jsx";
import { Stat } from "./Live.jsx";

export default function Insights() {
  const t = useT();
  const loc = useLocale();
  const [bl, setBl] = useState(null);
  const [proj, setProj] = useState(null);
  const [prices, setPrices] = useState(null);
  const [plan, setPlan] = useState(null);
  const [hours, setHours] = useState(2);
  const [deadline, setDeadline] = useState("");      // local HH:MM, next occurrence
  const [contiguous, setContiguous] = useState(true);

  useEffect(() => {
    api("/api/baseload?days=365").then(setBl).catch(() => {});
    api("/api/costs/projection").then(setProj).catch(() => {});
    const p = parts(new Date());
    const s = ams(p.y, p.m, p.d);
    const e = ams(p.y, p.m, p.d + 2);
    api(`/api/prices?${qs({ start: iso(s), end: iso(e) })}`).then(setPrices).catch(() => {});
  }, []);
  useEffect(() => {
    const q = { hours, contiguous };
    if (deadline) q.before = iso(nextLocal(deadline));
    api(`/api/prices/plan?${qs(q)}`).then(setPlan).catch(() => setPlan(null));
  }, [hours, deadline, contiguous]);

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
          markArea: plan?.available ? { itemStyle: { color: cssVar("--exp"), opacity: 0.15 },
            data: plan.slots.map(([s, e]) => [{ xAxis: Date.parse(s) }, { xAxis: Date.parse(e) }]) } : undefined },
        ...(hasAllIn ? [{ type: "line", step: "start", name: "All-in", showSymbol: false,
          data: prices.rows.map((r) => [Date.parse(r.ts), r.all_in]), lineStyle: { color: cssVar("--imp") } }] : []),
      ],
    };
  }, [prices, plan, loc]);

  const eur = (v) => (v == null ? "–" : `€ ${fmt(loc, v, 0)}`);
  const timeFmt = (s) => new Date(s).toLocaleString(loc, { weekday: "short", hour: "2-digit", minute: "2-digit", timeZone: TZ });

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
            <div className="row small">
              <span>{t("planner")}</span>
              <select className="in" value={hours} onChange={(e) => setHours(Number(e.target.value))}>
                {[0.5, 1, 1.5, 2, 3, 4, 6, 8].map((h) => <option key={h} value={h}>{fmt(loc, h, h % 1 ? 1 : 0)} {t("hours")}</option>)}
              </select>
              <label className="row">{t("readyBy")}
                <input className="in" type="time" value={deadline} onChange={(e) => setDeadline(e.target.value)} />
              </label>
              <label className="row"><input type="checkbox" checked={contiguous} onChange={(e) => setContiguous(e.target.checked)} />{t("oneBlock")}</label>
            </div>
            {plan && (plan.available ? (
              <p className="small" style={{ margin: "6px 0" }}>
                {plan.slots.map(([s, e], i) => (
                  <strong key={s}>{i > 0 && ", "}{timeFmt(s)} – {new Date(e).toLocaleTimeString(loc, { hour: "2-digit", minute: "2-digit", timeZone: TZ })}</strong>
                ))}{" "}
                <span className="muted">({t("avg")} € {fmt(loc, plan.avg_all_in ?? plan.avg_spot, 3)}/kWh{plan.avg_all_in ? "" : " " + t("spot")}
                  {plan.avg_spot_window ? ` · ${fmt(loc, 100 * (1 - plan.avg_spot / plan.avg_spot_window), 0)}% ${t("belowAvg")}` : ""})</span>
              </p>
            ) : <p className="small muted" style={{ margin: "6px 0" }}>{t("planUnavailable")}</p>)}
            <EChart option={priceOption} />
          </>
        ) : <p className="muted">{t("noPrices")}</p>}
      </section>
    </div>
  );
}

// The next moment the local (Amsterdam) clock shows HH:MM.
function nextLocal(hhmm) {
  const [h, m] = hhmm.split(":").map(Number);
  const now = new Date();
  const p = parts(now);
  for (let d = 0; d < 2; d++) {
    const t = new Date(ams(p.y, p.m, p.d + d).getTime() + (h * 60 + m) * 60000);
    if (t > now) return t;   // DST days are off by an hour at most; fine for a deadline
  }
  return new Date(now.getTime() + 86400000);
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
