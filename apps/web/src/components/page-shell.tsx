"use client";

import { useEffect, useState, type ReactNode } from "react";
import { SessionSidebar } from "@/components/session-sidebar";
import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from "@/components/ui/breadcrumb";
import { Separator } from "@/components/ui/separator";
import { SidebarProvider, SidebarTrigger } from "@/components/ui/sidebar";
import { fetchWorkspaces, type WorkspaceSummary } from "@/lib/api";

/** The frame every non-chat page sits in: project navigation on the left, a
 *  header that says where you are, and one content column.
 *
 *  Each page used to build its own header — a back-to-chat button, a bold
 *  span, and its own container width — so the three utility pages looked like
 *  three different apps and the shell's own header row sat empty above them.
 *  Now the shell owns the header (breadcrumb, description, actions) and the
 *  page owns only its content. */
export function PageShell({
  title,
  description,
  actions,
  children,
}: {
  title: string;
  description?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
}) {
  const [current, setCurrent] = useState<WorkspaceSummary | null>(null);

  useEffect(() => {
    const id = window.localStorage.getItem("ana_thread_id");
    if (!id) return;
    void fetchWorkspaces()
      .then((items) => setCurrent(items.find((item) => item.id === id) ?? null))
      .catch(() => {});
  }, []);

  const openWorkspace = (id: string | null) => {
    if (id) window.localStorage.setItem("ana_thread_id", id);
    else window.localStorage.removeItem("ana_thread_id");
    window.location.href = "/";
  };

  return (
    <SidebarProvider className="min-h-svh">
      <SessionSidebar
        workspaceId={current?.id ?? null}
        workspace={current?.path ?? null}
        workspaceName={current?.name ?? null}
        onOpenWorkspace={openWorkspace}
        reloadKey={0}
      />
      <main className="flex min-w-0 flex-1 flex-col bg-background">
        {/* Sticky, because these pages scroll and "where am I" should not
            scroll away with them. */}
        <header className="sticky top-0 z-10 flex h-12 shrink-0 items-center gap-2 border-b border-border bg-background/95 px-3 backdrop-blur supports-[backdrop-filter]:bg-background/80">
          <SidebarTrigger />
          <Separator orientation="vertical" className="mr-1 h-4" />
          <Breadcrumb>
            <BreadcrumbList>
              <BreadcrumbItem className="hidden sm:block">
                <BreadcrumbLink href="/">
                  {current?.name ?? "채팅"}
                </BreadcrumbLink>
              </BreadcrumbItem>
              <BreadcrumbSeparator className="hidden sm:block" />
              <BreadcrumbItem>
                <BreadcrumbPage>{title}</BreadcrumbPage>
              </BreadcrumbItem>
            </BreadcrumbList>
          </Breadcrumb>
          {actions ? <div className="ml-auto flex items-center gap-1">{actions}</div> : null}
        </header>

        <div className="mx-auto flex w-full max-w-4xl flex-col gap-6 px-4 py-6 sm:px-6">
          {description ? (
            <p className="max-w-[70ch] text-sm text-muted-foreground">{description}</p>
          ) : null}
          {children}
        </div>
      </main>
    </SidebarProvider>
  );
}
