/** The split view: which tabs are on screen at once, and where.
 *
 *  A tab bar answers "what is open"; it cannot answer "show me the diff and
 *  the preview at the same time". Dragging a tab onto the page splits it, and
 *  what comes out is a tree: every split has two children, and a child is
 *  either another split or a pane showing one tab. Nesting falls out of that
 *  — a column inside a row is a row whose second child is a column — so
 *  "가로의 세로분할" needs no special case.
 *
 *  Pure functions over the tree. Who is focused, what is persisted and what
 *  the router is showing all live in the tab store, which owns this. */

export type PaneNode =
  | { kind: "leaf"; id: string; href: string }
  | { kind: "split"; id: string; dir: "row" | "col"; ratio: number; children: [PaneNode, PaneNode] };

export type DropSide = "left" | "right" | "top" | "bottom" | "center";

let counter = 0;

/** Ids only have to be unique within one page's lifetime; a restored layout
 *  brings its own, and the counter is moved past them on restore. */
export function paneId(): string {
  counter += 1;
  return `p${counter}`;
}

export function adoptIds(node: PaneNode): void {
  const number = Number(node.id.replace(/^p/, ""));
  if (Number.isFinite(number) && number > counter) counter = number;
  if (node.kind === "split") node.children.forEach(adoptIds);
}

export function leaf(href: string): PaneNode {
  return { kind: "leaf", id: paneId(), href };
}

export function leaves(node: PaneNode): Extract<PaneNode, { kind: "leaf" }>[] {
  return node.kind === "leaf" ? [node] : node.children.flatMap(leaves);
}

export function findLeaf(node: PaneNode, id: string) {
  return leaves(node).find((pane) => pane.id === id) ?? null;
}

/** Rebuilds the tree with `id` replaced by whatever `make` returns. */
export function mapNode(
  node: PaneNode,
  id: string,
  make: (found: PaneNode) => PaneNode
): PaneNode {
  if (node.id === id) return make(node);
  if (node.kind === "leaf") return node;
  const [first, second] = node.children;
  const next: [PaneNode, PaneNode] = [mapNode(first, id, make), mapNode(second, id, make)];
  return next[0] === first && next[1] === second ? node : { ...node, children: next };
}

/** Splits the pane `id` in the direction the drop asked for, and says which
 *  pane is now showing the dropped tab so the caller can focus it. */
export function split(
  node: PaneNode,
  id: string,
  side: DropSide,
  href: string
): { layout: PaneNode; focus: string } {
  if (side === "center") {
    return { layout: mapNode(node, id, (found) => ({ ...found, kind: "leaf", href })), focus: id };
  }
  const added = leaf(href);
  const dir = side === "left" || side === "right" ? "row" : "col";
  const before = side === "left" || side === "top";
  const layout = mapNode(node, id, (found) => ({
    kind: "split",
    id: paneId(),
    dir,
    ratio: 0.5,
    children: before ? [added, found] : [found, added],
  }));
  return { layout, focus: added.id };
}

/** Removes a pane and collapses the split it was half of. Returns null when
 *  the pane was the last one: the caller decides what a view with nothing in
 *  it should show. */
export function remove(node: PaneNode, id: string): PaneNode | null {
  if (node.id === id) return null;
  if (node.kind === "leaf") return node;
  const [first, second] = node.children;
  if (first.id === id) return second;
  if (second.id === id) return first;
  const next: [PaneNode, PaneNode] = [
    remove(first, id) ?? first,
    remove(second, id) ?? second,
  ];
  return next[0] === first && next[1] === second ? node : { ...node, children: next };
}

/** Drops every pane showing `href` — what closing a tab means for the view.
 *  The last pane standing keeps the layout alive rather than emptying it. */
export function removeHref(node: PaneNode, href: string): PaneNode | null {
  let next: PaneNode | null = node;
  for (const pane of leaves(node)) {
    if (pane.href !== href || !next) continue;
    if (leaves(next).length === 1) return null;
    next = remove(next, pane.id);
  }
  return next;
}

/** Which region of a pane a pointer is over. The edges are a fifth of the
 *  box each, so the middle — "just show it here" — is the easy target. */
export function sideAt(x: number, y: number, box: DOMRect): DropSide {
  const horizontal = (x - box.left) / box.width;
  const vertical = (y - box.top) / box.height;
  const edge = Math.min(horizontal, 1 - horizontal, vertical, 1 - vertical);
  if (edge > 0.2) return "center";
  if (edge === horizontal) return "left";
  if (edge === 1 - horizontal) return "right";
  return edge === vertical ? "top" : "bottom";
}
