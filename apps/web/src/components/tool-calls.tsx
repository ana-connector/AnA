"use client";

import { useState } from "react";
import { ChevronDownIcon, TerminalIcon } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { cn } from "@/lib/utils";
import type { ToolEvent } from "@/hooks/useChatSocket";
import { toolDisplay } from "@/lib/tool-display";

/** Beyond this many calls the list stops being readable inline and starts
 *  burying the reply it belongs to. */
const INLINE_LIMIT = 4;

const VERDICT_LABEL: Record<string, string> = {
  allow: "허용",
  deny: "거부",
  pending: "대기",
};

function commandOf(tool: ToolEvent): string {
  return toolDisplay(tool.tool_name, tool.input);
}

/** What the agent actually ran, inline under its reply.
 *
 *  Every entry comes from the governance gate, so this is the same record the
 *  audit log keeps — not a separate, prettier story. Three things changed from
 *  the flat list this replaced: a denied call no longer looks like an allowed
 *  one, a long run collapses instead of burying the answer, and a row can be
 *  opened to see the arguments the truncation hid. */
export function ToolCalls({ tools }: { tools: ToolEvent[] }) {
  const [selected, setSelected] = useState<ToolEvent | null>(null);

  if (tools.length === 0) return null;

  const denied = tools.filter((tool) => tool.verdict === "deny").length;
  const head = tools.slice(0, INLINE_LIMIT);
  const rest = tools.slice(INLINE_LIMIT);

  const row = (tool: ToolEvent, index: number) => {
    const isDeny = tool.verdict === "deny";
    return (
      <button
        key={index}
        type="button"
        onClick={() => setSelected(tool)}
        className={cn(
          "flex w-full items-start gap-2 rounded-sm px-1 py-0.5 text-left text-xs",
          "hover:bg-muted focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none"
        )}
      >
        <TerminalIcon
          className={cn(
            "mt-0.5 size-3.5 shrink-0",
            isDeny ? "text-destructive" : "text-muted-foreground"
          )}
        />
        <code
          className={cn(
            "min-w-0 flex-1 truncate font-mono",
            isDeny && "text-destructive line-through decoration-destructive/40"
          )}
        >
          {commandOf(tool)}
        </code>
        {tool.verdict !== "allow" ? (
          <Badge
            variant={isDeny ? "destructive" : "outline"}
            className="shrink-0"
          >
            {VERDICT_LABEL[tool.verdict] ?? tool.verdict}
          </Badge>
        ) : null}
      </button>
    );
  };

  return (
    <>
      <div className="flex flex-col gap-0.5 rounded-md border border-border bg-background/60 p-2">
        {/* The count is the summary a reader skimming the transcript wants;
            the rows are for when they stop to look. */}
        <div className="flex items-center gap-2 px-1 pb-1 text-[11px] text-muted-foreground">
          <span>도구 {tools.length}회</span>
          {denied > 0 ? (
            <span className="text-destructive">거부 {denied}회</span>
          ) : null}
        </div>

        {head.map(row)}

        {rest.length > 0 ? (
          <Collapsible>
            <CollapsibleTrigger
              render={
                <button
                  type="button"
                  className="group flex w-full items-center gap-1 rounded-sm px-1 py-1 text-left text-[11px] text-muted-foreground hover:bg-muted"
                />
              }
            >
              <ChevronDownIcon className="size-3 transition-transform group-data-[panel-open]:rotate-180" />
              {rest.length}개 더 보기
            </CollapsibleTrigger>
            <CollapsibleContent className="flex flex-col gap-0.5">
              {rest.map((tool, index) => row(tool, INLINE_LIMIT + index))}
            </CollapsibleContent>
          </Collapsible>
        ) : null}
      </div>

      {/* The row is a one-line summary of a call that may carry a page of
          arguments; the title attribute it used to rely on could not show
          them. */}
      <Dialog open={selected !== null} onOpenChange={(open) => !open && setSelected(null)}>
        <DialogContent className="sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle className="font-mono text-sm">{selected?.tool_name}</DialogTitle>
            <DialogDescription>
              {selected
                ? `${VERDICT_LABEL[selected.verdict] ?? selected.verdict} · ${selected.decided_by}${
                    selected.rule_id ? ` · ${selected.rule_id}` : ""
                  }`
                : ""}
            </DialogDescription>
          </DialogHeader>
          {selected ? (
            <pre className="max-h-[60vh] overflow-auto rounded-md bg-muted p-3 font-mono text-xs whitespace-pre-wrap break-all">
              {JSON.stringify(selected.input ?? {}, null, 2)}
            </pre>
          ) : null}
        </DialogContent>
      </Dialog>
    </>
  );
}
