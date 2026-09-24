"use client";

import { type ReactNode } from "react";
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
import { cn } from "@/lib/utils";
import { SidebarInset, SidebarProvider, SidebarTrigger } from "@/components/ui/sidebar";

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
  center,
  children,
}: {
  title: string;
  description?: ReactNode;
  actions?: ReactNode;
  /** Sits the column in the middle of the panel while it fits. For a page
   *  that is short by design; a long one scrolls from the top either way. */
  center?: boolean;
  children: ReactNode;
}) {
  const openWorkspace = (id: string | null) => {
    if (id) window.localStorage.setItem("ana_thread_id", id);
    else window.localStorage.removeItem("ana_thread_id");
    window.location.href = "/";
  };

  return (
    // Bounded rather than min-height: the inset panel is what scrolls, not the
    // window. A document-height panel would carry its rounded corners and its
    // bottom margin off the bottom of the screen, which is why these pages
    // stopped looking inset as soon as they were long enough to scroll.
    <SidebarProvider className="h-svh overflow-hidden">
      {/* No workspace is active here: settings, the tool log and the secrets
          belong to the installation, not to whichever conversation you were
          last in. Highlighting one said you were still inside it. */}
      <SessionSidebar workspaceId={null} onOpenWorkspace={openWorkspace} reloadKey={0} />
      <SidebarInset className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
        {/* Outside the scroll area rather than sticky inside it: "where am I"
            stays put, and the panel's top corners stay clean. */}
        <header className="flex h-12 shrink-0 items-center gap-2 border-b border-border px-3">
          <SidebarTrigger />
          <Separator orientation="vertical" className="mr-1 h-4" />
          <Breadcrumb>
            <BreadcrumbList>
              <BreadcrumbItem className="hidden sm:block">
                <BreadcrumbLink href="/">AnA</BreadcrumbLink>
              </BreadcrumbItem>
              <BreadcrumbSeparator className="hidden sm:block" />
              <BreadcrumbItem>
                <BreadcrumbPage>{title}</BreadcrumbPage>
              </BreadcrumbItem>
            </BreadcrumbList>
          </Breadcrumb>
          {actions ? <div className="ml-auto flex items-center gap-1">{actions}</div> : null}
        </header>

        <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">
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
      </SidebarInset>
    </SidebarProvider>
  );
}
