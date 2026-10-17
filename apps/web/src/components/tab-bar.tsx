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
import { useTabs } from "@/components/tabs";
import { TAB_DRAG_TYPE } from "@/components/pane-host";
import { BROWSER_ROUTE } from "@/lib/preview";
import { WorkspaceIcon } from "@/components/workspace-icon";
import { Separator } from "@/components/ui/separator";
import { SidebarTrigger } from "@/components/ui/sidebar";
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

/** The top of the content pane: every page you have open, and the controls
 *  that belong to the one you are reading.
 *
 *  It replaced a per-page header that repeated where you were — a breadcrumb
 *  on the utility pages, the workspace name on the chat — which told you
 *  nothing you could act on and gave you no way back to the page you had just
 *  left. A tab says both at once. */
export function TabBar({ actions }: { actions?: ReactNode }) {
  const { tabs, activeHref, close, open } = useTabs();
  const activeTab = useRef<HTMLDivElement | null>(null);

  // On a narrow window the bar scrolls, and the tab you just opened can be
  // the one off the end of it: switching tabs would then look like nothing
  // happened at all. After a frame, because a tab is named by the page
  // inside it — the width this measures is not final until that name is.
  //
  // A narrowing window needs the same treatment: the bar keeps the scroll
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
  }, [activeHref, tabs]);

  return (
    // The strip is darker than the page and the open tab is the page's own
    // colour, which is what makes it read as the sheet in front: tabs drawn
    // on the same background as their content are only underlined words.
    <header className="flex h-9 shrink-0 items-stretch border-b border-border bg-muted/40 pr-2">
      <div className="flex shrink-0 items-center gap-2 pr-2 pl-3">
        <SidebarTrigger />
        <Separator orientation="vertical" className="h-4" />
      </div>

      {/* Links rather than a tablist: each one is a page with its own
          address, so the browser's own middle-click, back button and
          open-in-new-window all keep working. */}
      <nav
        aria-label="열린 탭"
        data-slot="tab-bar"
        className="no-scrollbar flex min-w-0 flex-1 items-stretch overflow-x-auto"
      >
        {tabs.map((tab) => {
          const active = tab.href === activeHref;
          const RouteIcon =
            ROUTE_ICONS[tab.href] ?? (tab.href.startsWith(BROWSER_ROUTE) ? GlobeIcon : undefined);
          return (
            <div
              key={tab.href}
              ref={active ? activeTab : undefined}
              data-slot="tab"
              data-active={active}
              data-href={tab.href}
              // Dragged onto the page, a tab splits it. The body is marked
              // for the length of the drag: a pane showing another page is
              // an iframe, and an iframe swallows the drag events that would
              // otherwise reach the pane — a cover goes over it while the
              // mark is there.
              draggable
              onDragStart={(event) => {
                event.dataTransfer.setData(TAB_DRAG_TYPE, tab.href);
                event.dataTransfer.effectAllowed = "move";
                document.body.setAttribute("data-dragging-tab", tab.href);
              }}
              onDragEnd={() => document.body.removeAttribute("data-dragging-tab")}
              // Middle click closes, the way it does in every browser. The
              // link would otherwise open the page in a new window, which is
              // not what that button means on a tab.
              onAuxClick={(event) => {
                if (event.button !== 1) return;
                event.preventDefault();
                close(tab.href);
              }}
              className={cn(
                // As wide as what is on it and no wider, up to a ceiling: a
                // tab that stretches to fill the bar is a toolbar button, and
                // one that spans a wide window on its own looks broken. Past
                // the ceiling the name is cut; past the floor the strip
                // scrolls instead.
                "group/tab relative flex min-w-28 max-w-56 shrink items-center gap-2",
                "border-r border-border/60 pr-1 pl-3 text-sm transition-colors",
                active
                  ? // The line sits along the bottom, on top of the header's
                    // own border, so it reads as the edge the page hangs from.
                    "bg-background text-foreground after:absolute after:inset-x-0 after:bottom-0 after:h-0.5 after:bg-primary"
                  : "text-muted-foreground hover:bg-background/60 hover:text-foreground"
              )}
            >
              <Link
                href={tab.href}
                aria-current={active ? "page" : undefined}
                title={tab.detail ? `${tab.title}\n${tab.detail}` : tab.title}
                className="flex min-w-0 flex-1 items-baseline gap-1.5 py-1.5 outline-none focus-visible:underline"
              >
                {tab.icon ? (
                  <WorkspaceIcon icon={tab.icon} className="self-center" />
                ) : RouteIcon ? (
                  <RouteIcon className="size-4 shrink-0 self-center" strokeWidth={1.5} />
                ) : null}
                <span data-slot="tab-title" className="min-w-0 flex-1 truncate">{tab.title}</span>
                {tab.detail ? (
                  // The folder used to sit at the far right of the bar, where
                  // it belonged to whichever tab happened to be open. It
                  // belongs to the tab: two workspaces are often called the
                  // same thing in different trees. Whichever of the two runs
                  // out of room is the one that gets cut.
                  <span
                    data-slot="tab-detail"
                    // Hidden outright on a tab too narrow to hold both: a
                    // folder cut down to "…/…" takes room from the name and
                    // says nothing. The title attribute still has it.
                    // Capped rather than left to shrink with the name: when
                    // both are too long, the name is the one worth reading.
                    className="hidden max-w-[45%] shrink-0 truncate font-mono text-[11px] text-muted-foreground @[10rem]/tab:inline"
                  >
                    {tab.detail}
                  </span>
                ) : null}
              </Link>
              <button
                type="button"
                data-slot="tab-close"
                aria-label={`${tab.title} 탭 닫기`}
                onClick={() => close(tab.href)}
                // Always there on the active tab, on hover or keyboard focus
                // elsewhere: a row of permanent crosses reads as clutter, and
                // one that only appears on hover cannot be reached by keyboard.
                className={cn(
                  "flex size-5 shrink-0 items-center justify-center rounded-sm text-muted-foreground opacity-0 hover:bg-muted hover:text-foreground focus-visible:opacity-100 group-hover/tab:opacity-100",
                  active && "opacity-100"
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
