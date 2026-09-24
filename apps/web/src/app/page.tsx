"use client";

import { useEffect, useMemo, useState } from "react";
import { NetworkIcon } from "lucide-react";
import {
  useChatSocket,
  type MessageBlock,
  type SubagentTask,
  type ToolEvent,
} from "@/hooks/useChatSocket";
import {
  AGENT_CHOICES,
  AGENT_LABELS,
  AgentToggle,
  type Agent,
  type AgentChoice,
} from "@/components/ui/agent-toggle";
import { ApprovalCard } from "@/components/ui/approval-card";
import { QuestionCard } from "@/components/ui/question-card";
import { Markdown } from "@/components/markdown";
import { ThemeToggle } from "@/components/theme";
import { ToolCalls } from "@/components/tool-calls";
import { UsageBar } from "@/components/usage-bar";
import { PlanGraph } from "@/components/plan-graph";
import { Reasoning } from "@/components/reasoning";
import { SessionSidebar } from "@/components/session-sidebar";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Empty, EmptyContent, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty";
import { Separator } from "@/components/ui/separator";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Bubble, BubbleContent } from "@/components/ui/bubble";
import { Composer } from "@/components/composer";
import { Marker, MarkerContent } from "@/components/ui/marker";
import { Message, MessageContent } from "@/components/ui/message";
import {
  MessageScroller,
  MessageScrollerButton,
  MessageScrollerContent,
  MessageScrollerItem,
  MessageScrollerProvider,
  MessageScrollerViewport,
} from "@/components/ui/message-scroller";
import {
  SidebarProvider,
  SidebarTrigger,
} from "@/components/ui/sidebar";

const AGENT_STORAGE_KEY = "ana_agent";

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

/** A reply's blocks with runs of consecutive tool calls merged into one group.
 *
 *  The socket records one block per call, in the order things happened, which is
 *  what keeps a command sitting between the two things the agent said around it.
 *  Rendering that list one-block-at-a-time gave a separate card per call, each
 *  headed "도구 1회" — a turn that ran eight commands looked like eight
 *  one-command turns. Grouping only merges neighbours, so the ordering that
 *  matters is untouched. */
function groupBlocks(
  blocks: MessageBlock[]
): ({ kind: "text"; text: string } | { kind: "tools"; tools: ToolEvent[] })[] {
  const grouped: ({ kind: "text"; text: string } | { kind: "tools"; tools: ToolEvent[] })[] = [];
  for (const block of blocks) {
    if (block.kind === "text") {
      grouped.push(block);
      continue;
    }
    const last = grouped[grouped.length - 1];
    if (last?.kind === "tools") last.tools.push(block.tool);
    else grouped.push({ kind: "tools", tools: [block.tool] });
  }
  return grouped;
}

function isAgentChoice(value: string | null): value is AgentChoice {
  return value !== null && (AGENT_CHOICES as readonly string[]).includes(value);
}

