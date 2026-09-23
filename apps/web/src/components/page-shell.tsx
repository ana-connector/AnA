"use client";

import { useEffect, useState, type ReactNode } from "react";
import { SessionSidebar } from "@/components/session-sidebar";
import { SidebarInset, SidebarProvider, SidebarTrigger } from "@/components/ui/sidebar";
import { fetchWorkspaces, type WorkspaceSummary } from "@/lib/api";

/** Keeps project navigation visible on the utility pages as well as chat. */
export function PageShell({ children }: { children: ReactNode }) {
  const [current, setCurrent] = useState<WorkspaceSummary | null>(null);

  useEffect(() => {
    const id = window.localStorage.getItem("ana_thread_id");
    if (!id) return;
    void fetchWorkspaces().then((items) => setCurrent(items.find((item) => item.id === id) ?? null)).catch(() => {});
  }, []);

  const openWorkspace = (id: string | null) => {
    if (id) window.localStorage.setItem("ana_thread_id", id);
    else window.localStorage.removeItem("ana_thread_id");
    window.location.href = "/";
  };

  return (
    <SidebarProvider className="min-h-svh pb-2">
      <SessionSidebar
        workspaceId={current?.id ?? null}
        workspace={current?.path ?? null}
        workspaceName={current?.name ?? null}
        onOpenWorkspace={openWorkspace}
        reloadKey={0}
      />
      <SidebarInset className="min-w-0">
        <div className="flex h-12 shrink-0 items-center border-b border-border px-3">
          <SidebarTrigger />
        </div>
        {children}
      </SidebarInset>
    </SidebarProvider>
  );
}
