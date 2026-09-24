"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { AGENTS, type Agent } from "@/components/ui/agent-toggle";
import { fetchThreadHistory, wsBase } from "@/lib/api";

export type ChatEvent =
  // The backend's call_lead_agent (apps/server/app/graph.py) only writes
  // the full session_id/is_error/agent payload for non-"text" events — a
  // "text" chunk (a content delta appended as-is) is always just {type, text}.
  | { type: "text"; text: string }
  | {
      type:
        | "result"
        | "usage"
        | "error"
        | "thread_id"
        | "cancelled"
        | "approval_request"
        | "question"
        | "status"
        | "replay"
        | "prompt"
        | "failover"
        | "plan"
        | "subagent"
        | "route"
        | "model"
        | "tool"
        | "reasoning";
      text: string;
      session_id: string | null;
      is_error: boolean;
      agent?: Agent;
      usage?: Record<string, number>;
      workspace?: string;
      workspace_id?: string;
      workspace_name?: string;
      from_rung?: string;
      to_rung?: string;
      mode?: string;
      tasks?: {
        id: string;
        title: string;
        agent: string;
        prompt?: string;
        scope?: { mode: string; paths: string[] };
        needs?: string[];
      }[];
      task?: SubagentTask;
      questions?: AgentQuestion[];
      count?: number;
      /** Frames of a turn that ran while this browser was away. */
      frames?: ChatEvent[];
      /** Whether the conversation being opened is mid-turn right now. */
      running?: boolean;
      role?: string;
      request_id?: string;
      rule_id?: string | null;
      tool?: ToolEvent;
      cost_usd?: number | null;
      total_cost_usd?: number;
      turn_tokens?: number;
      total_tokens?: number;
    };

/** A reply is a sequence of what the agent said and what it did, in the order
 *  it happened: text, then a tool call, then more text is exactly how an agent
 *  works, and flattening that into "answer plus a list of tools" loses which
 *  command belonged to which step. */
export type MessageBlock =
  | { kind: "text"; text: string }
  | { kind: "tool"; tool: ToolEvent };

export type ChatMessage = {
  role: "user" | "agent";
  /** The reply as one string — what history restore has, and what a copy of
   *  the message should contain. `blocks` carries the ordering. */
  text: string;
  /** Which CLI this message was sent to (user) or produced by (agent). */
  agent: Agent;
  /** Set on notices the server sent (a failover switch), not on chat content. */
  notice?: boolean;
  /** The model that actually answered, when the CLI reported one. */
  model?: string;
  blocks?: MessageBlock[];
  /** The agent's thinking, when the CLI reported any. */
  reasoning?: string;
  /** The sub-agent fan-out this turn ran, restored from history too — a
   *  reopened conversation should show the work, not just the answer. */
  plan?: PlanState;
};

/** A tool call the governance gate is holding until the user answers. */
export type ApprovalRequest = {
  request_id: string;
  workspace?: string | null;
  tool_name: string;
  tool_input: Record<string, unknown>;
  /** What the matched rule says this is about, for the person answering. */
  description?: string;
  rule_id: string | null;
  category: string | null;
  agent: Agent | null;
  cwd: string | null;
};

/** One question the agent asked the user, waiting for an answer.
 *
 *  The counterpart of an approval: an approval is the machine asking permission
 *  for something it already decided to do, a question is the agent admitting it
 *  cannot decide without the person. Both reach the browser over this socket
 *  and block the turn until they are answered. */
export type AgentQuestion = {
  question: string;
  /** Choices to pick from. Empty means a free-text answer. */
  options: string[];
  /** Whether several options may be picked at once. */
  multi: boolean;
  /** Why it is being asked, or what the options imply. */
  note: string;
};

export type QuestionRequest = {
  request_id: string;
  questions: AgentQuestion[];
  agent: string | null;
  role: string;
};

/** One planned sub-task and how it is going. The lead plans them; the
 *  supervisor runs them with a concurrency cap. */
export type SubagentTask = {
  id: string;
  title: string;
  prompt?: string;
  agent: string;
  /** The permission scope the lead gave this sub-agent, enforced at the gate
   *  (packages/governance/subagent_scope.py). Absent means unrestricted. */
  scope?: { mode: string; paths: string[] };
  /** Task ids this one waited for. Empty means it started immediately. */
  needs?: string[];
  /** The model that answered this task, as its CLI reported it (or the one the
   *  ladder asked for). A plan routes its tasks to different models on purpose,
   *  so "codex" alone does not say what ran. */
  model?: string;
  status: "running" | "done" | "error" | "timeout" | "blocked" | "unknown";
  error?: string;
  output?: string;
  cost_usd?: number;
  tokens?: number;
  tools?: ToolEvent[];
};

