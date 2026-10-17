"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
import { TabStrip } from "@/components/tab-bar";
import { useTabs } from "@/components/tabs";
import { paneSrc } from "@/hooks/use-pane-mode";
import { leaves, sideAt, type DropSide, type Leaf, type PaneNode } from "@/lib/panes";
import { cn } from "@/lib/utils";

/** What a tab carries while it is being dragged. A private type keeps a file
 *  or a link dragged in from elsewhere from being read as a tab. */
export const TAB_DRAG_TYPE = "application/x-ana-tab";
export const PANE_DRAG_TYPE = "application/x-ana-pane";

/** Where a drop would put the tab, drawn over the pane it is over. */
function DropHint({ side }: { side: DropSide }) {
  return (
    <div
      data-slot="pane-drop-hint"
      data-side={side}
      className={cn(
        "pointer-events-none absolute z-20 bg-primary/25 ring-1 ring-primary",
        side === "center" && "inset-0",
        side === "left" && "inset-y-0 left-0 w-1/2",
        side === "right" && "inset-y-0 right-0 w-1/2",
        side === "top" && "inset-x-0 top-0 h-1/2",
        side === "bottom" && "inset-x-0 bottom-0 h-1/2"
      )}
    />
  );
}

/** One pane: the page it shows, and the drop target it is while a tab is in
 *  the air. The focused pane shows the live page — the one the router is on
 *  — and every other pane shows the same app addressed as a pane. */
function Pane({
  node,
  focused,
  split,
  leading,
  actions,
  children,
}: {
  node: Leaf;
  focused: boolean;
  /** True when more than one pane is on screen — then this pane is iframe-only. */
  split: boolean;
  leading?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
}) {
  const { dropTab, focusPane } = useTabs();
  const [hint, setHint] = useState<DropSide | null>(null);
  const box = useRef<HTMLDivElement>(null);

  const sideOf = (event: React.DragEvent) => {
    const rect = box.current?.getBoundingClientRect();
    return rect ? sideAt(event.clientX, event.clientY, rect) : "center";
  };

  return (
    <div
      ref={box}
      data-slot="pane"
      data-pane={node.id}
      data-focused={focused}
      className="relative flex min-h-0 min-w-0 flex-1 flex-col"
      onMouseDown={() => focusPane(node.id)}
      onDragOver={(event) => {
        if (!event.dataTransfer.types.includes(TAB_DRAG_TYPE)) return;
        event.preventDefault();
        event.dataTransfer.dropEffect = "move";
        setHint(sideOf(event));
      }}
      onDragLeave={(event) => {
        // Only when the pointer has left the pane itself: moving over a child
        // fires this too, and the hint would flicker off on every row.
        if (!box.current?.contains(event.relatedTarget as Node)) setHint(null);
      }}
      onDrop={(event) => {
        const href = event.dataTransfer.getData(TAB_DRAG_TYPE);
        const from = event.dataTransfer.getData(PANE_DRAG_TYPE);
        setHint(null);
        // Cleared here as well as on dragend: a drop does not always produce
        // one, and a mark left behind leaves an invisible cover over every
        // pane that swallows the next click.
        document.body.removeAttribute("data-dragging-tab");
        if (!href) return;
        event.preventDefault();
        dropTab(node.id, sideOf(event), href, from);
      }}
    >
      {/* Every pane carries its own tabs. Splitting the screen is how a
          second strip comes about — there is no separate window-wide bar to
          disagree with it. */}
      <TabStrip pane={node} leading={leading} actions={actions} />

      {/* When the window is split, every pane is an iframe — including the
          focused one. Swapping the live router tree between panes remounted
          both sides on every cross-pane click. Frames stay mounted; only
          which one is visible changes. A single pane still uses the live
          router document (no iframe tax for the common case). */}
      <div className="relative flex min-h-0 min-w-0 flex-1">
        {node.tabs.map((href) => {
          const showFrame = split
            ? href === node.active
            : !focused && href === node.active;
          return (
            <iframe
              key={href}
              src={paneSrc(href)}
              title={href}
              data-slot="pane-frame"
              className={cn(
                "min-h-0 w-full flex-1 border-0 bg-background",
                !showFrame && "hidden"
              )}
            />
          );
        })}
        {focused && !split ? (
          <div className="absolute inset-0 flex min-h-0 min-w-0 flex-col overflow-hidden">
            {children}
          </div>
        ) : null}
        <div
          data-slot="pane-drop-cover"
          className="absolute inset-0 z-10 hidden [body[data-dragging-tab]_&]:block"
        />
      </div>
      {hint && <DropHint side={hint} />}
    </div>
  );
}

