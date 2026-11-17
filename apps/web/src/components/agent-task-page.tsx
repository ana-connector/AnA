"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Markdown } from "@/components/markdown";
import { BlockList, toolRuns } from "@/components/chat-page";
import { PageShell } from "@/components/page-shell";
import { ToolCalls } from "@/components/tool-calls";
import { useTabs } from "@/components/tabs";
import { AGENTS_CHANGED, publishLiveAgents, readLiveAgents } from "@/lib/agent-live";
import { fetchThreadHistory, fetchTurnDetail } from "@/lib/api";
import { useChatSocket, type SubagentTask, type ToolEvent, type ToolRun } from "@/hooks/useChatSocket";

export default function AgentTaskPage({ sessionId, taskId, turnId: selectedTurnId, startedAt }: { sessionId: string; taskId: string; turnId: number | null; startedAt: number | null }) {
  const [storedTask, setTask] = useState<SubagentTask | null>(null);
  const [turnId, setTurnId] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const { open } = useTabs();
  const { plan: socketPlan, streaming, messages } = useChatSocket(sessionId);
  const socketStartedAt = messages.findLast((message) => message.role === "user")?.at;
  const currentRun = selectedTurnId === null && (!startedAt ||
    (socketStartedAt !== undefined && Math.abs(socketStartedAt - startedAt) < 30000));
  const witnessedRun = useRef(false);
  const task = currentRun
    ? socketPlan?.tasks.find((item) => item.id === taskId) ?? storedTask
    : storedTask;

  const refresh = useCallback(async () => {
    const state = selectedTurnId === null ? readLiveAgents(sessionId) : null;
    const liveState = !startedAt || state?.startedAt === startedAt ? state : null;
    const live = liveState?.tasks.find((item) => item.id === taskId);
    if (live) { setTask(live); setLoading(false); }
    try {
      const history = selectedTurnId === null ? await fetchThreadHistory(sessionId, { limit: 30 }) : null;
      const turn = selectedTurnId !== null ? await fetchTurnDetail(sessionId, selectedTurnId) : history?.turns.slice().reverse().find((item) =>
        item.meta?.subagents?.some((agent) => agent.id === taskId) && (!startedAt || Date.parse(item.created_at) >= startedAt)
      );
      const stored = turn?.meta?.subagents?.find((agent) => agent.id === taskId);
      if (stored) {
        setTask(stored as SubagentTask);
        setTurnId(turn?.id ?? null);
      }
    } catch { /* Keep the live copy when history is temporarily unavailable. */ }
    setLoading(false);
  }, [sessionId, taskId, selectedTurnId, startedAt]);

  useEffect(() => {
    if (!currentRun) return;
    if (streaming || socketPlan) {
      witnessedRun.current = true;
      const previous = readLiveAgents(sessionId);
      publishLiveAgents(sessionId, {
        tasks: socketPlan?.tasks ?? previous?.tasks ?? [], running: streaming,
        startedAt: previous?.startedAt,
      });
    } else if (witnessedRun.current) {
      publishLiveAgents(sessionId, { tasks: [], running: false });
      void Promise.resolve().then(refresh);
      witnessedRun.current = false;
    }
  }, [sessionId, currentRun, socketPlan, streaming, refresh]);

  useEffect(() => {
    void Promise.resolve().then(refresh);
    const syncLive = () => {
      if (selectedTurnId !== null) return;
      const state = readLiveAgents(sessionId);
      if (startedAt && state?.startedAt !== startedAt) return;
      const live = state?.tasks.find((item) => item.id === taskId);
      if (live) setTask(live);
    };
    const onChange = (event: Event) => {
      if ((event as CustomEvent<string>).detail === sessionId) syncLive();
    };
    let channel: BroadcastChannel | null = null;
    try {
      channel = new BroadcastChannel(AGENTS_CHANGED);
      channel.onmessage = (event) => { if (event.data === sessionId) syncLive(); };
    } catch { /* Same-document events still work. */ }
    window.addEventListener(AGENTS_CHANGED, onChange);
    const timer = window.setInterval(() => { void refresh(); }, 5000);
    return () => { window.removeEventListener(AGENTS_CHANGED, onChange); channel?.close(); clearInterval(timer); };
  }, [refresh, sessionId, taskId, selectedTurnId, startedAt]);

  const loadFullInput = turnId === null ? undefined : async (toolUseId: string) => {
    const turn = await fetchTurnDetail(sessionId, turnId);
    const agent = turn?.meta?.subagents?.find((item) => item.id === taskId);
    const tools = [...(agent?.tools ?? []), ...(agent?.blocks ?? []).filter((block) => block.kind === "tool").map((block) => block.kind === "tool" ? block.tool : null).filter((item) => item !== null)];
    return tools.find((item) => item.tool_use_id === toolUseId)?.input ?? null;
  };
  const loadRun = turnId === null ? undefined : async (run: ToolRun): Promise<ToolEvent[]> => {
    const turn = await fetchTurnDetail(sessionId, turnId);
    return turn ? toolRuns(turn)[run.index] ?? [] : [];
  };

  return (
    <PageShell title={task?.title ?? "서브에이전트"}>
      <div className="flex flex-col gap-5">
        <div className="flex items-center gap-3">
          <Button variant="outline" size="sm" onClick={() => open(`/${encodeURIComponent(sessionId)}`)}>대장 대화</Button>
          <h1 className="font-heading text-xl font-semibold">{task?.title ?? "서브에이전트"}</h1>
        </div>
        {loading && <p className="text-sm text-muted-foreground">작업을 불러오는 중…</p>}
        {!loading && !task && <p className="text-sm text-muted-foreground">작업 기록을 찾지 못했습니다.</p>}
        {task && <>
          <div className="flex flex-wrap gap-2">
            <Badge variant="outline">{task.agent}</Badge>
            {task.model && <Badge variant="outline">{task.model}</Badge>}
            <Badge variant="secondary">{task.status === "running" ? "실행 중" : task.status === "done" ? "완료" : task.status}</Badge>
            {task.scope && <Badge variant="outline">{task.scope.mode === "read" ? "읽기 전용" : "쓰기 가능"}{task.scope.paths?.length ? ` · ${task.scope.paths.join(", ")}` : ""}</Badge>}
          </div>
          {task.prompt && <section className="rounded-md border p-4"><div className="mb-2 text-xs text-muted-foreground">대장 에이전트의 지시</div><p className="whitespace-pre-wrap text-sm">{task.prompt}</p></section>}
          {task.blocks?.length ? <BlockList blocks={task.blocks} loadFullInput={loadFullInput} loadRun={loadRun} defaultToolsOpen={task.status === "running"} /> : task.tools?.length ? <ToolCalls tools={task.tools} /> : null}
          {task.output ? <Markdown>{task.output}</Markdown> : <p className="text-sm text-muted-foreground">{task.error ?? "작업 결과를 기다리는 중…"}</p>}
          {task.output && task.error && <p className="text-sm text-destructive">{task.error}</p>}
        </>}
      </div>
    </PageShell>
  );
}
