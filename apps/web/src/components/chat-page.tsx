"use client";

import { useCallback, useMemo, useRef, useState } from "react";
import dynamic from "next/dynamic";
import { CopyIcon, NetworkIcon } from "lucide-react";
import {
  useChatSocket,
  type MessageBlock,
  type SubagentTask,
  type ToolEvent,
  type ToolRun,
} from "@/hooks/useChatSocket";
import { AGENT_LABELS, type Agent } from "@/components/ui/agent-toggle";
import { ApprovalCard } from "@/components/ui/approval-card";
import { QuestionCard } from "@/components/ui/question-card";
import { Markdown } from "@/components/markdown";
import { ToolCalls } from "@/components/tool-calls";
import { fetchTurnDetail, type HistoryBlock, type HistoryTurn } from "@/lib/api";
// Loaded when a turn actually has a fan-out to draw: the graph brings its own
// layout library (@xyflow) and its stylesheet, which is a large part of a
// first load that most conversations never render.
const PlanGraph = dynamic(
  () => import("@/components/plan-graph").then((module) => module.PlanGraph),
  { ssr: false }
);
import { Reasoning } from "@/components/reasoning";
import { PageShell } from "@/components/page-shell";
import { TurnStatusLine } from "@/components/turn-status";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Empty, EmptyContent, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty";
import { Skeleton } from "@/components/ui/skeleton";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { clockTime, duration, timeExact } from "@/lib/format";
import { notify } from "@/lib/notify";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Composer, type ComposerHandle } from "@/components/composer";
import { Marker, MarkerContent } from "@/components/ui/marker";
import {
  MessageScroller,
  MessageScrollerButton,
  MessageScrollerContent,
  MessageScrollerItem,
  MessageScrollerProvider,
  MessageScrollerViewport,
} from "@/components/ui/message-scroller";

const TASK_STATUS_LABEL: Record<SubagentTask["status"], string> = {
  running: "실행 중",
  done: "완료",
  error: "실패",
  timeout: "시간 초과",
  blocked: "대기 취소",
  unknown: "상태 확인 불가",
};

/** What a fresh workspace can be asked for. Three concrete requests beat a
 *  blank rectangle: they say what this thing is for, and one click is faster
 *  than composing the first sentence. */
const STARTERS = [
  "이 폴더 구조를 읽고 무슨 프로젝트인지 한 단락으로 정리해줘",
  "테스트를 실행하고 실패하는 것만 보고해줘",
  "최근 커밋 5개를 읽고 지금 작업 중인 흐름을 설명해줘",
];

/** The arguments of one call inside a stored turn, wherever they sit: the
 *  turn's own blocks, its audit-derived list, or one of its sub-agents' runs.
 *  Searched rather than indexed because the same id appears in exactly one of
 *  those places and which one depends on when the turn was recorded. */
function findToolInput(
  turn: HistoryTurn,
  toolUseId: string
): Record<string, unknown> | null {
  const match = (record: { tool_use_id: string | null } | undefined) =>
    record?.tool_use_id === toolUseId;
  for (const record of turn.tools ?? []) {
    if (match(record)) return record.input ?? {};
  }
  for (const block of turn.meta?.blocks ?? []) {
    if (block.kind === "tool" && match(block.tool)) return block.tool.input ?? {};
  }
  for (const task of turn.meta?.subagents ?? []) {
    for (const record of task.tools ?? []) {
      if (match(record)) return record.input ?? {};
    }
    for (const block of task.blocks ?? []) {
      if (block.kind === "tool" && match(block.tool)) return block.tool.input ?? {};
    }
  }
  return null;
}

/** The runs of calls in a stored turn, numbered the way the server numbers
 *  them: the turn's own blocks first, then each sub-agent's in order. A
 *  summarized run carries that number, so this is how its rows are found. */
