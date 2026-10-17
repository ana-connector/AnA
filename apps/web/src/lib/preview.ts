/** The browser tab: a page of your own, previewed next to the conversation.
 *
 *  An agent that just started a dev server has nowhere to show you the result,
 *  and "open http://localhost:3000 yourself" loses the conversation behind
 *  another window. A browser tab is an ordinary tab of ANA's whose content is
 *  an iframe, so the preview sits beside the work that produced it.
 *
 *  The address lives in the path (`/browser/<encoded url>`) rather than in a
 *  query, because a tab is identified by its path: two previews are two tabs. */

export const BROWSER_ROUTE = "/browser";

/** Hosts that mean "this machine". A link an agent writes to one of these is
 *  something it just started, so it opens in a tab instead of a new window. */
const LOOPBACK = new Set(["localhost", "127.0.0.1", "0.0.0.0", "[::1]", "::1"]);

export function parseUrl(value: string): URL | null {
  const text = value.trim();
  if (!text) return null;
  try {
    // A bare "localhost:3000" is what people type and paste; it parses as a
    // URL whose protocol is "localhost:", so the scheme is added first.
    const url = new URL(/^[a-z][a-z0-9+.-]*:\/\//i.test(text) ? text : `http://${text}`);
    return url.protocol === "http:" || url.protocol === "https:" ? url : null;
  } catch {
    return null;
  }
}

export function isLoopback(value: string): boolean {
  const url = parseUrl(value);
  return url !== null && LOOPBACK.has(url.hostname.toLowerCase());
}

/** Whether this address may be framed. Anything that is not http(s) is out:
 *  `javascript:` and `data:` in an iframe run in our own page. */
export function canPreview(value: string): boolean {
  return parseUrl(value) !== null;
}

/** ANA framing itself: the iframe would be another copy of this UI (tabs,
 *  browser chrome, …), which is never what a preview is for. */
export function isOwnOrigin(
  value: string,
  page: Pick<Location, "hostname" | "port" | "protocol"> = window.location
): boolean {
  const url = parseUrl(value);
  if (!url) return false;
  const pagePort = page.port || (page.protocol === "https:" ? "443" : "80");
  const urlPort = url.port || (url.protocol === "https:" ? "443" : "80");
  return (
    url.hostname.toLowerCase() === page.hostname.toLowerCase() && urlPort === pagePort
  );
}

/** The same address as the machine *looking* at it would reach.
 *
 *  An agent writes "localhost:3000" meaning the machine it is working on.
 *  Opened from another device — ANA on `http://192.168.0.33:3005` — that
 *  address means the phone or laptop doing the looking, which is running
 *  nothing, so the frame says "연결을 거부했습니다". When the page itself is
 *  not on a loopback address, that host is the one the server is at, so a
 *  loopback target is pointed there instead. */
export function forViewer(value: string, pageHostname?: string): string {
  const url = parseUrl(value);
  if (!url) return value;
  const host = pageHostname ?? (typeof window === "undefined" ? "" : window.location.hostname);
  if (!host || !LOOPBACK.has(url.hostname.toLowerCase()) || LOOPBACK.has(host.toLowerCase())) {
    return url.href;
  }
  url.hostname = host;
  return url.href;
}

export function browserHref(value: string): string {
  const url = parseUrl(value);
  return url ? `${BROWSER_ROUTE}/${encodeURIComponent(forViewer(url.href))}` : BROWSER_ROUTE;
}

/** The address back out of a `/browser/<encoded>` path segment. */
export function previewTarget(segment: string | undefined): string {
  if (!segment) return "";
  try {
    return decodeURIComponent(segment);
  } catch {
    return segment;
  }
}

/** Fallback tab name before (or when we cannot read) the framed page's
 *  `<title>`: host and port, which is how people refer to a dev server. The
 *  path is left out — it changes as you click around inside the frame. */
export function previewLabel(value: string): string {
  const url = parseUrl(value);
  if (!url) return "브라우저";
  return url.port ? `${url.hostname}:${url.port}` : url.hostname;
}
