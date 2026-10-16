/**
 * Where the browser thinks the server is. One calculation, in one place.
 *
 * ANA is deployed split today — the UI on 3005, the API on 8100 — and is
 * meant to end up behind the daemon on a single origin. Both have to come out
 * of the same rule, or the HTTP base and the WebSocket base drift apart: they
 * already read different environment variables and derived their host in
 * different ways.
 *
 * The functions here are pure and take the page's location as an argument, so
 * every mode can be checked without a browser (`apps/web/tests/urls.test.ts`).
 * `api.ts` applies them to the real `window.location`.
 *
 * Next inlines `NEXT_PUBLIC_*` at build time, so an image built with an
 * absolute override carries that address wherever it is run. The derived
 * defaults are what let one image serve localhost and the LAN alike.
 */

/** The part of `window.location` these rules read. */
export type PageLocation = {
  protocol: string
  hostname: string
  origin: string
}

/** The `NEXT_PUBLIC_*` values that steer the calculation. */
export type AddressEnv = {
  /** An absolute API origin, optionally with a path prefix. Wins over all. */
  apiUrl?: string
  /** An absolute WebSocket URL, including its path. Wins for the socket. */
  wsUrl?: string
  /** The API port for the split deployment. Defaults to 8100. */
  apiPort?: string
  /** "same-origin" serves the API from the page's own origin. Anything else,
   *  including unset, keeps the split-port default. */
  mode?: string
}

export const DEFAULT_API_PORT = "8100"
export const CHAT_SOCKET_PATH = "/ws/chat"

function withoutTrailingSlash(url: string): string {
  return url.replace(/\/+$/, "")
}

/**
 * The origin (and any path prefix) every HTTP call hangs off, with no
 * trailing slash, so callers can always write `${apiBase()}/threads`.
 */
export function resolveApiBase(env: AddressEnv, location: PageLocation): string {
  if (env.apiUrl) return withoutTrailingSlash(env.apiUrl)
  if (env.mode === "same-origin") return withoutTrailingSlash(location.origin)
  // The page's hostname, never "localhost": a UI opened from a phone on the
  // LAN would then call the phone. The page's own port is deliberately not
  // reused — in the split deployment it is the UI's, not the API's.
  const base = new URL(`${location.protocol}//${location.hostname}`)
  base.port = env.apiPort || DEFAULT_API_PORT
  return withoutTrailingSlash(base.toString())
}

/**
 * The chat socket's full URL. Derived from the API base unless given one
 * outright, which is what keeps the two from disagreeing about the host.
 */
export function resolveSocketUrl(
  env: AddressEnv,
  location: PageLocation,
  path: string = CHAT_SOCKET_PATH
): string {
  if (env.wsUrl) return withoutTrailingSlash(env.wsUrl)
  const url = new URL(resolveApiBase(env, location) + path)
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:"
  return withoutTrailingSlash(url.toString())
}

/**
 * Adds a query parameter to a URL that may already carry one, which the
 * `workspace` parameter on the chat socket does.
 */
export function withQuery(url: string, params: Record<string, string>): string {
  const entries = Object.entries(params).filter(([, value]) => value !== "")
  if (entries.length === 0) return url
  const separator = url.includes("?") ? "&" : "?"
  const query = entries
    .map(([key, value]) => `${encodeURIComponent(key)}=${encodeURIComponent(value)}`)
    .join("&")
  return `${url}${separator}${query}`
}
