"use client";

import { use } from "react";
import { BrowserView } from "@/components/browser-view";
import { PageShell } from "@/components/page-shell";
import { previewLabel, previewTarget } from "@/lib/preview";

/** One previewed page. The address is in the path rather than in a query
 *  because a tab is identified by its path: two previews are two tabs. */
export default function BrowserTab({ params }: { params: Promise<{ target: string }> }) {
  const target = previewTarget(use(params).target);
  return (
    <PageShell title={previewLabel(target)} fill>
      <BrowserView key={target} target={target} />
    </PageShell>
  );
}
