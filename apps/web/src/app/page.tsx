"use client";

import { useEffect, useMemo, useState, type FormEvent } from "react";
import { NetworkIcon } from "lucide-react";
import { useChatSocket } from "@/hooks/useChatSocket";
import {
  AGENT_CHOICES,
  AGENT_LABELS,
  AgentToggle,
  type Agent,
  type AgentChoice,
} from "@/components/ui/agent-toggle";
import { ApprovalCard } from "@/components/ui/approval-card";
import { Markdown } from "@/components/markdown";
import { ThemeToggle } from "@/components/theme";
import { ToolCalls } from "@/components/tool-calls";
import { UsageBar } from "@/components/usage-bar";
import { PlanGraph } from "@/components/plan-graph";
import { Reasoning } from "@/components/reasoning";
import { SessionSidebar } from "@/components/session-sidebar";
import { Badge } from "@/components/ui/badge";
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty";
import { Separator } from "@/components/ui/separator";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Bubble, BubbleContent } from "@/components/ui/bubble";
import { Composer } from "@/components/composer";
import { Marker, MarkerContent } from "@/components/ui/marker";
import { Toggle } from "@/components/ui/toggle";
import {
  Message,
  MessageContent,
  MessageHeader,
} from "@/components/ui/message";
import {
  MessageScroller,
  MessageScrollerButton,
  MessageScrollerContent,
  MessageScrollerItem,
  MessageScrollerProvider,
  MessageScrollerViewport,
} from "@/components/ui/message-scroller";
import {
  SidebarInset,
  SidebarProvider,
  SidebarTrigger,
} from "@/components/ui/sidebar";

const AGENT_STORAGE_KEY = "ana_agent";

function isAgentChoice(value: string | null): value is AgentChoice {
  return value !== null && (AGENT_CHOICES as readonly string[]).includes(value);
}