function toolRuns(turn: HistoryTurn): ToolEvent[][] {
  const runs: ToolEvent[][] = [];
  const collect = (blocks: HistoryBlock[] | undefined) => {
    let current: ToolEvent[] = [];
    for (const block of blocks ?? []) {
      if (block.kind === "tool") {
        current.push(block.tool as ToolEvent);
        continue;
      }
      if (current.length) runs.push(current);
      current = [];
    }
    if (current.length) runs.push(current);
  };
  collect(turn.meta?.blocks);
  for (const task of turn.meta?.subagents ?? []) collect(task.blocks);
  return runs;
}

/** A reply's blocks with runs of consecutive tool calls merged into one group.
 *
 *  The socket records one block per call, in the order things happened, which is
 *  what keeps a command sitting between the two things the agent said around it.
 *  Rendering that list one-block-at-a-time gave a separate card per call, each
 *  headed "도구 1회" — a turn that ran eight commands looked like eight
 *  one-command turns. Grouping only merges neighbours, so the ordering that
 *  matters is untouched. */
type Grouped =
  | { kind: "text"; text: string }
  | { kind: "notice"; text: string }
  | { kind: "tools"; tools: ToolEvent[] }
  /** A run history summarized: already one card, with its rows still on the
   *  server. */
  | { kind: "tool_run"; run: ToolRun };

function groupBlocks(blocks: MessageBlock[]): Grouped[] {
  const grouped: Grouped[] = [];
  for (const block of blocks) {
    if (block.kind === "text" || block.kind === "notice" || block.kind === "tool_run") {
      grouped.push(block);
      continue;
    }
    const last = grouped[grouped.length - 1];
    if (last?.kind === "tools") last.tools.push(block.tool);
    else grouped.push({ kind: "tools", tools: [block.tool] });
  }
  return grouped;
}

/** One agent's record — text it wrote, calls the gate screened, dividers ANA
 *  inserted — in the order it happened. Used for the lead's reply and, by the
 *  sub-agent panel, for one worker's run: the same thing happened in both, so
 *  reading them should not be two different experiences. */
function BlockList({
  blocks,
  defaultToolsOpen,
  loadFullInput,
  loadRun,
}: {
  blocks: MessageBlock[];
  defaultToolsOpen?: boolean;
  /** Passed down to the tool card: a restored call carries only the
   *  label-sized part of its arguments until someone opens it. */
  loadFullInput?: (toolUseId: string) => Promise<Record<string, unknown> | null>;
  /** Fetches the calls of a run that history only summarized. */
  loadRun?: (run: ToolRun) => Promise<ToolEvent[]>;
}) {
  return (
    <>
      {groupBlocks(blocks).map((block, index) =>
        block.kind === "tool_run" ? (
          <ToolCalls
            key={index}
            tools={[]}
            run={block.run}
            loadRun={loadRun}
            loadFullInput={loadFullInput}
          />
        ) : block.kind === "tools" ? (
          <ToolCalls
            key={index}
            tools={block.tools}
            defaultOpen={defaultToolsOpen}
            loadFullInput={loadFullInput}
          />
        ) : block.kind === "notice" ? (
          // ANA talking about the turn, not an agent talking in it.
          <Marker key={index} variant="separator">
            <MarkerContent>{block.text}</MarkerContent>
          </Marker>
        ) : (
          block.text.trim().length > 0 && (
            <Markdown key={index}>{block.text}</Markdown>
          )
        )
      )}
    </>
  );
}

