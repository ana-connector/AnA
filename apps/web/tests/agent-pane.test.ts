import assert from "node:assert/strict";
import { test } from "node:test";
import { agentPane, leaves, split, type Leaf, type PaneNode } from "../src/lib/panes.ts";

const first: Leaf = { kind: "leaf", id: "p1", tabs: ["/branch"], active: "/branch" };

test("the first worker opens in a new right pane", () => {
  const opened = split(first, first.id, "right", "/agents/branch/live-1/task");
  assert.equal(leaves(opened.layout as PaneNode).length, 2);
  assert.equal(agentPane(opened.layout as PaneNode, first.id).active, "/agents/branch/live-1/task");
});

test("a second worker uses the existing second pane", () => {
  const opened = split(first, first.id, "right", "/agents/branch/live-1/task");
  const layout = opened.layout as PaneNode;
  assert.equal(agentPane(layout, first.id).id, opened.focus);
  assert.equal(agentPane(layout, opened.focus).id, opened.focus);
});

test("more than two panes use the source pane", () => {
  const two = split(first, first.id, "right", "/agent-one").layout as PaneNode;
  const three = split(two, first.id, "bottom", "/notes").layout as PaneNode;
  assert.equal(leaves(three).length, 3);
  assert.equal(agentPane(three, first.id).id, first.id);
  assert.equal(agentPane(three, first.id, leaves(three)[2].id).id, leaves(three)[2].id);
});
