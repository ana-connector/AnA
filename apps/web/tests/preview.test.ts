/**
 * The browser tab's address rules, checked without a browser.
 *
 * Run with `node --test apps/web/tests/preview.test.ts` from the repository
 * root (Node strips the types itself; nothing to install).
 */

import assert from "node:assert/strict"
import { test } from "node:test"

import {
  browserHref,
  canPreview,
  forViewer,
  isLoopback,
  parseUrl,
  previewLabel,
  previewTarget,
} from "../src/lib/preview.ts"

test("a bare host and port is an http address", () => {
  assert.equal(parseUrl("localhost:3000")?.href, "http://localhost:3000/")
  assert.equal(parseUrl("192.168.0.33:8520/path")?.href, "http://192.168.0.33:8520/path")
  assert.equal(parseUrl("https://example.com")?.href, "https://example.com/")
})

test("only http and https can be framed", () => {
  // A javascript: or data: frame runs in our own page.
  assert.equal(parseUrl("javascript:alert(1)"), null)
  assert.equal(parseUrl("data:text/html,<script>"), null)
  assert.equal(canPreview("javascript:alert(1)"), false)
  assert.equal(canPreview("localhost:3000"), true)
})

test("ANA's own address is allowed, because it is the user's own app", () => {
  assert.equal(canPreview("http://localhost:3005/settings"), true)
})

test("a loopback address is pointed at the machine serving the page", () => {
  // Opened from a phone on the LAN, "localhost" is the phone, which is
  // running nothing: the server is wherever the page itself came from.
  assert.equal(forViewer("http://localhost:8520/", "192.168.0.33"), "http://192.168.0.33:8520/")
  assert.equal(forViewer("http://127.0.0.1:8520/x", "192.168.0.33"), "http://192.168.0.33:8520/x")
  // Opened on the machine itself, nothing moves.
  assert.equal(forViewer("http://localhost:8520/", "localhost"), "http://localhost:8520/")
  assert.equal(forViewer("http://localhost:8520/", "127.0.0.1"), "http://localhost:8520/")
  // Somebody else's host is never rewritten.
  assert.equal(forViewer("https://example.com/", "192.168.0.33"), "https://example.com/")
})

test("the address survives the round trip through the path", () => {
  const href = browserHref("http://localhost:3000/a?b=c")
  assert.equal(href.startsWith("/browser/"), true)
  assert.equal(previewTarget(href.slice("/browser/".length)), "http://localhost:3000/a?b=c")
})

test("a tab is named after the host and port", () => {
  assert.equal(previewLabel("http://localhost:3000/deep/path"), "localhost:3000")
  assert.equal(previewLabel("https://example.com"), "example.com")
  // A bare word is a host, so only something that is not an address at all
  // falls back to the generic name.
  assert.equal(previewLabel("nonsense"), "nonsense")
  assert.equal(previewLabel("javascript:alert(1)"), "브라우저")
})

test("loopback is what an agent means by this machine", () => {
  assert.equal(isLoopback("http://127.0.0.1:8123/healthz"), true)
  assert.equal(isLoopback("http://[::1]:8123/"), true)
  assert.equal(isLoopback("https://example.com"), false)
})