/** One tool call as the governance gate saw it. */
export type ToolEvent = {
  /** The CLI's id for this call. A call that needs approval is reported twice
   *  (pending, then the decision); the list updates that entry instead of
   *  showing the same command twice. */
  tool_use_id?: string | null;
  tool_name: string;
  input: Record<string, unknown>;
  verdict: string;
  decided_by: string;
  rule_id: string | null;
  category: string | null;
  agent_id?: string | null;
};

export type PlanState = {
  mode: string;
  note: string;
  tasks: SubagentTask[];
};

/** Which phase the turn in flight is in, as the server reports it.
 *
 *  A turn can spend a minute planning and another few running workers before a
 *  single character of the answer exists. Without this the screen says nothing
 *  for that whole time, which is indistinguishable from a broken connection. */
export type TurnStatus = {
  /** "thinking" is the browser's own: the gap between pressing Enter and the
   *  server's first frame, before anyone knows which phase this turn will take. */
  phase:
    | "thinking"
    | "planning"
    | "delegating"
    | "replanning"
    | "synthesizing"
    | "answering";
  agent: string;
  count: number;
  /** When this phase started, for the elapsed counter. */
  since: number;
};

/** What the planning call decided, kept even when it decided *not* to split:
 *  a Plan toggle that silently changes nothing looks broken. */
export type PlanOutcome = {
  mode: string;
  note: string;
  taskCount: number;
};

export type UsageTotals = {
  /** Only the Claude CLI reports a dollar figure; Codex turns add nothing. */
  totalCostUsd: number;
  /** Both CLIs report tokens, so this covers the whole thread. */
  totalTokens: number;
};

const THREAD_STORAGE_KEY = "ana_thread_id";

function appendText(blocks: MessageBlock[], text: string): MessageBlock[] {
  const last = blocks[blocks.length - 1];
  if (last?.kind === "text") {
    // Streaming deltas extend the current paragraph rather than starting a new
    // block per chunk.
    return [...blocks.slice(0, -1), { kind: "text", text: last.text + text }];
  }
  return [...blocks, { kind: "text", text }];
}

function appendTool(blocks: MessageBlock[], tool: ToolEvent): MessageBlock[] {
  // A call that needed approval is reported twice (pending, then the
  // decision): update that block in place instead of adding a second one.
  const index = tool.tool_use_id
    ? blocks.findIndex((b) => b.kind === "tool" && b.tool.tool_use_id === tool.tool_use_id)
    : -1;
  if (index === -1) return [...blocks, { kind: "tool", tool }];
  const next = [...blocks];
  next[index] = { kind: "tool", tool };
  return next;
}

function isAgentName(value: string): value is Agent {
  return (AGENTS as readonly string[]).includes(value);
}

