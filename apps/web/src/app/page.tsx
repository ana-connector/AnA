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
  const [planMode, setPlanMode] = useState(true);
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
    sendMessage(draft, agent, planMode);
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
        <header className="flex h-12 shrink-0 items-center gap-2 border-b border-border px-3">
          <SidebarTrigger />
          <Badge variant={connected ? "secondary" : "destructive"}>
            {connected ? "connected" : "disconnected"}
          </Badge>
          <Select value={selectedView} onValueChange={(value) => setSelectedView(value ?? "lead")}>
            <SelectTrigger size="sm" aria-label="표시할 에이전트" className="max-w-52">
              <SelectValue placeholder="대장 Agent" />
            </SelectTrigger>
            <SelectContent>
              <SelectGroup>
                <SelectItem value="lead">대장 Agent</SelectItem>
                {taskChoices.map((choice) => (
                  <SelectItem key={choice.value} value={choice.value}>
                    하위 Agent · {choice.title}
                  </SelectItem>
                ))}
              </SelectGroup>
            </SelectContent>
          </Select>
          {/* Cost comes only from Claude turns (the Codex CLI reports tokens,
              not dollars), so both numbers are shown rather than one total. */}
          {route && (
            // Auto-routing is invisible otherwise: the user should be able to
            // see which CLI the rules picked, and why.
            <span className="text-xs text-muted-foreground">
              → {models[route.agent] ?? AGENT_LABELS[route.agent as Agent] ?? route.agent} ({route.reason})
            </span>
          )}
          <div className="ml-auto flex items-center gap-2">
            <AgentToggle value={agent} onChange={handleAgentChange} />
            <ThemeToggle />
          </div>
        </header>

        <MessageScrollerProvider autoScroll>
          <MessageScroller className="min-h-0 flex-1">
            <MessageScrollerViewport>
              <MessageScrollerContent className="mx-auto w-full max-w-3xl gap-4 p-4">
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
                        계획: 단독 처리{planOutcome.note ? ` (${planOutcome.note})` : ""}
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
            sendMessage(draft, agent, planMode);
            setDraft("");
          }}
          onCancel={cancel}
          streaming={streaming}
          connected={connected}
          planMode={planMode}
          onPlanModeChange={setPlanMode}
          placeholder={
            agent === "auto"
              ? "메시지… (규칙이 CLI를 고름, Enter로 전송)"
              : `${AGENT_LABELS[agent]}에게 메시지…`
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
