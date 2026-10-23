"use client";

import { useState } from "react";
import { ChevronDownIcon, TerminalIcon } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { cn } from "@/lib/utils";
import type { ToolEvent, ToolRun } from "@/hooks/useChatSocket";
import { toolDisplay } from "@/lib/tool-display";

/** Up to this many calls the run is short enough to read inline; beyond it the
 *  card starts closed so a long run cannot bury the reply it belongs to. */
const OPEN_BY_DEFAULT_UP_TO = 4;

const VERDICT_LABEL: Record<string, string> = {
  allow: "허용",
  deny: "거부",
  pending: "대기",
};

function commandOf(tool: ToolEvent): string {
  return toolDisplay(tool.tool_name, tool.input);
}

/** What the agent actually ran, as one card under its reply.
 *
 *  Every entry comes from the governance gate, so this is the same record the
 *  audit log keeps — not a separate, prettier story.
 *
 *  One card per run of calls, not one per call: the transcript used to render a
 *  card per tool block, so a turn that ran eight commands showed eight cards
 *  each headed "도구 1회". The header is the summary and the fold, and the rows
 *  are for when you stop to look. */
export function ToolCalls({
  tools,
  run,
  defaultOpen,
  loadFullInput,
  loadRun,
}: {
  tools: ToolEvent[];
  /** A run restored from history, where the rows are not on the page yet:
   *  the header is drawn from this and `loadRun` fetches the calls when the
   *  card is opened. */
  run?: ToolRun;
  /** Overrides the length heuristic — the in-flight turn keeps its card open so
   *  you can watch what the agent is doing. */
  defaultOpen?: boolean;
  /** Fetches a restored call's whole arguments. History pages carry only the
   *  label-sized part of them — a long conversation's transcript is mostly
   *  tool arguments — so the dialog asks for the rest when it opens. */
  loadFullInput?: (toolUseId: string) => Promise<Record<string, unknown> | null>;
  /** Fetches the calls of a restored run, in the order they ran. */
  loadRun?: (run: ToolRun) => Promise<ToolEvent[]>;
}) {
  const [selected, setSelected] = useState<ToolEvent | null>(null);
  // toolUseId -> the arguments as stored, once fetched.
  const [fullInputs, setFullInputs] = useState<Record<string, Record<string, unknown>>>({});
  const [loadingInput, setLoadingInput] = useState(false);
  // The rows of a restored run, once they have been asked for.
  const [fetched, setFetched] = useState<ToolEvent[] | null>(null);
  const [loadingRows, setLoadingRows] = useState(false);
  const count = run?.count ?? tools.length;
  const [open, setOpen] = useState(
    // A restored run starts closed whatever its length: opening it is a fetch,
    // and nobody asked for one by scrolling past.
    run ? false : defaultOpen ?? tools.length <= OPEN_BY_DEFAULT_UP_TO
  );

  const rows = fetched ?? tools;

  const toggle = () => {
    const next = !open;
    setOpen(next);
    if (!next || !run || !loadRun || fetched || loadingRows) return;
    setLoadingRows(true);
    void loadRun(run)
      .then((loaded) => setFetched(loaded))
      .finally(() => setLoadingRows(false));
  };

  const show = (tool: ToolEvent) => {
    setSelected(tool);
    const id = tool.tool_use_id;
    if (!tool.input_truncated || !loadFullInput || !id || fullInputs[id]) return;
    setLoadingInput(true);
    void loadFullInput(id)
      .then((input) => {
        if (input) setFullInputs((current) => ({ ...current, [id]: input }));
      })
      .finally(() => setLoadingInput(false));
  };

  const shownInput = (tool: ToolEvent): Record<string, unknown> =>
    (tool.tool_use_id ? fullInputs[tool.tool_use_id] : undefined) ?? tool.input ?? {};

  if (count === 0) return null;

  const denied = run ? run.denied : tools.filter((tool) => tool.verdict === "deny").length;
  const pending = run
    ? run.pending
    : tools.filter((tool) => tool.verdict === "pending").length;
  const lastLabel = run ? run.last_label : commandOf(tools[tools.length - 1]);

  return (
    <>
      <div className="flex min-w-0 flex-col gap-0.5 rounded-md border border-border bg-background/60 p-2">
        <button
          type="button"
          onClick={toggle}
          aria-expanded={open}
          className="flex min-w-0 items-center gap-1.5 rounded-sm px-1 py-0.5 text-left text-[11px] text-muted-foreground hover:bg-muted"
        >
          <ChevronDownIcon
            className={cn("size-3 shrink-0 transition-transform", !open && "-rotate-90")}
          />
          <span className="shrink-0">도구 {count}회</span>
          {denied > 0 && <span className="shrink-0 text-destructive">거부 {denied}</span>}
          {pending > 0 && <span className="shrink-0">승인 대기 {pending}</span>}
          {/* Closed, the card still says what the last thing it ran was: a bare
              count tells you nothing about whether to open it. */}
          {!open && <code className="min-w-0 truncate font-mono">{lastLabel}</code>}
        </button>

        {open && loadingRows && rows.length === 0 && (
          <p className="px-1 py-0.5 text-[11px] text-muted-foreground">불러오는 중…</p>
        )}

        {open && (
          <div className="flex max-h-64 min-w-0 flex-col gap-0.5 overflow-y-auto">
            {rows.map((tool, index) => {
              const isDeny = tool.verdict === "deny";
              return (
                <button
                  key={tool.tool_use_id ?? index}
                  type="button"
                  onClick={() => show(tool)}
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
                    <Badge variant={isDeny ? "destructive" : "outline"} className="shrink-0">
                      {VERDICT_LABEL[tool.verdict] ?? tool.verdict}
                    </Badge>
                  ) : null}
                </button>
              );
            })}
          </div>
        )}
      </div>

      {/* The row is a one-line summary of a call that may carry a page of
          arguments; the title attribute it used to rely on could not show
          them. */}
      <Dialog open={selected !== null} onOpenChange={(isOpen) => !isOpen && setSelected(null)}>
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
            <>
              <pre className="max-h-[60vh] overflow-auto rounded-md bg-muted p-3 font-mono text-xs whitespace-pre-wrap break-all">
                {JSON.stringify(shownInput(selected), null, 2)}
              </pre>
              {selected.input_truncated &&
              !(selected.tool_use_id && fullInputs[selected.tool_use_id]) ? (
                <p className="text-xs text-muted-foreground">
                  {loadingInput
                    ? "전체 인자를 불러오는 중…"
                    : "요약된 인자입니다. 전체 기록은 도구 로그에 있습니다."}
                </p>
              ) : null}
            </>
          ) : null}
        </DialogContent>
      </Dialog>
    </>
  );
}
