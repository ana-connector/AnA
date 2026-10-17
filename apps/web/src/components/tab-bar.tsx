"use client";

import { useEffect, useRef, type ReactNode } from "react";
import Link from "next/link";
import {
  ClockIcon,
  GlobeIcon,
  HomeIcon,
  LockIcon,
  PlusIcon,
  Settings2Icon,
  TerminalIcon,
  XIcon,
  type LucideIcon,
} from "lucide-react";
import { useTabs, type Tab } from "@/components/tabs";
import { PANE_DRAG_TYPE, TAB_DRAG_TYPE } from "@/components/pane-host";
import { WorkspaceIcon } from "@/components/workspace-icon";
import { Separator } from "@/components/ui/separator";
import { BROWSER_ROUTE } from "@/lib/preview";
import type { Leaf } from "@/lib/panes";
import { cn } from "@/lib/utils";

/** The installation's own pages have no workspace icon to show, and a tab
 *  with nothing in front of its name reads as a list row rather than a tab. */
const ROUTE_ICONS: Record<string, LucideIcon> = {
  "/": HomeIcon,
  "/settings": Settings2Icon,
  "/tools": TerminalIcon,
  "/secrets": LockIcon,
  "/automation": ClockIcon,
};

function iconFor(tab: Tab) {
  if (tab.icon) return <WorkspaceIcon icon={tab.icon} className="self-center" />;
  const Route =
    ROUTE_ICONS[tab.href] ?? (tab.href.startsWith(BROWSER_ROUTE) ? GlobeIcon : undefined);
  return Route ? <Route className="size-4 shrink-0 self-center" strokeWidth={1.5} /> : null;
}

/** One pane's tabs.
 *
 *  A strip belongs to a pane, not to the window: splitting the screen gives
 *  the new half its own strip, and a tab dragged across moves between them.
 *  With nothing split there is one pane, so its strip is the window's own
 *  header — which is why the sidebar trigger and the page's status sit in
 *  it rather than in a row of their own.
 */
