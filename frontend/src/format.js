export const fmtInt = (n) => (n ?? 0).toLocaleString("en-US");

export const fmtPct = (n, digits = 0) => (n == null ? "–" : `${n.toFixed(digits)}%`);

export function fmtChange(pct) {
  if (pct == null) return "NEW";
  const sign = pct > 0 ? "+" : "";
  return `${sign}${pct.toFixed(0)}%`;
}

export function changeClass(pct) {
  if (pct == null) return "chg-new";
  if (pct > 0) return "chg-up";
  if (pct < 0) return "chg-down";
  return "";
}

export function fmtWeek(iso) {
  const start = new Date(`${iso}T00:00:00Z`);
  const end = new Date(start.getTime() + 6 * 86400000);
  const o = { month: "short", day: "numeric", timeZone: "UTC" };
  return `${start.toLocaleDateString("en-US", o)} – ${end.toLocaleDateString("en-US", { ...o, year: "numeric" })}`;
}
