/**
 * Prompt ids, which have to work on an origin that is not a secure context.
 *
 * Run with `node --test apps/web/tests/ids.test.ts` from the repository root.
 */

import assert from "node:assert/strict"
import { test } from "node:test"

import { newId } from "../src/lib/ids.ts"

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/

test("an id is a version 4 uuid", () => {
  assert.match(newId(), UUID)
  assert.notEqual(newId(), newId())
})

test("it works where crypto.randomUUID does not exist", () => {
  // Plain HTTP on a LAN address is not a secure context, so randomUUID is
  // missing there — and every send used to throw instead of going out.
  const real = globalThis.crypto
  try {
    Object.defineProperty(globalThis, "crypto", {
      value: { getRandomValues: real.getRandomValues.bind(real) },
      configurable: true,
    })
    assert.match(newId(), UUID)
    assert.notEqual(newId(), newId())
  } finally {
    Object.defineProperty(globalThis, "crypto", { value: real, configurable: true })
  }
})

test("and where there is no crypto at all", () => {
  const real = globalThis.crypto
  try {
    Object.defineProperty(globalThis, "crypto", { value: undefined, configurable: true })
    assert.match(newId(), UUID)
  } finally {
    Object.defineProperty(globalThis, "crypto", { value: real, configurable: true })
  }
})