export function useChatSocket() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [connected, setConnected] = useState(false);
  const [streaming, setStreaming] = useState(false);
  const [usage, setUsage] = useState<UsageTotals>({ totalCostUsd: 0, totalTokens: 0 });
  const [approvals, setApprovals] = useState<ApprovalRequest[]>([]);
  // Questions the agent is waiting on. Plural because one call can ask several,
  // and a second call can arrive while the first is unanswered.
  const [questions, setQuestions] = useState<QuestionRequest[]>([]);
  // The current turn's fan-out, or null when the turn is running solo.
  const [plan, setPlan] = useState<PlanState | null>(null);
  const planRef = useRef<PlanState | null>(null);
  const [planOutcome, setPlanOutcome] = useState<PlanOutcome | null>(null);
  // Which model each CLI is running right now, as reported by the CLI or
  // learned from a hook payload.
  const [models, setModels] = useState<Record<string, string>>({});
  const [status, setStatus] = useState<TurnStatus | null>(null);
  // How the current turn was routed, when the rules chose.
  const [route, setRoute] = useState<{ agent: string; reason: string } | null>(null);
  // The directory this conversation works in, and which conversation it is.
  // Both come from the server's first frame.
  const [workspace, setWorkspace] = useState<string | null>(null);
  const [threadId, setThreadId] = useState<string | null>(null);
  // A workspace owns exactly one conversation, so its name is the name of the
  // conversation too.
  const [workspaceName, setWorkspaceName] = useState<string | null>(null);
  // What the next connection should ask for: a specific thread, or null to let
  // the server mint one. Changing it is what switches conversations.
  const [requestedThreadId, setRequestedThreadId] = useState<string | null | undefined>(
    undefined
  );

  const socketRef = useRef<WebSocket | null>(null);
  // Total number of messages appended so far, kept in sync by hand next to
  // every setMessages call that appends one. React state (`messages`) can't
  // serve this purpose here: it's only guaranteed current after a render,
  // but onmessage needs the *next* index synchronously, before that render
  // happens, to record where the in-progress agent message lives.
  const messageCountRef = useRef(0);
  // Index of the in-progress agent message in `messages`, or null when no
  // agent turn is currently streaming. Both this and messageCountRef above
  // are read and written from the onmessage handler itself, never from
  // inside a setMessages updater: React requires updaters to be pure, and
  // React 18+ StrictMode invokes them twice in development specifically to
  // surface impure updaters.
  const agentMessageIndexRef = useRef<number | null>(null);
  // Where the turn in flight starts in `messages` (the index of its user
  // message), so a replay can discard what this browser rendered for that turn
  // and rebuild it from the server's own buffer. Without it a reconnect
  // mid-turn — a wifi blip, a laptop waking up — would append the replayed
  // prompt and text on top of the copy already on screen.
  const turnStartRef = useRef<number | null>(null);
  // Which agent the in-flight turn was sent to. A "text" frame carries no
  // agent field (it's a bare content delta), so the label for a streaming
  // reply comes from the request, not the response.
  const pendingAgentRef = useRef<Agent>("claude");
  // Thinking can arrive before the reply it belongs to exists, so it is held
  // until there is a message to attach it to.
  const pendingReasoningRef = useRef<string>("");
  // Mirrors `models` for the onmessage handler, which needs the current value
  // synchronously rather than after a render.
  const modelsRef = useRef<Record<string, string>>({});
  // The conversation the socket is actually on. Distinct from
  // `requestedThreadId` (which only triggers the effect): when the server mints
  // an id for a brand-new conversation, a later reconnect has to reuse *that*
  // id, or a dropped connection would silently start yet another thread.
  const activeThreadRef = useRef<string | null>(null);

  // Resolved once on mount: which conversation to open first.
  useEffect(() => {
    const stored = window.localStorage.getItem(THREAD_STORAGE_KEY);
    activeThreadRef.current = stored;
    setRequestedThreadId(stored);
  }, []);

  useEffect(() => {
    if (requestedThreadId === undefined) return; // still resolving from storage

    let disposed = false;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    // Grows per failed attempt so a server that is down does not get hammered,
    // and resets on a successful open.
    let retryDelayMs = 1000;

    const scheduleReconnect = () => {
      if (disposed || retryTimer !== null) return;
      retryTimer = setTimeout(() => {
        retryTimer = null;
        void connect();
      }, retryDelayMs);
      retryDelayMs = Math.min(retryDelayMs * 2, 10000);
    };

    const connect = async () => {
      // History is loaded *before* the socket opens, not in parallel: both
      // paths append to the same message list through messageCountRef, and a
      // streamed turn arriving mid-fetch would be indexed against a list that
      // is about to be replaced. Skipped on a reconnect, since the transcript
      // on screen is already current.
      const openingThreadId = activeThreadRef.current;

      if (openingThreadId && messageCountRef.current === 0) {
        try {
          const { turns, totals } = await fetchThreadHistory(openingThreadId);
          if (disposed) return;
          // The thread's running totals, so reopening a conversation does not
          // read "$0.0000 · 0 tok" for work that already happened.
          setUsage({ totalCostUsd: totals.cost_usd, totalTokens: totals.tokens });
          const restored: ChatMessage[] = [];
          for (const turn of turns) {
            const agent: Agent = isAgentName(turn.agent) ? turn.agent : "claude";
            restored.push({ role: "user", text: turn.prompt, agent });

            // Older turns have no ordered blocks. Fall back to their audit
            // tools and final answer; new turns restore the recorded sequence.
            const oldBlocks: MessageBlock[] = (turn.tools ?? []).map((tool) => ({
              kind: "tool" as const,
              tool: {
                tool_use_id: tool.tool_use_id,
                tool_name: tool.tool_name,
                input: tool.input,
                verdict: tool.verdict,
                decided_by: tool.decided_by,
                rule_id: tool.rule_id,
                category: tool.category,
              },
            }));
            if (turn.response) oldBlocks.push({ kind: "text", text: turn.response });
            const blocks: MessageBlock[] = turn.meta?.blocks?.length
              ? turn.meta.blocks as MessageBlock[]
              : oldBlocks;

            const subagents = turn.meta?.subagents ?? [];
            restored.push({
              role: "agent",
              text: turn.response,
              agent,
              model: turn.meta?.model ?? undefined,
              blocks,
              plan: subagents.length
                ? {
                    mode: "delegate",
                    note: "",
                    tasks: subagents.map((task) => ({
                      id: task.id,
                      title: task.title,
                      prompt: task.prompt,
                      agent: task.agent,
                      model: task.model ?? undefined,
                      status: (task.status as SubagentTask["status"]) ?? "done",
                      error: task.error,
                      output: task.output,
                      tokens: task.tokens,
                      cost_usd: task.cost_usd,
                      tools: task.tools,
                    })),
                  }
                : undefined,
            });
          }
          messageCountRef.current = restored.length;
          setMessages(restored);
        } catch {
          // A failed history fetch is not worth blocking the chat over: the
          // conversation itself lives server-side either way.
        }
      }

      if (disposed) return;

      const baseUrl = wsBase();
      const url = openingThreadId
        ? `${baseUrl}${baseUrl.includes("?") ? "&" : "?"}workspace=${encodeURIComponent(openingThreadId)}`
        : baseUrl;

      const socket = new WebSocket(url);
      socketRef.current = socket;

      socket.onopen = () => {
        retryDelayMs = 1000;
        setConnected(true);
      };
      socket.onclose = () => {
        setConnected(false);
        setStreaming(false);
        // A server restart (or any dropped connection) used to leave the page
        // reading "disconnected" until someone refreshed it.
        scheduleReconnect();
      };
      // onerror fires before onclose for a refused connection; the close
      // handler above is what schedules the retry.
      socket.onerror = () => setConnected(false);

      // Named, because a replay feeds the same handler: a frame that arrived
      // while this browser was elsewhere has to be processed exactly like one
      // arriving now, or the two paths drift.
      const handleEvent = (event: ChatEvent) => {
        if (event.type === "replay") {
          // Everything that happened while nobody was watching, in order. The
          // turn kept running server-side; this is the catch-up, and the
          // server's buffer is the authority — so anything this browser had
          // already drawn for the turn in flight is dropped first.
          const start = turnStartRef.current;
          if (start !== null) {
            messageCountRef.current = start;
            agentMessageIndexRef.current = null;
            pendingReasoningRef.current = "";
            planRef.current = null;
            setPlan(null);
            setMessages((prev) => prev.slice(0, start));
          }
          for (const frame of event.frames ?? []) handleEvent(frame);
          return;
        }

        if (event.type === "prompt") {
          // Only ever replayed: the client that typed it already showed it.
          turnStartRef.current = messageCountRef.current;
          messageCountRef.current += 1;
          const agent = isAgentName(event.agent ?? "") ? (event.agent as Agent) : pendingAgentRef.current;
          pendingAgentRef.current = agent;
          setMessages((prev) => [...prev, { role: "user", text: event.text, agent }]);
          return;
        }

        if (event.type === "thread_id") {
          window.localStorage.setItem(THREAD_STORAGE_KEY, event.text);
          activeThreadRef.current = event.text;
          setThreadId(event.text);
          if (event.workspace) setWorkspace(event.workspace);
          if (event.workspace_name) setWorkspaceName(event.workspace_name);
          // A conversation can be mid-turn when it is opened: the work belongs
          // to the thread, not to whoever is looking at it, so the composer has
          // to come up in its streaming state rather than pretending it is idle.
          if (event.running) {
            setStreaming(true);
            setStatus((previous) => previous ?? {
              phase: "answering",
              agent: event.agent ?? pendingAgentRef.current,
              count: 0,
              since: Date.now(),
            });
          }
          return;
        }

        if (event.type === "status") {
          const phase = event.text as TurnStatus["phase"];
          setStatus({
            phase,
            agent: event.agent ?? pendingAgentRef.current,
            count: event.count ?? 0,
            since: Date.now(),
          });
          return;
        }

        if (event.type === "route") {
          setRoute({ agent: event.agent ?? "claude", reason: event.text });
          pendingAgentRef.current = (event.agent as Agent) ?? pendingAgentRef.current;
          return;
        }

        if (event.type === "model") {
          const agent = event.agent ?? pendingAgentRef.current;
          modelsRef.current = { ...modelsRef.current, [agent]: event.text };
          setModels((prev) => (prev[agent] === event.text ? prev : { ...prev, [agent]: event.text }));
          return;
        }

        if (event.type === "tool" && event.tool) {
          const tool = event.tool;
          if (tool.agent_id) {
            const id = `native:${tool.agent_id}`;
            const current = planRef.current ?? { mode: "delegate", note: "", tasks: [] };
            const existing = current.tasks.find((task) => task.id === id);
            const tools = [...(existing?.tools ?? [])];
            const toolIndex = tool.tool_use_id
              ? tools.findIndex((item) => item.tool_use_id === tool.tool_use_id)
              : -1;
            if (toolIndex >= 0) tools[toolIndex] = tool;
            else tools.push(tool);
            const agent = event.agent ?? "codex";
            const task: SubagentTask = existing
              ? { ...existing, tools }
              : {
                  id,
                  title: "하위 에이전트",
                  agent,
                  // A native sub-agent runs inside the lead's own process, so
                  // the model the lead reported is the one it runs on.
                  model: modelsRef.current[agent],
                  status: "running",
                  tools,
                };
            planRef.current = {
              ...current,
              tasks: existing
                ? current.tasks.map((item) => item.id === id ? task : item)
                : [...current.tasks, task],
            };
            setPlan(planRef.current);
            return;
          }
          if (agentMessageIndexRef.current === null) {
            // Tool calls usually land before any reply text — agents act
            // first and narrate after — so the reply is created here rather
            // than dropping the call.
            agentMessageIndexRef.current = messageCountRef.current;
            messageCountRef.current += 1;
            const agent = pendingAgentRef.current;
            const reasoning = pendingReasoningRef.current || undefined;
            setMessages((prev) => [
              ...prev,
              {
                role: "agent",
                text: "",
                agent,
                model: modelsRef.current[agent],
                blocks: [{ kind: "tool", tool }],
                reasoning,
              },
            ]);
            return;
          }
          const index = agentMessageIndexRef.current;
          setMessages((prev) => {
            if (!prev[index]) return prev;
            const next = [...prev];
            next[index] = {
              ...next[index],
              blocks: appendTool(next[index].blocks ?? [], tool),
            };
            return next;
          });
          return;
        }

        if (event.type === "reasoning") {
          pendingReasoningRef.current = `${pendingReasoningRef.current}${
            pendingReasoningRef.current ? "\n\n" : ""
          }${event.text}`;
          const index = agentMessageIndexRef.current;
          const reasoning = pendingReasoningRef.current;
          if (index !== null) {
            setMessages((prev) => {
              if (!prev[index]) return prev;
              const next = [...prev];
              next[index] = { ...next[index], reasoning };
              return next;
            });
          }
          return;
        }

        if (event.type === "plan") {
          setPlanOutcome({
            mode: event.mode ?? "direct",
            note: event.text ?? "",
            taskCount: event.tasks?.length ?? 0,
          });
          const existing = planRef.current?.tasks ?? [];
          const nativeTasks = existing.filter((task) => task.id.startsWith("native:"));
          // A turn can plan more than once: the lead is shown the first round's
          // results and may add the step they revealed, and the server resends
          // the whole graph. Tasks already on screen keep their own state —
          // replacing the list wholesale reset finished tasks to "running".
          planRef.current = event.mode === "delegate" && event.tasks?.length
            ? {
                mode: event.mode,
                note: event.text,
                tasks: [
                  ...event.tasks.map((task) => {
                    const known = existing.find((item) => item.id === task.id);
                    return known
                      ? { ...known, ...task, status: known.status, tools: known.tools }
                      : { ...task, status: "running" as const };
                  }),
                  ...nativeTasks,
                ],
              }
            : nativeTasks.length ? { mode: "delegate", note: "", tasks: nativeTasks } : null;
          setPlan(planRef.current);
          return;
        }

        if (event.type === "subagent" && event.task) {
          const task = event.task;
          const current = planRef.current ?? { mode: "delegate", note: "", tasks: [] };
          const exists = current.tasks.some((item) => item.id === task.id);
          planRef.current = {
            ...current,
            tasks: exists
              ? current.tasks.map((item) => item.id === task.id
                ? { ...item, ...task, tools: task.tools ?? item.tools }
                : item)
              : [...current.tasks, task],
          };
          setPlan(planRef.current);
          return;
        }

        if (event.type === "approval_request") {
          setApprovals((prev) => [...prev, event as unknown as ApprovalRequest]);
          return;
        }

        if (event.type === "question") {
          const request: QuestionRequest = {
            request_id: event.request_id ?? "",
            questions: event.questions ?? [],
            agent: event.agent ?? null,
            role: event.role ?? "lead",
          };
          if (request.request_id && request.questions.length) {
            setQuestions((prev) => [...prev, request]);
          }
          return;
        }

        if (event.type === "text") {
          // The answer has started: the phase line has served its purpose.
          setStatus(null);
          if (agentMessageIndexRef.current === null) {
            agentMessageIndexRef.current = messageCountRef.current;
            messageCountRef.current += 1;
            const agent = pendingAgentRef.current;
            setMessages((prev) => [
              ...prev,
              {
                role: "agent",
                text: event.text,
                agent,
                model: modelsRef.current[agent],
                blocks: [{ kind: "text", text: event.text }],
                reasoning: pendingReasoningRef.current || undefined,
              },
            ]);
          } else {
            const index = agentMessageIndexRef.current;
            setMessages((prev) => {
              if (!prev[index]) return prev;
              const next = [...prev];
              next[index] = {
                ...next[index],
                text: next[index].text + event.text,
                blocks: appendText(next[index].blocks ?? [], event.text),
              };
              return next;
            });
          }
          return;
        }

        if (event.type === "failover") {
          // The turn is continuing on another rung, so this is a notice in the
          // transcript rather than an error: the answer still arrives.
          messageCountRef.current += 1;
          const agent = event.agent ?? pendingAgentRef.current;
          setMessages((prev) => [
            ...prev,
            {
              role: "agent",
              agent,
              notice: true,
              text: `${event.from_rung} → ${event.to_rung} 전환 (${event.text})`,
            },
          ]);
          // The next text chunk belongs to the retry, not to the partial reply
          // that was streaming before the switch.
          agentMessageIndexRef.current = null;
          // A rung label is "<agent>" or "<agent>/<mode>", so the agent is
          // whatever precedes the slash.
          const rungAgent = event.to_rung?.split("/")[0];
          if (rungAgent && isAgentName(rungAgent)) pendingAgentRef.current = rungAgent;
          return;
        }

        if (event.type === "error") {
          setStatus(null);
          messageCountRef.current += 1;
          const agent = event.agent ?? pendingAgentRef.current;
          setMessages((prev) => [...prev, { role: "agent", text: `⚠️ ${event.text}`, agent }]);
          agentMessageIndexRef.current = null;
          setStreaming(false);
          return;
        }

        if (event.type === "cancelled") {
          setStatus(null);
          agentMessageIndexRef.current = null;
          setStreaming(false);
          return;
        }

        if (event.type === "result") {
          // The fan-out belongs to the turn that ran it: moving it onto the
          // message keeps it on screen after the turn ends and through a
          // reload, instead of vanishing with the next prompt.
          const index = agentMessageIndexRef.current;
          const finishedPlan = planRef.current;
          if (finishedPlan && index !== null) {
            setMessages((prev) => {
              if (!prev[index]) return prev;
              const next = [...prev];
              next[index] = { ...next[index], plan: finishedPlan };
              return next;
            });
          }
          planRef.current = null;
          setPlan(null);
          setStatus(null);
          agentMessageIndexRef.current = null;
          pendingReasoningRef.current = "";
          turnStartRef.current = null;
          setStreaming(false);
          // Server-side totals, not locally accumulated: they come from the
          // checkpointed graph state, so they stay right across reconnects.
          setUsage({
            totalCostUsd: event.total_cost_usd ?? 0,
            totalTokens: event.total_tokens ?? 0,
          });
        }
      };

      socket.onmessage = (raw) => handleEvent(JSON.parse(raw.data) as ChatEvent);
    };

    void connect();

    return () => {
      disposed = true;
      if (retryTimer !== null) clearTimeout(retryTimer);
      socketRef.current?.close();
    };
  }, [requestedThreadId]);

  const sendMessage = useCallback(
    (text: string, agent: Agent | "auto") => {
      turnStartRef.current = messageCountRef.current;
      messageCountRef.current += 1;
      // "auto" is resolved server-side; until the route frame arrives, label
      // the pending reply with whatever ran last.
      if (agent !== "auto") pendingAgentRef.current = agent;
      setMessages((prev) => [
        ...prev,
        { role: "user", text, agent: agent === "auto" ? pendingAgentRef.current : agent },
      ]);
      setStreaming(true);
      // Set before the server says anything: the gap between pressing Enter and
      // the first frame is exactly where the screen used to go quiet.
      setStatus({
        phase: "thinking",
        agent: agent === "auto" ? pendingAgentRef.current : agent,
        count: 0,
        since: Date.now(),
      });
      pendingReasoningRef.current = "";
      // A new turn replaces the previous turn's fan-out rather than stacking.
      setPlan(null);
      planRef.current = null;
      setPlanOutcome(null);
      setRoute(null);
      // JSON protocol: {prompt, agent}. The server also still
      // accepts a bare text frame — see _parse_client_frame.
      socketRef.current?.send(
        JSON.stringify({ prompt: text, agent })
      );
    },
    []
  );

  const cancel = useCallback(() => {
    socketRef.current?.send(JSON.stringify({ type: "cancel" }));
  }, []);

  /** Sends the user's answers back to the agent that asked. Removed locally
   *  rather than on an ack, for the same reason an approval is: the reply goes
   *  to the waiting tool call, not back to the browser. */
  const answerQuestion = useCallback((requestId: string, answers: (string | string[])[]) => {
    socketRef.current?.send(
      JSON.stringify({ type: "answer", request_id: requestId, answers })
    );
    setQuestions((prev) => prev.filter((item) => item.request_id !== requestId));
  }, []);

  const answerApproval = useCallback((requestId: string, decision: "allow" | "deny") => {
    socketRef.current?.send(
      JSON.stringify({ type: "approval", request_id: requestId, decision })
    );
    // Removed locally rather than on an ack: the gate answers the CLI, not the
    // browser, so this frame is the last the UI hears of this request.
    setApprovals((prev) => prev.filter((a) => a.request_id !== requestId));
  }, []);

  /** Opens another workspace's conversation, or a brand new one when `id` is
   *  null. The socket and the transcript are both rebuilt, since each
   *  workspace has its own CLI sessions, folder and history. */
  const openThread = useCallback((id: string | null) => {
    setConnected(false);
    socketRef.current?.close();
    socketRef.current = null;
    messageCountRef.current = 0;
    agentMessageIndexRef.current = null;
    // Held thinking belongs to the turn it arrived in: left behind, it used to
    // be attached to the first reply of the conversation being opened.
    pendingReasoningRef.current = "";
    turnStartRef.current = null;
    setMessages([]);
    setApprovals([]);
    setQuestions([]);
    setPlan(null);
    planRef.current = null;
    // The previous conversation's planning result and route would otherwise
    // stay on screen: "대장이 직접 답했습니다" above an empty transcript, and a
    // model badge for a turn this workspace never ran.
    setPlanOutcome(null);
    setRoute(null);
    setStatus(null);
    setUsage({ totalCostUsd: 0, totalTokens: 0 });
    setWorkspace(null);
    setWorkspaceName(null);
    setStreaming(false);
    activeThreadRef.current = id;
    setThreadId(id);
    if (id) window.localStorage.setItem(THREAD_STORAGE_KEY, id);
    else window.localStorage.removeItem(THREAD_STORAGE_KEY);
    // A new thread id is minted by the server; passing a fresh random string
    // instead would create the row before the first turn exists.
    // Bumped even when the id is unchanged so the effect re-runs and rebuilds
    // the socket; the value itself is only a trigger (see activeThreadRef).
    setRequestedThreadId((prev) => (prev === id ? `${id ?? ""}#${Date.now()}` : id));
  }, []);

  return {
    messages,
    connected,
    streaming,
    usage,
    workspace,
    workspaceName,
    threadId,
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
    openThread,
  };
}
