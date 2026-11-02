// Period helpers. Calendar boundaries (days, weeks, months, years) are computed
// in Europe/Amsterdam, the same calendar the backend uses for its daily
// buckets, whatever timezone the browser is in. The API receives UTC ISO.
export const TZ = "Europe/Amsterdam";

const dtf = new Intl.DateTimeFormat("en-US", {
  timeZone: TZ, hourCycle: "h23", weekday: "short",
  year: "numeric", month: "numeric", day: "numeric", hour: "numeric", minute: "numeric", second: "numeric",
});
const WD = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

/** Amsterdam calendar parts of an instant. m is 0-based, wd 0 = Monday. */
export function parts(date) {
  const o = {};
  for (const p of dtf.formatToParts(date)) o[p.type] = p.value;
  return { y: +o.year, m: +o.month - 1, d: +o.day, h: +o.hour % 24, mi: +o.minute, s: +o.second, wd: WD.indexOf(o.weekday) };
}

/** The instant of Amsterdam midnight on y-m-d (overflowing m/d roll over like Date.UTC). */
export function ams(y, m, d) {
  const target = Date.UTC(y, m, d);
  let t = target;
  for (let i = 0; i < 3; i++) {
    const p = parts(new Date(t));
    const offset = Date.UTC(p.y, p.m, p.d, p.h, p.mi, p.s) - t;
    t = target - offset;
  }
  return new Date(t);
}

export const iso = (d) => d.toISOString();

export const startOfDay = (d) => { const p = parts(d); return ams(p.y, p.m, p.d); };

// view -> { start(d), next(start, n), resolution }
export const VIEWS = {
  day: { start: startOfDay, next: (s, n) => { const p = parts(s); return ams(p.y, p.m, p.d + n); }, resolution: "1h" },
  week: {
    start: (d) => { const p = parts(d); return ams(p.y, p.m, p.d - p.wd); },
    next: (s, n) => { const p = parts(s); return ams(p.y, p.m, p.d + 7 * n); }, resolution: "1d",
  },
  month: {
    start: (d) => { const p = parts(d); return ams(p.y, p.m, 1); },
    next: (s, n) => { const p = parts(s); return ams(p.y, p.m + n, 1); }, resolution: "1d",
  },
  year: {
    start: (d) => ams(parts(d).y, 0, 1),
    next: (s, n) => ams(parts(s).y + n, 0, 1), resolution: "1mo",
  },
  all: { start: () => ams(2000, 0, 1), next: (s, n) => (n > 0 ? ams(2100, 0, 1) : s), resolution: "1y" },
};

export function period(view, anchor) {
  const v = VIEWS[view];
  const start = v.start(anchor);
  return { start, end: v.next(start, 1) };
}

export function sameLastYear({ start, end }) {
  const a = parts(start), b = parts(end);
  return { start: ams(a.y - 1, a.m, a.d), end: ams(b.y - 1, b.m, b.d) };
}
