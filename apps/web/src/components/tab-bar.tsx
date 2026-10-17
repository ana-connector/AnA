"use client";

import { useEffect, useRef, type ReactNode } from "react";
import Link from "next/link";
import {
  ClockIcon,
  Columns2Icon,
  GlobeIcon,
  HomeIcon,
  LockIcon,
  PlusIcon,
  Rows2Icon,
  Settings2Icon,
  TerminalIcon,
  XIcon,
  type LucideIcon,
} from "lucide-react";
import { useTabs, type Tab } from "@/components/tabs";
import { TAB_DRAG_TYPE } from "@/components/pane-host";
import { WorkspaceIcon } from "@/components/workspace-icon";
import { Separator } from "@/components/ui/separator";
import { SidebarTrigger } from "@/components/ui/sidebar";
import { leaves } from "@/lib/panes";
import { BROWSER_ROUTE } from "@/lib/preview";
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

/** What is written on a tab: the mark, the name, and the folder it works in. */
function TabFace({ tab, active }: { tab: Tab; active: boolean }) {
  return (
    <>
      {iconFor(tab)}
      <span data-slot="tab-title" className="min-w-0 flex-1 truncate">
        {tab.title}
      </span>
      {tab.detail ? (
        // The folder used to sit at the far right of the bar, where it
        // belonged to whichever tab happened to be open. It belongs to the
        // tab: two workspaces are often called the same thing in different
        // trees. Capped rather than left to shrink with the name: when both
        // are too long, the name is the one worth reading.
        <span
          data-slot="tab-detail"
          className={cn(
            "max-w-[45%] shrink-0 truncate font-mono text-[11px]",
            active ? "text-muted-foreground" : "text-muted-foreground/80"
          )}
        >
          {tab.detail}
        </span>
      ) : null}
    </>
  );
}

function CloseButton({ label, onClose, always }: { label: string; onClose: () => void; always: boolean }) {
  return (
    <button
      type="button"
      data-slot="tab-close"
      aria-label={`${label} 탭 닫기`}
      onClick={(event) => {
        event.preventDefault();
        event.stopPropagation();
        onClose();
      }}
      // Always there on the active tab, on hover or keyboard focus
      // elsewhere: a row of permanent crosses reads as clutter, and one that
      // only appears on hover cannot be reached by keyboard.
      className={cn(
        "flex size-5 shrink-0 items-center justify-center rounded-sm text-muted-foreground opacity-0 hover:bg-muted hover:text-foreground focus-visible:opacity-100 group-hover/tab:opacity-100",
        always && "opacity-100"
      )}
    >
      <XIcon className="size-3.5" strokeWidth={2} />
    </button>
  );
}

/** Dragging a tab is how the page gets split, so every tab and every part of
 *  a split tab carries the same handlers. */
function dragProps(href: string) {
  return {
    draggable: true,
    onDragStart: (event: React.DragEvent) => {
      event.dataTransfer.setData(TAB_DRAG_TYPE, href);
      event.dataTransfer.effectAllowed = "move";
      // Marked for the length of the drag: a pane's page is an iframe, and
      // an iframe swallows the drag events that would otherwise reach the
      // pane — a cover goes over it while the mark is there.
      document.body.setAttribute("data-dragging-tab", href);
    },
    onDragEnd: () => document.body.removeAttribute("data-dragging-tab"),
  };
}

/** The top of the content pane: every page you have open, and the controls
 *  that belong to the one you are reading.
 *
 *  It replaced a per-page header that repeated where you were — a breadcrumb
 *  on the utility pages, the workspace name on the chat — which told you
 *  nothing you could act on and gave you no way back to the page you had just
 *  left. A tab says both at once.
 *
 *  Pages that are on screen together are one tab, drawn as the split they
 *  are: two pages side by side are one view, and listing them apart left the
 *  bar saying three things were open when two of them were the same screen. */
