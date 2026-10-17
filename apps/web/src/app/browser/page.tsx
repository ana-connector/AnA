"use client";

import { BrowserView } from "@/components/browser-view";
import { PageShell } from "@/components/page-shell";

/** A browser tab with nowhere to go yet: what the + button opens. */
export default function NewBrowserTab() {
  return (
    <PageShell title="브라우저" fill>
      <BrowserView target="" />
    </PageShell>
  );
}
