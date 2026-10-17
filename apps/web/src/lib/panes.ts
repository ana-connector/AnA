/** The split view: which tabs are on screen at once, and where.
 *
 *  A window holds a tree. Every split has two children, and a child is
 *  either another split or a pane. Nesting falls out of that — a column
 *  inside a row is a row whose second child is a column — so "가로의
 *  세로분할" needs no special case.
 *
 *  A pane owns tabs, not one page: splitting the screen gives you a second
 *  tab strip, and dragging a tab from one strip to the other moves it. That
 *  is why a leaf carries a list and which of them it is showing.
 *
 *  Pure functions over the tree. Who is focused, what each tab is called and
 *  what the router is showing all live in the tab store, which owns this. */

export type Leaf = { kind: "leaf"; id: string; tabs: string[]; active: string };
export type PaneNode =
  | Leaf
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

export function leaf(href: string, more: string[] = []): Leaf {
  const tabs = [href, ...more.filter((other) => other !== href)];
  return { kind: "leaf", id: paneId(), tabs, active: href };
}

export function leaves(node: PaneNode): Leaf[] {
  return node.kind === "leaf" ? [node] : node.children.flatMap(leaves);
}

export function findLeaf(node: PaneNode, id: string): Leaf | null {
  return leaves(node).find((pane) => pane.id === id) ?? null;
}

/** Every page on screen, in the order the panes are laid out. */
export function shownHrefs(node: PaneNode): string[] {
  return leaves(node).map((pane) => pane.active);
}

/** Rebuilds the tree with `id` replaced by whatever `make` returns. */
export function mapNode(node: PaneNode, id: string, make: (found: PaneNode) => PaneNode): PaneNode {
  if (node.id === id) return make(node);
  if (node.kind === "leaf") return node;
  const [first, second] = node.children;
  const next: [PaneNode, PaneNode] = [mapNode(first, id, make), mapNode(second, id, make)];
  return next[0] === first && next[1] === second ? node : { ...node, children: next };
}

/** The same leaf with one tab added (or raised, if it is already there). */
export function withTab(pane: Leaf, href: string): Leaf {
  return {
    ...pane,
    tabs: pane.tabs.includes(href) ? pane.tabs : [...pane.tabs, href],
    active: href,
  };
}

/** The same leaf with one tab gone, or null when that was its last one — a
 *  pane with no tabs is not a pane, it is a hole. */
export function withoutTab(pane: Leaf, href: string): Leaf | null {
  if (!pane.tabs.includes(href)) return pane;
  const tabs = pane.tabs.filter((tab) => tab !== href);
  if (!tabs.length) return null;
  if (pane.active !== href) return { ...pane, tabs };
  // Closing the tab you are reading moves to its right, the way every editor
  // does it, and to its left when it was the last one on that side.
  const index = pane.tabs.indexOf(href);
  return { ...pane, tabs, active: tabs[index] ?? tabs[index - 1] };
}

/** Splits the pane `id` in the direction the drop asked for, and says which
 *  pane now holds the tab so the caller can focus it. When the source pane
 *  has no other tab, splitting that pane keeps its original view in place. */
export function split(
  node: PaneNode,
  id: string,
  side: DropSide,
  href: string,
  from?: string
): { layout: PaneNode | null; focus: string } {
  const target = findLeaf(node, id);
  if (!target) return { layout: node, focus: id };

  if (side === "center") {
    const moved = mapNode(node, id, (found) => withTab(found as Leaf, href));
    return { layout: detach(moved, href, id, from), focus: id };
  }

  const added = leaf(href);
  const dir = side === "left" || side === "right" ? "row" : "col";
  const before = side === "left" || side === "top";
  const grown = mapNode(node, id, (found) => ({
    kind: "split",
    id: paneId(),
    dir,
    ratio: 0.5,
    children: before ? [added, found] : [found, added],
  }));
  const copySoleTab = from === id && target.tabs.length === 1;
  return { layout: copySoleTab ? grown : detach(grown, href, added.id, from), focus: added.id };
}

/** Takes `href` out of every pane but `keep`. Used after a move, so the tab
 *  is not left behind in the strip it was dragged out of. */
function detach(node: PaneNode, href: string, keep: string, from?: string): PaneNode | null {
  let next: PaneNode | null = node;
  for (const pane of leaves(node)) {
    if (pane.id === keep || !next) continue;
    if (from && pane.id !== from) continue;
    if (!pane.tabs.includes(href)) continue;
    next = closeTab(next, pane.id, href);
  }
  return next;
}

/** Closes one tab in one pane, dropping the pane when it was its last. */
export function closeTab(node: PaneNode, paneId: string, href: string): PaneNode | null {
  const pane = findLeaf(node, paneId);
  if (!pane) return node;
  const left = withoutTab(pane, href);
  if (left) return mapNode(node, paneId, () => left);
  return remove(node, paneId);
}

/** Removes a pane and collapses the split it was half of. Returns null when
 *  the pane was the last one: the caller decides what an empty view shows. */
export function remove(node: PaneNode, id: string): PaneNode | null {
  if (node.id === id) return null;
  if (node.kind === "leaf") return node;
  const [first, second] = node.children;
  if (first.id === id) return second;
  if (second.id === id) return first;
  const next: [PaneNode, PaneNode] = [remove(first, id) ?? first, remove(second, id) ?? second];
  return next[0] === first && next[1] === second ? node : { ...node, children: next };
}

/** Drops a page from every pane holding it — what closing a tab means for
 *  the view when the pane it was closed from is not the only one. */
export function removeHref(node: PaneNode, href: string): PaneNode | null {
  let next: PaneNode | null = node;
  for (const pane of leaves(node)) {
    if (!next || !pane.tabs.includes(href)) continue;
    next = closeTab(next, pane.id, href);
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
