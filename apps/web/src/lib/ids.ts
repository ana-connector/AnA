/** A unique id for one prompt, on any origin ANA is reachable from.
 *
 *  `crypto.randomUUID` only exists in a secure context, which plain HTTP on
 *  a LAN address is not — so opening ANA at `http://192.168.0.33:3005`
 *  instead of localhost left every send throwing "crypto.randomUUID is not a
 *  function", and the composer kept the text with no sign of why.
 *  `crypto.getRandomValues` has no such restriction, so the fallback is a
 *  version 4 UUID built from it. */
export function newId(): string {
  const source = globalThis.crypto;
  if (typeof source?.randomUUID === "function") return source.randomUUID();

  const bytes = new Uint8Array(16);
  if (typeof source?.getRandomValues === "function") {
    source.getRandomValues(bytes);
  } else {
    // No crypto at all: the id only has to be unique within one page's
    // conversation, and the server treats it as an opaque string.
    for (let index = 0; index < bytes.length; index += 1) {
      bytes[index] = Math.floor(Math.random() * 256);
    }
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40; // version 4
  bytes[8] = (bytes[8] & 0x3f) | 0x80; // variant 1
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
