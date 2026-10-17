"use client";

import { type ReactNode } from "react";
import { PaneHost } from "@/components/pane-host";
import { SessionSidebar } from "@/components/session-sidebar";
import { TabBar } from "@/components/tab-bar";
import { useTabTitle } from "@/components/tabs";
import { UsageBar } from "@/components/usage-bar";
import { usePaneMode } from "@/hooks/use-pane-mode";
import { cn } from "@/lib/utils";
import { SidebarInset, SidebarProvider } from "@/components/ui/sidebar";

/** The frame every non-chat page sits in: project navigation on the left, the
 *  open tabs across the top, and one content column.
 *
 *  Each page used to build its own header — a back-to-chat button, a bold
 *  span, and its own container width — so the three utility pages looked like
 *  three different apps. Then the shell owned a breadcrumb that said the same
 *  thing the sidebar highlight already said. Now the page says only what it is
 *  called, the tab carries that name, and the page owns only its content. */
export function PageShell({
  title,
  description,
  actions,
  barActions,
  center,
  fill,
  usage,
  sidebarReloadKey = 0,
  children,
}: {
  /** What the tab is called. Empty while the page does not know yet — a tab
   *  keeps the name it was opened with rather than flashing a placeholder. */
  title: string;
  description?: ReactNode;
  /** The page's own controls — saving, mostly. They sit on a bar along the
   *  bottom of the panel rather than in the tab bar: the tab bar is for
   *  moving between pages, and a save button wedged into it both crowded the
   *  tabs and scrolled out of reach on a long page. */
  actions?: ReactNode;
  /** Sits the column in the middle of the panel while it fits. For a page
   *  that is short by design; a long one scrolls from the top either way. */
  center?: boolean;
  /** Hands the whole panel to the page, with no column and no padding. For a
   *  page that is one surface — a conversation, or the browser tab's frame. */
  fill?: boolean;
  /** Small status that belongs beside the tabs rather than under the page:
   *  whether the socket is up, which CLI answered. Not buttons. */
  barActions?: ReactNode;
  /** What this conversation has spent, for the status bar. Left out by a
   *  page that is not a conversation; the bar still shows the limits. */
  usage?: { refreshKey: number; totalCostUsd: number; totalTokens: number;
            failingAgents?: Record<string, string> };
  /** Bumped when a turn finishes, so the sidebar's list follows along. */
  sidebarReloadKey?: number;
  children: ReactNode;
}) {
  useTabTitle(title);
  // One pane of a split view: the window around it owns the sidebar, the
  // tabs and the status bar, so this document renders only the page.
  const pane = usePaneMode();

  const column = fill ? (
    children
  ) : (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-y-auto">
      <div
        className={cn(
          "mx-auto flex w-full max-w-4xl flex-col gap-6 px-4 py-6 sm:px-6",
          // Auto margins rather than justify-center: a centred flex child
          // that outgrows its scroll container has its top cut off, and an
          // auto margin simply stops giving room.
          center && "my-auto"
        )}
      >
        {description ? (
          <p className="max-w-[70ch] text-sm text-muted-foreground">{description}</p>
        ) : null}
        {children}
      </div>
    </div>
  );

  const actionBar = actions ? (
    // Outside the scroll area, so it is there whether you are at the top of
    // a long page or the bottom of it.
    <div
      data-slot="page-actions"
      className="flex shrink-0 items-center justify-end gap-2 border-t border-border px-4 py-2"
    >
      {actions}
    </div>
  ) : null;

  if (pane) {
    return (
      <div className="flex h-svh min-h-0 flex-col overflow-hidden bg-background">
        {column}
        {actionBar}
      </div>
    );
  }

  return (
    // Bounded rather than min-height: the content panel is what scrolls, not
    // the window.
    <SidebarProvider className="h-svh overflow-hidden">
      <SessionSidebar reloadKey={sidebarReloadKey} />
      <SidebarInset className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
        {/* Outside the scroll area rather than sticky inside it: which pages
            are open does not move while you read one of them. */}
        <TabBar actions={barActions} />

        {/* The page, or several of them side by side: a tab dragged onto the
            panel splits it. Only the focused pane is this document; the rest
            are the same app in an iframe. */}
        <PaneHost>
          {column}
          {actionBar}
        </PaneHost>

        {/* The status bar belongs to the window, not to the page in it: it
            stays put across every page and under every split. */}
        <UsageBar
          refreshKey={usage?.refreshKey ?? 0}
          totalCostUsd={usage?.totalCostUsd ?? 0}
          totalTokens={usage?.totalTokens ?? 0}
          failingAgents={usage?.failingAgents}
        />
      </SidebarInset>
    </SidebarProvider>
  );
}
