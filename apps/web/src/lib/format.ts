/** Formatting helpers shared by the console pages.
 *
 *  Timestamps in an operator console are read for recency first ("was that
 *  before or after the thing I just did?") and for the exact instant second.
 *  A bare `toLocaleString()` gives only the second, so every page printed a
 *  long absolute string the reader had to diff in their head. */

const UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ["second", 60],
  ["minute", 60],
  ["hour", 24],
  ["day", 7],
  ["week", 4.35],
  ["month", 12],
  ["year", Number.POSITIVE_INFINITY],
];

const relative = new Intl.RelativeTimeFormat("ko", { numeric: "auto" });

/** "3분 전", "어제" — the recency reading. */
export function timeAgo(value: string | number | Date): string {
  const at = new Date(value);
  if (Number.isNaN(at.getTime())) return "";
  let delta = (at.getTime() - Date.now()) / 1000;
  for (const [unit, span] of UNITS) {
    if (Math.abs(delta) < span) return relative.format(Math.round(delta), unit);
    delta /= span;
  }
  return relative.format(Math.round(delta), "year");
}

/** The exact instant, for the tooltip behind the recency reading. */
export function timeExact(value: string | number | Date): string {
  const at = new Date(value);
  return Number.isNaN(at.getTime()) ? "" : at.toLocaleString("ko-KR");
}
