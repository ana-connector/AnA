/**
 * How a sized preview is scaled into the pane it has.
 *
 * Run with `node --test apps/web/tests/fit.test.ts` from the repository root
 * (Node strips the types itself; nothing to install).
 */
import assert from "node:assert/strict";
import test from "node:test";

import { fitScale, scaleLabel } from "../src/lib/fit.ts";

test("a frame that fits is drawn at its own size", () => {
  assert.equal(fitScale([390, 844], { width: 900, height: 1000 }), 1);
  // Room to spare does not blow the frame up: the size is the question.
  assert.equal(fitScale([390, 844], { width: 4000, height: 4000 }), 1);
  assert.equal(scaleLabel(1), null);
});

test("the axis that runs out of room first sets the scale", () => {
  // Too wide: 640/1280.
  assert.equal(fitScale([1280, 800], { width: 640, height: 4000 }), 0.5);
  // Too tall: 400/800.
  assert.equal(fitScale([1280, 800], { width: 4000, height: 400 }), 0.5);
  // Both: the smaller one wins, so nothing is cut off.
  assert.equal(fitScale([1280, 800], { width: 640, height: 200 }), 0.25);
});

test("an unmeasured pane leaves the frame alone", () => {
  // First paint, before the ResizeObserver has said anything.
  assert.equal(fitScale([1280, 800], { width: 0, height: 0 }), 1);
  assert.equal(fitScale([1280, 800], { width: Number.NaN, height: Number.NaN }), 1);
  // And a size the inputs cannot produce does not divide by zero.
  assert.equal(fitScale([0, 0], { width: 500, height: 500 }), 1);
});

test("the label rounds and disappears near full size", () => {
  assert.equal(scaleLabel(0.5), "50%");
  assert.equal(scaleLabel(0.7234), "72%");
  // 99.6% is not worth a number on screen.
  assert.equal(scaleLabel(0.996), null);
});
