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

/** "14:32" — the clock only, for a timestamp sitting next to the thing it
 *  belongs to. The date is in the tooltip; a conversation you are reading now
 *  does not need it repeated on every line. */
export function clockTime(value: string | number | Date): string {
  const at = new Date(value);
  return Number.isNaN(at.getTime())
    ? ""
    : at.toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit", hour12: false });
}

/** "1분 20초", "14초" — how long something took, said the way a person would. */
export function duration(ms: number): string {
  const seconds = Math.max(0, Math.round(ms / 1000));
  if (seconds < 60) return `${seconds}초`;
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return `${minutes}분 ${rest}초`;
}

/** Keep the folder name visible in narrow project rows. Full path goes in a tooltip. */
export function shortPath(path: string | null): string {
  if (!path) return "—";
  const parts = path.split("/").filter(Boolean);
  return parts.length ? `…/${parts[parts.length - 1]}` : path;
}
