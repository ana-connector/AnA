/**
 * The address rules, checked without a browser.
 *
 * Run with `node --test apps/web/tests/urls.test.ts` from the repository root
 * (Node strips the types itself; nothing to install).
 */

import assert from "node:assert/strict"
import { test } from "node:test"

import {
  resolveApiBase,
  resolveSocketUrl,
  withQuery,
  type PageLocation,
} from "../src/lib/urls.ts"

const page = (origin: string): PageLocation => {
  const url = new URL(origin)
  return { protocol: url.protocol, hostname: url.hostname, origin: url.origin }
}

const localhost = page("http://localhost:3005")
const lan = page("http://192.168.0.33:3005")
const secure = page("https://ana.example.com")

test("the split deployment keeps the page's host and the API's port", () => {
  assert.equal(resolveApiBase({}, localhost), "http://localhost:8100")
  assert.equal(resolveApiBase({}, lan), "http://192.168.0.33:8100")
  assert.equal(resolveSocketUrl({}, localhost), "ws://localhost:8100/ws/chat")
  assert.equal(resolveSocketUrl({}, lan), "ws://192.168.0.33:8100/ws/chat")
})

test("an https page gets https and wss", () => {
  assert.equal(resolveApiBase({}, secure), "https://ana.example.com:8100")
  assert.equal(resolveSocketUrl({}, secure), "wss://ana.example.com:8100/ws/chat")
})

test("the port is configurable and the socket follows it", () => {
  const env = { apiPort: "9000" }
  assert.equal(resolveApiBase(env, localhost), "http://localhost:9000")
  assert.equal(resolveSocketUrl(env, localhost), "ws://localhost:9000/ws/chat")
})

test("same-origin mode serves the API from the page itself", () => {
  const env = { mode: "same-origin" }
  assert.equal(resolveApiBase(env, localhost), "http://localhost:3005")
  assert.equal(resolveSocketUrl(env, localhost), "ws://localhost:3005/ws/chat")
  assert.equal(resolveApiBase(env, secure), "https://ana.example.com")
  assert.equal(resolveSocketUrl(env, secure), "wss://ana.example.com/ws/chat")
})

test("same-origin mode is opt-in: anything else keeps the split default", () => {
  assert.equal(resolveApiBase({ mode: "" }, localhost), "http://localhost:8100")
  assert.equal(resolveApiBase({ mode: "split" }, localhost), "http://localhost:8100")
})

test("an absolute override wins, with its trailing slash dropped", () => {
  assert.equal(
    resolveApiBase({ apiUrl: "https://api.example.com/" }, localhost),
    "https://api.example.com"
  )
  // The socket follows the overridden API host rather than the page's.
  assert.equal(
    resolveSocketUrl({ apiUrl: "https://api.example.com/" }, localhost),
    "wss://api.example.com/ws/chat"
  )
  assert.equal(
    resolveSocketUrl({ wsUrl: "wss://sockets.example.com/ws/chat" }, localhost),
    "wss://sockets.example.com/ws/chat"
  )
})

test("a path prefix on the override is kept", () => {
  const env = { apiUrl: "https://example.com/ana/" }
  assert.equal(resolveApiBase(env, localhost), "https://example.com/ana")
  assert.equal(resolveSocketUrl(env, localhost), "wss://example.com/ana/ws/chat")
})

test("an IPv6 page keeps its brackets", () => {
  const ipv6 = page("http://[::1]:3005")
  assert.equal(resolveApiBase({}, ipv6), "http://[::1]:8100")
  assert.equal(resolveSocketUrl({}, ipv6), "ws://[::1]:8100/ws/chat")
})

test("a query is appended whether or not the URL already has one", () => {
  assert.equal(
    withQuery("ws://localhost:8100/ws/chat", { workspace: "a b" }),
    "ws://localhost:8100/ws/chat?workspace=a%20b"
  )
  assert.equal(
    withQuery("ws://localhost:8100/ws/chat?x=1", { workspace: "w" }),
    "ws://localhost:8100/ws/chat?x=1&workspace=w"
  )
  assert.equal(withQuery("ws://localhost:8100/ws/chat", {}), "ws://localhost:8100/ws/chat")
  assert.equal(
    withQuery("ws://localhost:8100/ws/chat", { workspace: "" }),
    "ws://localhost:8100/ws/chat"
  )
})
