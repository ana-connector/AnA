"use client";

import { useEffect, useRef, type ReactNode } from "react";
import Link from "next/link";
import {
  ClockIcon,
  HomeIcon,
  LockIcon,
  Settings2Icon,
  TerminalIcon,
  XIcon,
  type LucideIcon,
} from "lucide-react";
import { useTabs } from "@/components/tabs";
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
  const { tabs, activeHref, close } = useTabs();
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
    <header className="flex h-11 shrink-0 items-stretch border-b border-border bg-muted/40 pr-2">
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
          const RouteIcon = ROUTE_ICONS[tab.href];
          return (
            <div
              key={tab.href}
              ref={active ? activeTab : undefined}
              data-slot="tab"
              data-active={active}
              data-href={tab.href}
              // Middle click closes, the way it does in every browser. The
              // link would otherwise open the page in a new window, which is
              // not what that button means on a tab.
              onAuxClick={(event) => {
                if (event.button !== 1) return;
                event.preventDefault();
                close(tab.href);
              }}
              className={cn(
                // They share the width rather than hugging their names: a tab
                // the width of the word on it is a link. The floor keeps a
                // long list readable and lets the strip scroll instead.
                "group/tab relative flex min-w-32 max-w-72 flex-1 basis-0 items-center gap-2",
                "border-r border-border/60 pr-1 pl-3 text-sm transition-colors",
                active
                  ? // The 2px line marks it and the 1px strip underneath eats
                    // the header's own border, joining the tab to the page.
                    "bg-background text-foreground before:absolute before:inset-x-0 before:top-0 before:h-0.5 before:bg-primary after:absolute after:inset-x-0 after:-bottom-px after:h-px after:bg-background"
                  : "text-muted-foreground hover:bg-background/60 hover:text-foreground"
              )}
            >
              <Link
                href={tab.href}
                aria-current={active ? "page" : undefined}
                title={tab.title}
                className="flex min-w-0 flex-1 items-center gap-2 py-2 outline-none focus-visible:underline"
              >
                {tab.icon ? (
                  <WorkspaceIcon icon={tab.icon} />
                ) : RouteIcon ? (
                  <RouteIcon className="size-4 shrink-0" strokeWidth={1.5} />
                ) : null}
                <span data-slot="tab-title" className="min-w-0 flex-1 truncate">{tab.title}</span>
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
      </nav>

      {actions ? <div className="flex shrink-0 items-center gap-1 pl-2">{actions}</div> : null}
    </header>
  );
}
