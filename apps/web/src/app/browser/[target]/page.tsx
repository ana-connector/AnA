"use client";

import { use, useEffect, useState } from "react";
import { BrowserView } from "@/components/browser-view";
import { PageShell } from "@/components/page-shell";
import { previewLabel, previewTarget } from "@/lib/preview";

/** One previewed page. The address is in the path rather than in a query
 *  because a tab is identified by its path: two previews are two tabs. */
export default function BrowserTab({ params }: { params: Promise<{ target: string }> }) {
  const target = previewTarget(use(params).target);
  // Host:port until the iframe's `<title>` is readable (same origin); then
  // that document title becomes the ANA tab name.
  const [title, setTitle] = useState(() => previewLabel(target));
  useEffect(() => {
    setTitle(previewLabel(target));
  }, [target]);
  return (
    <PageShell title={title} fill>
      <BrowserView key={target} target={target} onDocumentTitle={setTitle} />
    </PageShell>
  );
}