function formatTokens(tokens: number): string {
  if (tokens < 1000) return `${tokens} tok`;
  return `${(tokens / 1000).toFixed(1)}k tok`;
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
    plan,
    planOutcome,
    models,
    route,
    sendMessage,
    cancel,
    answerApproval,
    openThread,
  } = useChatSocket();
  const [draft, setDraft] = useState("");
  // Auto by default: the routing rules pick per message, and the manual
  // options stay as an override.
  const [agent, setAgent] = useState<AgentChoice>("auto");
  const [selectedView, setSelectedView] = useState("lead");

  const taskChoices = useMemo(() => {
    const choices: { value: string; title: string; task: NonNullable<typeof plan>["tasks"][number] }[] = [];
    const latestTurn = messages.filter((message) => message.role === "user").length;
    let turn = 0;
    for (const message of messages) {
      if (message.role === "user") turn += 1;
      for (const task of message.plan?.tasks ?? []) {
        choices.push({ value: `task:${turn}:${task.id}`, title: task.title, task });
      }
      const hasNativeTasks = Boolean(
        message.plan?.tasks.some((task) => task.id.startsWith("native:")) ||
        (turn === latestTurn && plan?.tasks.some((task) => task.id.startsWith("native:")))
      );
      if (message.role === "agent" && !hasNativeTasks) {
        for (const block of message.blocks ?? []) {
          if (block.kind !== "tool" || block.tool.tool_name.toLowerCase() !== "collaborationspawn_agent" || block.tool.verdict !== "allow") continue;
          const name = typeof block.tool.input.task_name === "string" ? block.tool.input.task_name : "하위 에이전트";
          const id = `spawn:${block.tool.tool_use_id ?? name}`;
          choices.push({
            value: `task:${turn}:${id}`,
            title: name,
            task: { id, title: name, agent: message.agent, status: streaming && turn === latestTurn ? "running" : "unknown" },
          });
        }
      }
    }
    if (plan) {
      for (const task of plan.tasks) {
        const value = `task:${turn}:${task.id}`;
        const index = choices.findIndex((choice) => choice.value === value);
        const choice = { value, title: task.title, task };
        if (index >= 0) choices[index] = choice;
        else choices.push(choice);
      }
    }
    return choices;
  }, [messages, plan, streaming]);
  const selectedTask = taskChoices.find((choice) => choice.value === selectedView)?.task;

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

  const handleAgentChange = (next: AgentChoice) => {
    setAgent(next);
    window.localStorage.setItem(AGENT_STORAGE_KEY, next);
  };

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault();
    if (!draft.trim()) return;
    sendMessage(draft, agent);
    setDraft("");
  };

  return (
    // h-svh + overflow-hidden here is what makes the transcript scroll: without
    // a bounded height the inset grows with the messages, the page itself
    // scrolls, and the composer is pushed off the bottom of the screen.
    // pb-2 keeps the inset card (and the status bar inside it) off the window
    // edge at every width — the inset variant only applies its own margin from
    // md up.
    <SidebarProvider className="h-svh overflow-hidden pb-2">
      <SessionSidebar
        workspaceId={threadId}
        workspace={workspace}
        workspaceName={workspaceName}
        onOpenWorkspace={(nextThreadId) => {
          setSelectedView("lead");
          openThread(nextThreadId);
        }}
        reloadKey={completedTurns}
      />

      <SidebarInset className="flex h-full min-h-0 flex-col overflow-hidden">
        {/* One line at every width: the workspace you are in, whether the
            socket is up, which transcript you are reading, and the two
            controls that change the next turn. The status used to be an
            always-on English badge and the route a bare arrow of text; both
            shouted at the same volume as the things you can act on. */}
        <header className="flex h-12 shrink-0 items-center gap-2 border-b border-border px-3">
          <SidebarTrigger />
          <Separator orientation="vertical" className="h-4" />
          <span className="truncate text-sm font-medium">
            {workspaceName ?? "새 대화"}
          </span>

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

          {taskChoices.length > 0 && (
            <Select
              value={selectedView}
              onValueChange={(value) => setSelectedView(value ?? "lead")}
            >
              <SelectTrigger size="sm" aria-label="표시할 에이전트" className="ml-1 max-w-48">
                <SelectValue placeholder="대장 Agent" />
              </SelectTrigger>
              <SelectContent>
                <SelectGroup>
                  <SelectItem value="lead">대장 Agent</SelectItem>
                  {taskChoices.map((choice) => (
                    <SelectItem key={choice.value} value={choice.value}>
                      하위 · {choice.title}
                    </SelectItem>
                  ))}
                </SelectGroup>
              </SelectContent>
            </Select>
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
                {!selectedTask && messages.length === 0 && (
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
                    </Empty>
                  </MessageScrollerItem>
                )}

                {/* No "handoff" divider: which CLI answered is already on the
                    reply's header, and the extra line said nothing more. */}
                {selectedTask ? (
                  <MessageScrollerItem messageId={selectedView}>
                    <Message align="start">
                      <MessageContent>
                        <MessageHeader>{selectedTask.agent} · {selectedTask.title}</MessageHeader>
                        <Badge variant="outline">{selectedTask.status === "running" ? "실행 중" : selectedTask.status === "done" ? "완료" : selectedTask.status === "timeout" ? "시간 초과" : selectedTask.status === "unknown" ? "상태 확인 불가" : "실패"}</Badge>
                        {selectedTask.prompt ? <Bubble align="start" variant="muted"><BubbleContent><Markdown>{selectedTask.prompt}</Markdown></BubbleContent></Bubble> : null}
                        {selectedTask.tools?.length ? <ToolCalls tools={selectedTask.tools} /> : null}
                        {selectedTask.output ? (
                          <Bubble align="start" variant="muted"><BubbleContent><Markdown>{selectedTask.output}</Markdown></BubbleContent></Bubble>
                        ) : (
                          <p className="text-sm text-muted-foreground">{selectedTask.error || "작업 결과를 기다리는 중…"}</p>
                        )}
                      </MessageContent>
                    </Message>
                  </MessageScrollerItem>
                ) : messages.map((message, index) => {
                  // A turn whose agent differs from the previous turn's is
                  // where the server built a handoff brief for the incoming
                  // CLI; mark it so the switch is visible in the transcript.
                  return (
                    <MessageScrollerItem
                      key={index}
                      messageId={`m-${index}`}
                      scrollAnchor={message.role === "user"}
                    >
                      {message.notice ? (
                        // A failover notice is the system talking, not the
                        // agent — a marker, not a bubble.
                        <Marker variant="separator">
                          <MarkerContent>{message.text}</MarkerContent>
                        </Marker>
                      ) : (
                        <Message align={message.role === "user" ? "end" : "start"}>
                          <MessageContent>
                            {message.reasoning && (
                              <Reasoning
                                text={message.reasoning}
                                streaming={streaming && index === messages.length - 1}
                              />
                            )}
                            {message.role === "agent" && (
                              <MessageHeader>
                                {/* The model that answered, when the CLI said
                                    which — the product name matters less. */}
                                {message.model ??
                                  models[message.agent] ??
                                  AGENT_LABELS[message.agent]}
                              </MessageHeader>
                            )}
                            {message.role === "user" ? (
                              <Bubble align="end" variant="default">
                                <BubbleContent>
                                  {/* The user's own text is not markdown;
                                      showing it verbatim keeps their
                                      formatting intact. */}
                                  <span className="whitespace-pre-wrap">{message.text}</span>
                                </BubbleContent>
                              </Bubble>
                            ) : (
                              // Text and tool calls in the order they happened,
                              // so a command sits between the two things the
                              // agent said around it.
                              (
                                message.blocks ?? [{ kind: "text" as const, text: message.text }]
                              ).map((block, blockIndex) =>
                                block.kind === "text" ? (
                                  block.text.trim().length > 0 && (
                                    <Bubble key={blockIndex} align="start" variant="muted">
                                      <BubbleContent>
                                        <Markdown>{block.text}</Markdown>
                                      </BubbleContent>
                                    </Bubble>
                                  )
                                ) : (
                                  <ToolCalls key={blockIndex} tools={[block.tool]} />
                                )
                              )
                            )}
                            {message.plan && <PlanGraph plan={message.plan} />}
                          </MessageContent>
                        </Message>
                      )}
                    </MessageScrollerItem>
                  );
                })}

                {/* The current turn's fan-out, above the approvals it may
                    still be waiting on. */}
                {selectedView === "lead" && planOutcome && !plan && (
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

                {selectedView === "lead" && plan && (
                  <MessageScrollerItem messageId="plan">
                    <PlanGraph plan={plan} />
                  </MessageScrollerItem>
                )}

                {/* Gate requests render after the transcript: they are what the
                    conversation is currently blocked on. */}
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
      </SidebarInset>
    </SidebarProvider>
  );
}
