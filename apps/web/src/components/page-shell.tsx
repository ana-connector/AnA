"use client";

import { type ReactNode } from "react";
import { SessionSidebar } from "@/components/session-sidebar";
import { TabBar } from "@/components/tab-bar";
import { useTabTitle } from "@/components/tabs";
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
  useTabTitle(title);

  return (
    // Bounded rather than min-height: the content panel is what scrolls, not
    // the window.
    <SidebarProvider className="h-svh overflow-hidden">
      <SessionSidebar reloadKey={0} />
      <SidebarInset className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
        {/* Outside the scroll area rather than sticky inside it: which pages
            are open does not move while you read one of them. */}
        <TabBar actions={actions} />

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