export function TabBar({ actions }: { actions?: ReactNode }) {
  const { tabs, activeHref, close, open, panes, focusedPane, focusPane } = useTabs();
  const activeTab = useRef<HTMLDivElement | null>(null);
  const shown = leaves(panes);
  const split = shown.length > 1;
  const inSplit = new Set(shown.map((pane) => pane.href));

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
  }, [activeHref, tabs, panes]);

  const chrome = (active: boolean, wide: boolean) =>
    cn(
      // As wide as what is on it and no wider, up to a ceiling: a tab that
      // stretches to fill the bar is a toolbar button, and one that spans a
      // wide window on its own looks broken. Past the ceiling the name is
      // cut; past the floor the strip scrolls instead.
      "group/tab relative flex shrink items-center text-sm transition-colors",
      // A split is one tab: its halves sit inside one outlined box so the
      // bar does not read as two tabs that happen to be next to each other.
      wide
        ? "my-1 mr-1 ml-1 min-w-44 max-w-[30rem] overflow-hidden rounded-md border border-border/70"
        : "min-w-28 max-w-56 gap-2 border-r border-border/60 pr-1 pl-3",
      active
        ? "bg-background text-foreground"
        : "text-muted-foreground hover:bg-background/60 hover:text-foreground"
    );

  /** The line along the bottom edge, on top of the header's own border, so a
   *  selected tab reads as the edge the page hangs from. Inside a split tab
   *  it marks the pane you are in rather than the whole tab. */
  const underline = "after:absolute after:inset-x-0 after:bottom-0 after:h-0.5 after:bg-primary";

  const single = (tab: Tab) => {
    const active = tab.href === activeHref;
    return (
      <div
        key={tab.href}
        ref={active ? activeTab : undefined}
        data-slot="tab"
        data-active={active}
        data-href={tab.href}
        {...dragProps(tab.href)}
        onAuxClick={(event) => {
          // Middle click closes, the way it does in every browser. The link
          // would otherwise open the page in a new window, which is not what
          // that button means on a tab.
          if (event.button !== 1) return;
          event.preventDefault();
          close(tab.href);
        }}
        className={cn(chrome(active, false), active && underline)}
      >
        <Link
          href={tab.href}
          aria-current={active ? "page" : undefined}
          title={tab.detail ? `${tab.title}\n${tab.detail}` : tab.title}
          className="flex min-w-0 flex-1 items-baseline gap-1.5 py-1.5 outline-none focus-visible:underline"
        >
          <TabFace tab={tab} active={active} />
        </Link>
        <CloseButton label={tab.title} onClose={() => close(tab.href)} always={active} />
      </div>
    );
  };

  /** Everything on screen at once, as one tab divided the way the screen is. */
  const group = () => {
    const SplitIcon = panes.kind === "split" && panes.dir === "col" ? Rows2Icon : Columns2Icon;
    const active = inSplit.has(activeHref);
    return (
      <div
        key="split-group"
        ref={active ? activeTab : undefined}
        data-slot="tab"
        data-group="true"
        data-active={active}
        data-href={activeHref}
        className={cn(chrome(active, true), "pl-1.5", active && "bg-background")}
      >
        <SplitIcon className="size-4 shrink-0 text-muted-foreground" strokeWidth={1.5} />
        {shown.map((pane, index) => {
          const tab = tabs.find((item) => item.href === pane.href);
          const here = pane.id === focusedPane;
          return (
            <div
              key={pane.id}
              data-slot="tab-part"
              data-href={pane.href}
              data-active={here}
              {...dragProps(pane.href)}
              onClick={() => focusPane(pane.id)}
              title={tab?.detail ? `${tab.title}\n${tab.detail}` : tab?.title}
              className={cn(
                "group/tab relative flex min-w-0 flex-1 cursor-pointer items-baseline gap-1.5 py-1.5 pr-0.5 pl-2",
                // The divider between parts is the split itself, drawn small.
                index > 0 && "border-l border-border/60",
                here ? `text-foreground ${underline}` : "text-muted-foreground"
              )}
            >
              {tab ? <TabFace tab={tab} active={here} /> : null}
              <CloseButton
                label={tab?.title ?? pane.href}
                onClose={() => close(pane.href)}
                always={here}
              />
            </div>
          );
        })}
      </div>
    );
  };

  // The split occupies the slot of its leftmost member, so splitting does not
  // shuffle the bar around.
  const rendered: ReactNode[] = [];
  let groupPlaced = false;
  for (const tab of tabs) {
    if (split && inSplit.has(tab.href)) {
      if (!groupPlaced) {
        groupPlaced = true;
        rendered.push(group());
      }
      continue;
    }
    rendered.push(single(tab));
  }

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
        {rendered}

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