export function TabStrip({
  pane,
  leading,
  actions,
  className,
}: {
  pane: Leaf;
  /** Shown before the tabs. The sidebar trigger, in the leftmost strip. */
  leading?: ReactNode;
  /** Small status that belongs beside the tabs: whether the socket is up,
   *  which CLI answered. Not buttons. */
  actions?: ReactNode;
  className?: string;
}) {
  const { tabs, activeHref, focusedPane, close, open, selectTab } = useTabs();
  const here = pane.id === focusedPane;
  const activeTab = useRef<HTMLDivElement | null>(null);

  // On a narrow window the strip scrolls, and the tab you just opened can be
  // the one off the end of it: switching tabs would then look like nothing
  // happened at all. After a frame, because a tab is named by the page
  // inside it — the width this measures is not final until that name is.
  //
  // A narrowing window needs the same treatment: the strip keeps the scroll
  // offset it had when it was wide, which can leave the active tab half off
  // the end of it.
  useEffect(() => {
    let frame = 0;
    const reveal = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        activeTab.current?.scrollIntoView({ block: "nearest", inline: "nearest" });
      });
    };
    reveal();
    window.addEventListener("resize", reveal);
    return () => {
      cancelAnimationFrame(frame);
      window.removeEventListener("resize", reveal);
    };
  }, [activeHref, tabs, pane.tabs, pane.active]);

  return (
    // The strip is darker than the page and the open tab is the page's own
    // colour, which is what makes it read as the sheet in front: tabs drawn
    // on the same background as their content are only underlined words.
    <header
      data-slot="tab-strip"
      data-pane={pane.id}
      className={cn(
        "flex h-9 shrink-0 items-stretch border-b border-border bg-muted/40 pr-2",
        className
      )}
    >
      {leading ? (
        <div className="flex shrink-0 items-center gap-2 pr-2 pl-3">
          {leading}
          <Separator orientation="vertical" className="h-4" />
        </div>
      ) : null}

      {/* Links rather than a tablist: each one is a page with its own
          address, so the browser's own middle-click, back button and
          open-in-new-window all keep working. */}
      <nav
        aria-label="열린 탭"
        data-slot="tab-bar"
        className="no-scrollbar flex min-w-0 flex-1 items-stretch overflow-x-auto"
      >
        {pane.tabs.map((href) => {
          const tab = tabs.find((item) => item.href === href) ?? { href, title: href };
          // Marked in its own strip; the strip of the pane you are not in
          // shows which page it is on, without claiming to be the one you
          // are reading.
          const current = href === pane.active;
          const active = current && here;
          return (
            <div
              key={href}
              ref={active ? activeTab : undefined}
              data-slot="tab"
              data-active={active}
              data-current={current}
              data-href={href}
              // Dragged onto a pane, a tab moves there — onto an edge it
              // splits it first. The body is marked for the length of the
              // drag: a pane's page can be an iframe, and an iframe swallows
              // the events that would otherwise reach the pane, so a cover
              // goes over it while the mark is there.
              draggable
              onDragStart={(event) => {
                event.dataTransfer.setData(TAB_DRAG_TYPE, href);
                event.dataTransfer.setData(PANE_DRAG_TYPE, pane.id);
                event.dataTransfer.effectAllowed = "move";
                document.body.setAttribute("data-dragging-tab", href);
              }}
              onDragEnd={() => document.body.removeAttribute("data-dragging-tab")}
              onAuxClick={(event) => {
                // Middle click closes, the way it does in every browser.
                if (event.button !== 1) return;
                event.preventDefault();
                close(href, pane.id);
              }}
              className={cn(
                // As wide as what is on it and no wider, up to a ceiling: a
                // tab that stretches to fill the strip is a toolbar button,
                // and one that spans a wide window on its own looks broken.
                // Past the ceiling the name is cut; past the floor the strip
                // scrolls instead.
                "group/tab relative flex min-w-28 max-w-56 shrink items-center gap-2",
                "border-r border-border/60 pr-1 pl-3 text-sm transition-colors",
                current
                  ? "bg-background text-foreground"
                  : "text-muted-foreground hover:bg-background/60 hover:text-foreground",
                // The line sits along the bottom, on top of the strip's own
                // border, so it reads as the edge the page hangs from. Only
                // in the pane you are in: two underlined tabs would be two
                // answers to "where am I".
                active &&
                  "after:absolute after:inset-x-0 after:bottom-0 after:h-0.5 after:bg-primary"
              )}
            >
              <Link
                href={href}
                aria-current={active ? "page" : undefined}
                title={tab.detail ? `${tab.title}\n${tab.detail}` : tab.title}
                onClick={(event) => {
                  if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return;
                  event.preventDefault();
                  selectTab(pane.id, href);
                }}
                className="flex min-w-0 flex-1 items-baseline gap-1.5 py-1.5 outline-none focus-visible:underline"
              >
                {iconFor(tab)}
                <span data-slot="tab-title" className="min-w-0 flex-1 truncate">
                  {tab.title}
                </span>
                {tab.detail ? (
                  // The folder used to sit at the far right of the bar,
                  // where it belonged to whichever tab happened to be open.
                  // It belongs to the tab: two workspaces are often called
                  // the same thing in different trees. Capped rather than
                  // left to shrink with the name: when both are too long,
                  // the name is the one worth reading.
                  <span
                    data-slot="tab-detail"
                    className="max-w-[45%] shrink-0 truncate font-mono text-[11px] text-muted-foreground"
                  >
                    {tab.detail}
                  </span>
                ) : null}
              </Link>
              <button
                type="button"
                data-slot="tab-close"
                aria-label={`${tab.title} 탭 닫기`}
                onClick={(event) => {
                  event.preventDefault();
                  event.stopPropagation();
                  close(href, pane.id);
                }}
                // Always there on the tab this pane is showing, on hover or
                // keyboard focus elsewhere: a row of permanent crosses reads
                // as clutter, and one that only appears on hover cannot be
                // reached by keyboard.
                className={cn(
                  "flex size-5 shrink-0 items-center justify-center rounded-sm text-muted-foreground opacity-0 hover:bg-muted hover:text-foreground focus-visible:opacity-100 group-hover/tab:opacity-100",
                  current && "opacity-100"
                )}
              >
                <XIcon className="size-3.5" strokeWidth={2} />
              </button>
            </div>
          );
        })}

        {/* At the end of the tabs rather than pinned to the right edge, so
            it stays where the last tab is however many there are. */}
        <button
          type="button"
          data-slot="tab-new"
          aria-label="브라우저 탭 열기"
          title="브라우저 탭 열기"
          onClick={() => open(BROWSER_ROUTE, { title: "브라우저" })}
          className="my-1 ml-1 flex size-7 shrink-0 items-center justify-center self-center rounded-md text-muted-foreground hover:bg-background hover:text-foreground"
        >
          <PlusIcon className="size-4" strokeWidth={1.5} />
        </button>
      </nav>

      {actions ? <div className="flex shrink-0 items-center gap-1 pl-2">{actions}</div> : null}
    </header>
  );
}