/** The divider between two panes, and the only way to change their sizes. */
function Divider({
  id,
  dir,
  container,
}: {
  id: string;
  dir: "row" | "col";
  container: React.RefObject<HTMLDivElement | null>;
}) {
  const { resizePane } = useTabs();
  return (
    <div
      data-slot="pane-divider"
      role="separator"
      aria-orientation={dir === "row" ? "vertical" : "horizontal"}
      onPointerDown={(event) => {
        event.preventDefault();
        const target = event.currentTarget;
        target.setPointerCapture(event.pointerId);
        const move = (move: PointerEvent) => {
          const rect = container.current?.getBoundingClientRect();
          if (!rect) return;
          resizePane(
            id,
            dir === "row"
              ? (move.clientX - rect.left) / rect.width
              : (move.clientY - rect.top) / rect.height
          );
        };
        const up = () => {
          target.releasePointerCapture(event.pointerId);
          target.removeEventListener("pointermove", move);
          target.removeEventListener("pointerup", up);
        };
        target.addEventListener("pointermove", move);
        target.addEventListener("pointerup", up);
      }}
      className={cn(
        "shrink-0 bg-border transition-colors hover:bg-primary/60",
        dir === "row" ? "w-1 cursor-col-resize" : "h-1 cursor-row-resize"
      )}
    />
  );
}

function Split({
  node,
  leading,
  actions,
  children,
}: {
  node: Extract<PaneNode, { kind: "split" }>;
  leading?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
}) {
  const container = useRef<HTMLDivElement>(null);
  const [first, second] = node.children;
  return (
    <div
      ref={container}
      data-slot="pane-split"
      data-direction={node.dir}
      className={cn("flex min-h-0 min-w-0 flex-1", node.dir === "row" ? "flex-row" : "flex-col")}
    >
      <div className="flex min-h-0 min-w-0" style={{ flex: `${node.ratio} 1 0%` }}>
        {/* The sidebar trigger belongs to the leftmost strip, wherever that
            ends up in the tree. */}
        <Branch node={first} leading={leading} actions={actions}>{children}</Branch>
      </div>
      <Divider id={node.id} dir={node.dir} container={container} />
      <div className="flex min-h-0 min-w-0" style={{ flex: `${1 - node.ratio} 1 0%` }}>
        <Branch node={second} actions={actions}>{children}</Branch>
      </div>
    </div>
  );
}

function Branch({
  node,
  leading,
  actions,
  children,
}: {
  node: PaneNode;
  leading?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
}) {
  const { focusedPane, panes } = useTabs();
  if (node.kind === "split") {
    return (
      <Split node={node} leading={leading} actions={actions}>
        {children}
      </Split>
    );
  }
  const focused = node.id === focusedPane;
  const split = leaves(panes).length > 1;
  return (
    <Pane
      node={node}
      focused={focused}
      split={split}
      leading={leading}
      actions={focused ? actions : undefined}
    >
      {children}
    </Pane>
  );
}

/** The page, or several of them side by side.
 *
 *  Each pane carries its own tab strip, so the window has no bar of its
 *  own: with nothing split there is one pane, and its strip is the header.
 *
 *  `children` is the page the router is on, and it is rendered in exactly one
 *  pane — the focused one. Everything else is the same app in an iframe,
 *  which is what makes a second page on screen cost one line here instead of
 *  a rewrite of every page into something mountable twice. */
export function PaneHost({
  leading,
  actions,
  children,
}: {
  leading?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
}) {
  const { panes } = useTabs();

  // The cover over each pane is up for as long as the body carries the mark,
  // so the mark has to come down even when the drag ends somewhere that has
  // no handler of ours — outside the window, on the sidebar, nowhere.
  useEffect(() => {
    const clear = () => document.body.removeAttribute("data-dragging-tab");
    window.addEventListener("dragend", clear);
    window.addEventListener("drop", clear);
    window.addEventListener("mouseup", clear);
    return () => {
      window.removeEventListener("dragend", clear);
      window.removeEventListener("drop", clear);
      window.removeEventListener("mouseup", clear);
      clear();
    };
  }, []);

  return (
    <div data-slot="pane-host" className="flex min-h-0 min-w-0 flex-1">
      <Branch node={panes} leading={leading} actions={actions}>
        {children}
      </Branch>
    </div>
  );
}
