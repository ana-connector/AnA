"use client";

import { useState } from "react";
import { BrainIcon, ChevronDownIcon, ChevronRightIcon } from "lucide-react";

/** The agent's thinking, collapsed by default.
 *
 *  Shown separately from the answer rather than mixed into it: it is useful
 *  while a long turn runs and noise afterwards, which is exactly what a
 *  collapsed block is for. `streaming` shimmers the label so a silent turn
 *  still looks alive. */
export function Reasoning({
  text,
  streaming = false,
}: {
  text: string;
  streaming?: boolean;
}) {
  const [open, setOpen] = useState(false);
  if (!text.trim()) return null;

  return (
    <div className="rounded-md border border-border bg-background/60">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className="flex w-full items-center gap-1.5 px-2 py-1.5 text-left text-xs text-muted-foreground"
      >
        {open ? <ChevronDownIcon className="size-3.5" /> : <ChevronRightIcon className="size-3.5" />}
        <BrainIcon className="size-3.5" />
        <span className={streaming ? "shimmer" : undefined}>
          {streaming ? "Thinking…" : "생각 과정"}
        </span>
        <span className="ml-auto">{text.length.toLocaleString()}자</span>
      </button>
      {open && (
        <pre className="max-h-64 overflow-auto px-3 pb-2 text-xs whitespace-pre-wrap text-muted-foreground">
          {text}
        </pre>
      )}
    </div>
  );
}