export default function ChatPage() {
  const {
    messages,
    connected,
    streaming,
    usage,
    workspace,
    workspaceName,
    threadId,
    approvals,
    questions,
    plan,
    planOutcome,
    models,
    route,
    sendMessage,
    cancel,
    answerApproval,
    answerQuestion,
    openThread,
  } = useChatSocket();
  const [draft, setDraft] = useState("");
  // Auto by default: the routing rules pick per message, and the manual
  // options stay as an override.
  const [agent, setAgent] = useState<AgentChoice>("auto");
  // The sub-agent whose own transcript is open, if any. A panel over the
  // conversation rather than a replacement for it: the lead's reply is the
  // context that makes a worker's output mean anything, so leaving the
  // transcript on screen is the point.
  const [openTask, setOpenTask] = useState<SubagentTask | null>(null);

  // Read on mount rather than in useState's initializer: this component
  // renders on the server first (Next app router), where localStorage does
  // not exist, and an initializer that read it would produce a hydration
  // mismatch.
  useEffect(() => {
    const stored = window.localStorage.getItem(AGENT_STORAGE_KEY);
    if (isAgentChoice(stored)) setAgent(stored);
  }, []);

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

  const handleAgentChange = (next: AgentChoice) => {
    setAgent(next);
    window.localStorage.setItem(AGENT_STORAGE_KEY, next);
  };

  return (
    // h-svh + overflow-hidden bounds the transcript so it scrolls while the
    // composer and status bar remain visible at the bottom of the window.
    <SidebarProvider className="h-svh overflow-hidden">
      <SessionSidebar
        workspaceId={threadId}
        workspace={workspace}
        workspaceName={workspaceName}
        onOpenWorkspace={(nextThreadId) => {
          setOpenTask(null);
          openThread(nextThreadId);
        }}
        reloadKey={completedTurns}
      />

      <main className="flex h-full min-h-0 min-w-0 flex-1 flex-col overflow-hidden bg-background">
        {/* One line at every width: which workspace you are in, where it is on
            disk, whether the socket is up, and the two controls that change the
            next turn. Reading one sub-agent's transcript used to be a Select
            here — a picker in the title bar, listing tasks by turn number,
            that swapped the whole conversation out for one worker's output.
            That now happens where the plan is drawn. */}
        <header className="flex h-12 shrink-0 items-center gap-2 border-b border-border px-3">
          <SidebarTrigger />
          <Separator orientation="vertical" className="h-4" />
          <div className="flex min-w-0 items-baseline gap-2">
            <span className="truncate text-sm font-medium">
              {workspaceName ?? "새 대화"}
            </span>
            {workspace && (
              <span
                className="hidden truncate font-mono text-[11px] text-muted-foreground lg:inline"
                title={workspace}
              >
                {workspace}
              </span>
            )}
          </div>

          {connected ? (
            <Tooltip>
              <TooltipTrigger
                render={
                  <span
                    aria-label="연결됨"
                    className="size-1.5 shrink-0 rounded-full bg-primary"
                  />
                }
              />
              <TooltipContent>서버에 연결됨</TooltipContent>
            </Tooltip>
          ) : (
            <Badge variant="destructive">연결 끊김</Badge>
          )}

          {route && (
            // Which CLI the ladder picked, and why. Hidden below md: at that
            // width the controls matter more than the explanation.
            <Tooltip>
              <TooltipTrigger
                render={
                  <Badge variant="outline" className="ml-1 hidden max-w-56 md:inline-flex">
                    <span className="truncate">
                      {models[route.agent] ?? AGENT_LABELS[route.agent as Agent] ?? route.agent}
                    </span>
                  </Badge>
                }
              />
              <TooltipContent>자동 선택: {route.reason}</TooltipContent>
            </Tooltip>
          )}

          <div className="ml-auto flex items-center gap-1">
            <AgentToggle value={agent} onChange={handleAgentChange} />
            <ThemeToggle />
          </div>
        </header>

        <MessageScrollerProvider autoScroll>
          <MessageScroller className="min-h-0 flex-1">
            <MessageScrollerViewport>
              <MessageScrollerContent className="mx-auto w-full max-w-3xl gap-4 p-4">
                {/* A fresh conversation used to be a blank rectangle above
                    the composer, which says nothing about what this thing
                    does or what it may do on your behalf. */}
                {messages.length === 0 && (
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
                              onClick={() => setDraft(starter)}
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
                    {message.notice ? (
                      // A failover notice is the system talking, not the
                      // agent — a marker, not a bubble.
                      <Marker variant="separator">
                        <MarkerContent>{message.text}</MarkerContent>
                      </Marker>
                    ) : message.role === "user" ? (
                      // What you said is a message in a conversation: short,
                      // attributable, and worth setting apart from the page.
                      <Message align="end">
                        <MessageContent>
                          <Bubble align="end" variant="default">
                            <BubbleContent>
                              {/* The user's own text is not markdown; showing
                                  it verbatim keeps their formatting intact. */}
                              <span className="whitespace-pre-wrap">{message.text}</span>
                            </BubbleContent>
                          </Bubble>
                        </MessageContent>
                      </Message>
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
                        <div className="text-xs text-muted-foreground">
                          {message.model ??
                            models[message.agent] ??
                            AGENT_LABELS[message.agent]}
                        </div>
                        {groupBlocks(
                          message.blocks ?? [{ kind: "text" as const, text: message.text }]
                        ).map((block, blockIndex) =>
                          block.kind === "text" ? (
                            block.text.trim().length > 0 && (
                              <Markdown key={blockIndex}>{block.text}</Markdown>
                            )
                          ) : (
                            <ToolCalls
                              key={blockIndex}
                              tools={block.tools}
                              // The turn in flight keeps its card open, so you
                              // can watch what the agent is doing.
                              defaultOpen={
                                streaming && index === messages.length - 1 ? true : undefined
                              }
                            />
                          )
                        )}
                        {message.plan && (
                          <PlanGraph
                            plan={message.plan}
                            onSelectTask={setOpenTask}
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
                        대장이 직접 답했습니다 · 하위 에이전트 없음{planOutcome.note ? ` (${planOutcome.note})` : ""}
                      </MarkerContent>
                    </Marker>
                  </MessageScrollerItem>
                )}

                {plan && (
                  <MessageScrollerItem messageId="plan">
                    <PlanGraph
                      plan={plan}
                      onSelectTask={setOpenTask}
                      autoCollapse={leadAnswering}
                    />
                  </MessageScrollerItem>
                )}

                {/* The agent's own questions and the gate's approval requests
                    render after the transcript: both are what the conversation
                    is currently blocked on. Questions first — an approval is
                    about one command, a question is about what to build. */}
                {questions.map((request) => (
                  <MessageScrollerItem
                    key={request.request_id}
                    messageId={request.request_id}
                    className="min-w-0"
                  >
                    <QuestionCard request={request} onAnswer={answerQuestion} />
                  </MessageScrollerItem>
                ))}

                {approvals.map((request) => (
                  <MessageScrollerItem
                    key={request.request_id}
                    messageId={request.request_id}
                    className="min-w-0"
                  >
                    <ApprovalCard request={request} onAnswer={answerApproval} />
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
          value={draft}
          onChange={setDraft}
          onSubmit={() => {
            sendMessage(draft, agent);
            setDraft("");
          }}
          onCancel={cancel}
          streaming={streaming}
          connected={connected}
          placeholder={
            agent === "auto"
              ? "무엇을 해야 하는지 적으세요. Enter로 전송"
              : `${AGENT_LABELS[agent]}에게 보낼 요청`
          }
        />

        <UsageBar
          refreshKey={completedTurns}
          totalCostUsd={usage.totalCostUsd}
          totalTokens={usage.totalTokens}
        />
      </main>

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
            <div className="flex min-w-0 flex-col gap-3 px-4 pb-4">
              {shownTask.prompt && (
                <section className="flex min-w-0 flex-col gap-1">
                  <h3 className="text-xs font-medium text-muted-foreground">받은 지시</h3>
                  {/* The instruction stays in a box: it is quoted material, not
                      this panel's own text. */}
                  <div className="rounded-md border border-border bg-muted/50 px-3 py-2">
                    <Markdown>{shownTask.prompt}</Markdown>
                  </div>
                </section>
              )}

              {shownTask.tools?.length ? (
                <section className="flex min-w-0 flex-col gap-1">
                  <h3 className="text-xs font-medium text-muted-foreground">
                    도구 호출 {shownTask.tools.length}건
                  </h3>
                  <ToolCalls tools={shownTask.tools} />
                </section>
              ) : null}

              <section className="flex min-w-0 flex-col gap-1">
                <h3 className="text-xs font-medium text-muted-foreground">결과</h3>
                {shownTask.output ? (
                  // What the worker answered, as body text for the same reason
                  // the lead's reply is: it is the document, not an aside.
                  <Markdown>{shownTask.output}</Markdown>
                ) : (
                  <p className="text-sm text-muted-foreground">
                    {shownTask.error || "작업 결과를 기다리는 중…"}
                  </p>
                )}
              </section>
            </div>
          )}
        </SheetContent>
      </Sheet>
    </SidebarProvider>
  );
}
