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

/** Whether this address may be framed. Anything that is not http(s) is out —
 *  `javascript:` and `data:` in an iframe run in our own page. So is ANA
 *  itself: a frame of the same origin can reach out of its sandbox, and a
 *  browser tab showing the app it lives in is a hall of mirrors anyway. */
export function canPreview(value: string, ownOrigin?: string): boolean {
  const url = parseUrl(value);
  if (!url) return false;
  return !ownOrigin || url.origin !== ownOrigin;
}

export function browserHref(value: string): string {
  const url = parseUrl(value);
  return url ? `${BROWSER_ROUTE}/${encodeURIComponent(url.href)}` : BROWSER_ROUTE;
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

/** What the tab is called: the host and port, which is how people refer to a
 *  dev server. The path is left out — it changes as you click around inside
 *  the frame, and a tab whose name moves is hard to aim at. */
export function previewLabel(value: string): string {
  const url = parseUrl(value);
  if (!url) return "브라우저";
  return url.port ? `${url.hostname}:${url.port}` : url.hostname;
}
