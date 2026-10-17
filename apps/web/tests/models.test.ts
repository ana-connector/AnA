/**
 * How a long model list is grouped for the picker.
 *
 * Run with `node --test apps/web/tests/models.test.ts` from the repository
 * root (Node strips the types itself; nothing to install).
 */
import assert from "node:assert/strict";
import test from "node:test";

import { groupByProvider, modelLabel, MODEL_PICKER_THRESHOLD } from "../src/lib/models.ts";

test("names are grouped by the segment before the first slash", () => {
  const groups = groupByProvider([
    "opencode/gpt-6-astra",
    "opencode-go/kimi-k3",
    "opencode/claude-sonnet-5-5",
  ]);

  assert.deepEqual(
    groups.map((group) => group.provider),
    // Insertion order, not alphabetical: the CLI's own order is the one that
    // already groups the names.
    ["opencode", "opencode-go"]
  );
  assert.deepEqual(groups[0].models, [
    "opencode/gpt-6-astra",
    "opencode/claude-sonnet-5-5",
  ]);
});

test("a model id may carry slashes of its own", () => {
  const groups = groupByProvider(["omniroute/tapie/claude-opus-5"]);

  assert.equal(groups.length, 1);
  assert.equal(groups[0].provider, "omniroute");
  // Only the first slash is a separator; the rest belongs to the model.
  assert.equal(modelLabel(groups[0].models[0], "omniroute"), "tapie/claude-opus-5");
});

test("a name without a provider keeps its own group", () => {
  const groups = groupByProvider(["opus", "fable"]);

  assert.deepEqual(groups, [{ provider: "", models: ["opus", "fable"] }]);
  assert.equal(modelLabel("opus", ""), "opus");
});

test("the threshold leaves a short list as a dropdown", () => {
  // The Claude aliases plus full ids: still a list someone can read at once.
  assert.ok(MODEL_PICKER_THRESHOLD >= 8);
});