export default function ChatPage({ sessionId }: { sessionId: string }) {
  const {
    messages,
    connected,
    connectionVersion,
    streaming,
    usage,
    failingAgents,
    turnsFinished,
    loadingHistory,
    hasOlderTurns,
    loadingOlder,
    loadOlder,
    workspaceName,
    approvals,
    questions,
    status,
    plan,
    planOutcome,
    models,
    route,
    sendMessage,
    cancel,
    answerApproval,
    answerQuestion,
  } = useChatSocket(sessionId);
  // A restored tool call's arguments, fetched when its dialog opens. Bound to
  // the turn the message came from, because that is where the untrimmed record
  // is — one turn, not the whole transcript again.
  // The rows of a run history only summarized. One request per turn, and the
  // same request backs the arguments dialog below it.
  const runLoader = useCallback(
    (turnId: number | undefined) =>
      turnId === undefined
        ? undefined
        : async (run: ToolRun) => {
            const turn = await fetchTurnDetail(sessionId, turnId);
            if (!turn) return [];
            return toolRuns(turn)[run.index] ?? [];
          },
    [sessionId]
  );
  const fullInputLoader = useCallback(
    (turnId: number | undefined) =>
      turnId === undefined
        ? undefined
        : async (toolUseId: string) => {
            const turn = await fetchTurnDetail(sessionId, turnId);
            return turn ? findToolInput(turn, toolUseId) : null;
          },
    [sessionId]
  );
  // The draft itself lives in the composer, so typing does not re-render the
  // transcript; this only lets the starter buttons fill it.
  const composerRef = useRef<ComposerHandle>(null);
  // The sub-agent whose own transcript is open, if any. A panel over the
  // conversation rather than a replacement for it: the lead's reply is the
  // context that makes a worker's output mean anything, so leaving the
  // transcript on screen is the point.
  const [openTask, setOpenTask] = useState<SubagentTask | null>(null);
  // Which turn the open sub-agent came from, when it came from history: its
  // run of calls is summarized like the lead's, and the rows live on that
  // turn. Undefined for the turn in flight, whose calls are already here.
  const [openTaskTurnId, setOpenTaskTurnId] = useState<number | undefined>(undefined);

  // Finished turns are what create and rename sessions, so the sidebar list is
  // refetched when the count of completed turns changes rather than on a timer.
  const completedTurns = useMemo(
    () => messages.filter((m) => m.role === "user").length,
    [messages]
  );

  // The live task carries the status and output the panel should show: a task
  // opened while it was still running would otherwise stay frozen at the
  // moment it was clicked.
  const shownTask = useMemo(() => {
    if (!openTask) return null;
    const live = plan?.tasks.find((task) => task.id === openTask.id);
    return live ?? openTask;
  }, [openTask, plan]);

  // Whether the lead has started writing its answer for the turn in flight.
  // Once it has, the plan card folds itself: its canvas and task list sit below
  // the reply, so an open card pushes the sentence you are waiting for out of
  // the viewport.
  const leadAnswering = useMemo(() => {
    if (!streaming) return false;
    const last = messages[messages.length - 1];
    return last?.role === "agent" && last.text.trim().length > 0;
  }, [messages, streaming]);

  const copyResponse = async (response: string) => {
    try {
      let copied = false;
      if (navigator.clipboard?.writeText) {
        try {
          await navigator.clipboard.writeText(response);
          copied = true;
        } catch {
          // Fall back below when browser permissions reject the async API.
        }
      }
      if (!copied) {
        // Clipboard API is unavailable on some HTTP LAN origins.
        const field = document.createElement("textarea");
        field.value = response;
        field.style.position = "fixed";
        field.style.opacity = "0";
        document.body.appendChild(field);
        field.select();
        copied = document.execCommand("copy");
        field.remove();
        if (!copied) throw new Error("clipboard unavailable");
      }
      notify.success("응답을 복사했습니다");
    } catch {
      notify.error("응답을 복사하지 못했습니다");
    }
  };

  return (
    <>
    <PageShell
      // Empty until the socket says what this workspace is called: the tab
      // was opened with its name already, and a placeholder here would
      // overwrite it for a moment on every visit.
      title={workspaceName ?? ""}
      fill
      sidebarReloadKey={completedTurns}
      usage={{
        refreshKey: turnsFinished,
        totalCostUsd: usage.totalCostUsd,
        totalTokens: usage.totalTokens,
        failingAgents,
      }}
      // Beside the tabs rather than under the page: what a tab cannot say —
      // whether the socket is up, and which CLI the ladder picked.
      barActions={
          <>
            {/* Silence while the socket is up: a badge that is always green
                says nothing. Only the broken state is worth a word, and it
                is a word rather than a badge -- the composer is disabled
                anyway. */}
            {!connected && (
              <span className="shrink-0 text-xs text-destructive">연결 끊김</span>
            )}

            {route && (
              // Which CLI the ladder picked, and why. Hidden below md: at
              // that width the tabs matter more than the explanation.
              <Tooltip>
                <TooltipTrigger
                  render={
                    <Badge variant="outline" className="hidden max-w-56 md:inline-flex">
                      <span className="truncate">
                        {models[route.agent] ?? AGENT_LABELS[route.agent as Agent] ?? route.agent}
                      </span>
                    </Badge>
                  }
                />
                <TooltipContent>자동 선택: {route.reason}</TooltipContent>
              </Tooltip>
            )}
          </>
      }
    >
      <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
        {/* A phone shows one screen at a time, and the one thing that cannot
            wait is the request blocking the turn. Above the transcript, not
            inside it: the transcript is scrolled to its end, so anything
            placed at the top of it would be off screen. At md and up the
            cards stay in the flow, next to what they are about. */}
        {(questions.length > 0 || approvals.length > 0) && (
          <div className="flex max-h-[55vh] shrink-0 flex-col gap-4 overflow-y-auto border-b border-border px-4 py-3 md:hidden">
            {questions.map((request) => (
              <QuestionCard
                key={request.request_id}
                request={request}
                onAnswer={answerQuestion}
                connected={connected}
                connectionVersion={connectionVersion}
              />
            ))}
            {approvals.map((request) => (
              <ApprovalCard
                key={request.request_id}
                request={request}
                onAnswer={answerApproval}
                connected={connected}
                connectionVersion={connectionVersion}
              />
            ))}
          </div>
        )}

        <MessageScrollerProvider autoScroll>
          <MessageScroller className="min-h-0 flex-1">
            <MessageScrollerViewport>
              <MessageScrollerContent className="mx-auto w-full max-w-[68ch] gap-7 px-4 pt-6 pb-12 leading-relaxed">
                {/* A fresh conversation used to be a blank rectangle above
                    the composer, which says nothing about what this thing
                    does or what it may do on your behalf. */}
                {/* A transcript being fetched, not a conversation that is
                    empty: the empty state used to flash on every switch, which
                    says the wrong thing about a workspace with a hundred
                    turns in it. */}
                {loadingHistory && messages.length === 0 && (
                  <MessageScrollerItem messageId="loading">
                    <div className="mx-auto flex w-full max-w-[68ch] flex-col gap-6 py-6">
                      <div className="flex flex-col items-end gap-2">
                        <Skeleton className="h-9 w-2/5 rounded-xl" />
                      </div>
                      <div className="flex flex-col gap-2">
                        <Skeleton className="h-3 w-24" />
                        <Skeleton className="h-4 w-full" />
                        <Skeleton className="h-4 w-11/12" />
                        <Skeleton className="h-4 w-3/4" />
                      </div>
                      <div className="flex flex-col items-end gap-2">
                        <Skeleton className="h-9 w-1/3 rounded-xl" />
                      </div>
                      <div className="flex flex-col gap-2">
                        <Skeleton className="h-3 w-20" />
                        <Skeleton className="h-4 w-10/12" />
                        <Skeleton className="h-4 w-2/3" />
                      </div>
                    </div>
                  </MessageScrollerItem>
                )}

                {!loadingHistory && messages.length === 0 && (
                  <MessageScrollerItem messageId="empty">
                    <Empty className="py-16">
                      <EmptyHeader>
                        <EmptyMedia variant="icon">
                          <NetworkIcon />
                        </EmptyMedia>
                        <EmptyTitle>
                          {workspaceName ? `${workspaceName}에서 시작` : "무엇을 해볼까요"}
                        </EmptyTitle>
                        <EmptyDescription>
                          요청을 적으면 대장 에이전트가 판단합니다. 아는 것으로 답할 수 있으면
                          바로 답하고, 기계를 건드려야 하면 작업으로 나눠 하위 에이전트에게
                          맡깁니다. 파일을 바꾸는 작업은 각자 격리된 git 워크트리에서 돌고,
                          위험한 명령은 실행 전에 승인을 받습니다.
                        </EmptyDescription>
                      </EmptyHeader>
                      <EmptyContent>
                        <div className="flex flex-col gap-1.5">
                          {STARTERS.map((starter) => (
                            <Button
                              key={starter}
                              variant="outline"
                              size="sm"
                              className="h-auto justify-start whitespace-normal py-1.5 text-left"
                              onClick={() => composerRef.current?.fill(starter)}
                            >
                              {starter}
                            </Button>
                          ))}
                        </div>
                      </EmptyContent>
                    </Empty>
                  </MessageScrollerItem>
                )}

                {/* No "handoff" divider: which CLI answered is already on the
                    reply's header, and the extra line said nothing more. */}
                {/* The page above the one on screen. A conversation opens on
                    its last dozen turns — the rest is a click, not a wait. */}
                {hasOlderTurns && (
                  <MessageScrollerItem messageId="older">
                    <div className="flex justify-center">
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        disabled={loadingOlder}
                        onClick={() => void loadOlder()}
                      >
                        {loadingOlder ? "불러오는 중…" : "이전 대화 더 보기"}
                      </Button>
                    </div>
                  </MessageScrollerItem>
                )}

                {messages.map((message, index) => (
                  <MessageScrollerItem
                    key={index}
                    messageId={`m-${index}`}
                    // No scrollAnchor: anchoring the user's message to the top
                    // of the viewport jumps the view on every send, and with
                    // full-width replies the jump landed somewhere in the
                    // middle of the previous answer. Following the bottom while
                    // a reply streams is the behavior people expect, and the
                    // jump-to-latest button covers the rest.
                  >
                    {message.role === "user" ? (
                      // One reading line down the page: what you asked and
                      // what came back are the same column, told apart by the
                      // label above each. A right-aligned bubble split the
                      // transcript into two columns, which is a chat app's
                      // shape, not a record of work.
                      <div className="flex min-w-0 flex-col gap-1">
                        <div className="flex items-center gap-2 text-xs tracking-wide text-muted-foreground">
                          <span>나</span>
                          {message.at ? (
                            <Tooltip>
                              <TooltipTrigger
                                render={
                                  <span className="tabular-nums">· {clockTime(message.at)}</span>
                                }
                              />
                              <TooltipContent>{timeExact(message.at)}</TooltipContent>
                            </Tooltip>
                          ) : null}
                        </div>
                        {/* The user's own text is not markdown; showing it
                            verbatim keeps their formatting intact. */}
                        <p className="min-w-0 font-medium whitespace-pre-wrap">{message.text}</p>
                      </div>
                    ) : (
                      // The agent's reply is not a chat message, it is the
                      // document you asked for: a bubble put a hundred-line
                      // answer with tables and code inside a tinted box with a
                      // width of its own, and made long-form output look like
                      // an aside. Full width, plain body text.
                      <div className="flex min-w-0 flex-col gap-2">
                        {message.reasoning && (
                          <Reasoning
                            text={message.reasoning}
                            streaming={streaming && index === messages.length - 1}
                          />
                        )}
                        {/* Who answered, as a quiet line above the text rather
                            than a label attached to a box. */}
                        {/* Who answered and when it landed. With the prompt's
                            own time above, the pair says how long the turn
                            took once the live counter is gone. */}
                        <div className="flex flex-wrap items-center gap-2 text-xs tracking-wide text-muted-foreground">
                          <span>
                            {message.model ??
                              models[message.agent] ??
                              AGENT_LABELS[message.agent]}
                          </span>
                          {message.at ? (
                            <Tooltip>
                              <TooltipTrigger
                                render={
                                  <span className="tabular-nums">
                                    {clockTime(message.at)}
                                    {(() => {
                                      const asked = messages[index - 1];
                                      if (asked?.role !== "user" || !asked.at) return "";
                                      return ` · ${duration(message.at - asked.at)}`;
                                    })()}
                                  </span>
                                }
                              />
                              <TooltipContent>{timeExact(message.at)}</TooltipContent>
                            </Tooltip>
                          ) : null}
                          <Button
                            variant="ghost"
                            size="xs"
                            className="ml-auto"
                            disabled={!message.text.trim()}
                            onClick={() => void copyResponse(message.text)}
                            aria-label="응답 복사"
                            title="응답 복사"
                          >
                            <CopyIcon data-icon="inline-start" />
                            복사
                          </Button>
                        </div>
                        <BlockList
                          loadFullInput={fullInputLoader(message.turnId)}
                          loadRun={runLoader(message.turnId)}
                          blocks={
                            message.blocks ?? [{ kind: "text" as const, text: message.text }]
                          }
                          // The turn in flight keeps its tool card open, so you
                          // can watch what the agent is doing.
                          defaultToolsOpen={
                            streaming && index === messages.length - 1 ? true : undefined
                          }
                        />
                        {message.plan && (
                          <PlanGraph
                            plan={message.plan}
                            onSelectTask={(task) => {
                              setOpenTaskTurnId(message.turnId);
                              setOpenTask(task);
                            }}
                            defaultOpen={false}
                          />
                        )}
                      </div>
                    )}
                  </MessageScrollerItem>
                ))}

                {/* The current turn's fan-out, above the approvals it may
                    still be waiting on. */}
                {planOutcome && !plan && (
                  // A Plan toggle that silently changes nothing looks broken:
                  // say that the lead planned and chose to answer alone.
                  <MessageScrollerItem messageId="plan-outcome">
                    <Marker variant="separator">
                      <MarkerContent>
                        {planOutcome.mode === "execute" ? "대장이 직접 작업합니다" : "대장이 직접 답합니다"} · 하위 에이전트 없음{planOutcome.note ? ` (${planOutcome.note})` : ""}
                      </MarkerContent>
                    </Marker>
                  </MessageScrollerItem>
                )}

                {plan && (
                  <MessageScrollerItem messageId="plan">
                    <PlanGraph
                      plan={plan}
                      onSelectTask={(task) => {
                        setOpenTaskTurnId(undefined);
                        setOpenTask(task);
                      }}
                      autoCollapse={leadAnswering && !plan.tasks.some((task) => task.status === "running")}
                    />
                  </MessageScrollerItem>
                )}

                {/* Something is happening, and this is the only thing on screen
                    that says so while the lead plans, the workers run, or the
                    answer is being written but has not started arriving. */}
                {streaming && status && (
                  <MessageScrollerItem messageId="status">
                    <TurnStatusLine
                      status={status}
                      model={models[status.agent] ?? undefined}
                    />
                  </MessageScrollerItem>
                )}

                {/* The agent's own questions and the gate's approval requests
                    render after the transcript: both are what the conversation
                    is currently blocked on. Questions first — an approval is
                    about one command, a question is about what to build. */}
                {/* Hidden on a phone, where the same requests are pinned
                    above the transcript instead — see below. */}
                {questions.map((request) => (
                  <MessageScrollerItem
                    key={request.request_id}
                    messageId={request.request_id}
                    className="hidden min-w-0 md:block"
                  >
                    <QuestionCard request={request} onAnswer={answerQuestion} connected={connected} connectionVersion={connectionVersion} />
                  </MessageScrollerItem>
                ))}

                {approvals.map((request) => (
                  <MessageScrollerItem
                    key={request.request_id}
                    messageId={request.request_id}
                    className="hidden min-w-0 md:block"
                  >
                    <ApprovalCard request={request} onAnswer={answerApproval} connected={connected} connectionVersion={connectionVersion} />
                  </MessageScrollerItem>
                ))}
              </MessageScrollerContent>
            </MessageScrollerViewport>
            <MessageScrollerButton />
          </MessageScroller>
        </MessageScrollerProvider>

        {/* Outside the scroll area and never shrinking, so the composer stays
            on screen no matter how long the transcript gets. */}
        <Composer
          ref={composerRef}
          // Always "auto": the routing ladder picks the CLI per message.
          onSubmit={(text) => sendMessage(text, "auto")}
          onCancel={cancel}
          streaming={streaming}
          connected={connected}
          placeholder="무엇을 해야 하는지 적으세요. Enter로 전송"
        />

      </div>
    </PageShell>

      {/* One sub-agent's own record: what it was told, what it ran, what it
          answered. Opened from the plan it belongs to, and closed with Esc. */}
      <Sheet
        open={shownTask !== null}
        onOpenChange={(open) => { if (!open) setOpenTask(null); }}
      >
        <SheetContent className="w-full overflow-y-auto sm:max-w-xl">
          <SheetHeader>
            <SheetTitle className="pr-8">{shownTask?.title}</SheetTitle>
            <div className="flex flex-wrap items-center gap-1.5">
              {shownTask && <Badge variant="outline">{shownTask.agent}</Badge>}
              {shownTask?.model && shownTask.model !== shownTask.agent && (
                <Badge variant="outline" className="font-mono">
                  {shownTask.model}
                </Badge>
              )}
              {shownTask && (
                <Badge variant={shownTask.status === "error" || shownTask.status === "timeout" ? "destructive" : "secondary"}>
                  {TASK_STATUS_LABEL[shownTask.status]}
                </Badge>
              )}
              {shownTask?.scope && (
                <Badge variant="outline" title="대장이 이 하위 에이전트에게 준 권한 범위">
                  {shownTask.scope.mode === "read" ? "읽기 전용" : "쓰기 가능"}
                  {shownTask.scope.paths?.length ? ` · ${shownTask.scope.paths.join(", ")}` : ""}
                </Badge>
              )}
              {typeof shownTask?.tokens === "number" && (
                <Badge variant="outline">{shownTask.tokens.toLocaleString()} tok</Badge>
              )}
            </div>
          </SheetHeader>

          {shownTask && (
            // A conversation, because that is what it was: the lead sent this
            // worker an instruction and the worker answered. Read the same way
            // the main transcript is — the instruction as a message from the one
            // who gave it, the worker's own words as body text, its commands
            // where they happened — so nothing has to be learned twice.
            <div className="flex min-w-0 flex-col gap-4 px-4 pb-4">
              {shownTask.prompt && (
                <div className="flex min-w-0 flex-col gap-1">
                  <div className="text-xs tracking-wide text-muted-foreground">
                    대장 에이전트의 지시
                  </div>
                  <p className="min-w-0 font-medium whitespace-pre-wrap">{shownTask.prompt}</p>
                </div>
              )}

              <div className="flex min-w-0 flex-col gap-2">
                <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                  <span>
                    {shownTask.model ?? models[shownTask.agent] ?? shownTask.agent}
                  </span>
                </div>

                {/* What the worker said and ran, interleaved. A worker ANA gave
                    up on has no final answer, and this is the whole record of
                    what it did get done — which is exactly the run someone
                    opens this panel to read. */}
                {shownTask.blocks?.length ? (
                  <BlockList
                    blocks={shownTask.blocks}
                    loadFullInput={fullInputLoader(openTaskTurnId)}
                    loadRun={runLoader(openTaskTurnId)}
                    defaultToolsOpen={shownTask.status === "running" ? true : undefined}
                  />
                ) : shownTask.tools?.length ? (
                  // Older turns recorded the calls without their ordering.
                  <ToolCalls tools={shownTask.tools} />
                ) : null}

                {shownTask.output ? (
                  <Markdown>{shownTask.output}</Markdown>
                ) : (
                  <p className="text-sm text-muted-foreground">
                    {shownTask.error || "작업 결과를 기다리는 중…"}
                  </p>
                )}

                {shownTask.output && shownTask.error ? (
                  // Both: a worker that was cut off left work behind *and* a
                  // reason it stopped.
                  <p className="text-sm text-destructive">{shownTask.error}</p>
                ) : null}
              </div>
            </div>
          )}
        </SheetContent>
      </Sheet>
    </>
  );
}
