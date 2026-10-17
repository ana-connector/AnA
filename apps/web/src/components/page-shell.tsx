"use client";

import { useLayoutEffect, type ReactNode } from "react";
import { useTabTitle } from "@/components/tabs";
import { useShellMetaPublisher, type ShellUsage } from "@/components/shell-meta";
import { usePaneMode } from "@/hooks/use-pane-mode";
import { cn } from "@/lib/utils";

/** The focused page's body: title, column layout, and chrome slots.
 *
 *  Window chrome (sidebar, panes, usage bar) lives in `AppShell` above the
 *  router. This component only styles the page and publishes bar/usage props
 *  so a tab switch does not remount the surrounding UI. */
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
  usage?: ShellUsage;
  /** Bumped when a turn finishes, so the sidebar's list follows along. */
  sidebarReloadKey?: number;
  children: ReactNode;
}) {
  useTabTitle(title);
  const pane = usePaneMode();
  const { setMeta, resetMeta } = useShellMetaPublisher();

  // Publish into the window chrome. Skipped inside a pane iframe — that
  // document has no chrome of its own, and writing here would fight the
  // outer window's focused page.
  useLayoutEffect(() => {
    if (pane) return;
    setMeta({
      barActions: barActions ?? null,
      usage: usage ?? null,
      sidebarReloadKey,
    });
  }, [pane, barActions, usage, sidebarReloadKey, setMeta]);

  useLayoutEffect(() => {
    if (pane) return;
    return () => resetMeta();
  }, [pane, resetMeta]);

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
    <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
      {column}
      {actionBar}
    </div>
  );
}
