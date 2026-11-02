// Period helpers. All period boundaries are local (browser) midnight, which for
// a Dutch household is Europe/Amsterdam; the API receives ISO with offset.
export const iso = (d) => {
  const off = -d.getTimezoneOffset();
  const pad = (n) => String(Math.floor(Math.abs(n))).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}${off >= 0 ? "+" : "-"}${pad(off / 60)}:${pad(off % 60)}`;
};

export const startOfDay = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate());
const startOfWeek = (d) => { const s = startOfDay(d); const dow = (s.getDay() + 6) % 7; s.setDate(s.getDate() - dow); return s; };

// view -> { start(d), next(start, n), resolution }
export const VIEWS = {
  day:   { start: startOfDay, next: (s, n) => new Date(s.getFullYear(), s.getMonth(), s.getDate() + n), resolution: "1h", fine: "15m" },
  week:  { start: startOfWeek, next: (s, n) => new Date(s.getFullYear(), s.getMonth(), s.getDate() + 7 * n), resolution: "1d" },
  month: { start: (d) => new Date(d.getFullYear(), d.getMonth(), 1), next: (s, n) => new Date(s.getFullYear(), s.getMonth() + n, 1), resolution: "1d" },
  year:  { start: (d) => new Date(d.getFullYear(), 0, 1), next: (s, n) => new Date(s.getFullYear() + n, 0, 1), resolution: "1mo" },
  all:   { start: () => new Date(2000, 0, 1), next: (s, n) => (n > 0 ? new Date(2100, 0, 1) : s), resolution: "1y" },
};

export function period(view, anchor) {
  const v = VIEWS[view];
  const start = v.start(anchor);
  return { start, end: v.next(start, 1) };
}

export function sameLastYear({ start, end }) {
  return { start: new Date(start.getFullYear() - 1, start.getMonth(), start.getDate()),
           end: new Date(end.getFullYear() - 1, end.getMonth(), end.getDate()) };
}
