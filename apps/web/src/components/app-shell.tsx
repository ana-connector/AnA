"use client";

import { type ReactNode } from "react";
import { PaneHost } from "@/components/pane-host";
import { SessionSidebar } from "@/components/session-sidebar";
import { useShellMeta } from "@/components/shell-meta";
import { UsageBar } from "@/components/usage-bar";
import { SidebarInset, SidebarProvider, SidebarTrigger } from "@/components/ui/sidebar";
import { usePaneMode } from "@/hooks/use-pane-mode";

/** Window chrome that outlives any one route.
 *
 *  Tab switches used to remount `PageShell` with the page, which tore down the
 *  sidebar, every split pane, and every iframe. Keeping the chrome here means
 *  only the focused pane's document swaps when the router moves. */
export function AppShell({ children }: { children: ReactNode }) {
  const pane = usePaneMode();
  const meta = useShellMeta();

  if (pane) {
    // One pane of a split: the outer window owns chrome; this document is
    // only the page body (`?pane=1`).
    return <>{children}</>;
  }

  return (
    <SidebarProvider className="h-svh overflow-hidden">
      <SessionSidebar reloadKey={meta.sidebarReloadKey} />
      <SidebarInset className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
        <PaneHost leading={<SidebarTrigger />} actions={meta.barActions}>
          {children}
        </PaneHost>
        <UsageBar
          refreshKey={meta.usage?.refreshKey ?? 0}
          totalCostUsd={meta.usage?.totalCostUsd ?? 0}
          totalTokens={meta.usage?.totalTokens ?? 0}
          failingAgents={meta.usage?.failingAgents}
        />
      </SidebarInset>
    </SidebarProvider>
  );
}
