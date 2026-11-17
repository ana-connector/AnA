"use client";

import { useEffect, useState } from "react";
import { BotIcon, CircleIcon } from "lucide-react";
import { useTabs } from "@/components/tabs";
import { AGENTS_CHANGED, readLiveAgents } from "@/lib/agent-live";
import { fetchWorkspaceAgents, type WorkspaceAgent } from "@/lib/api";
import { agentHref } from "@/lib/workspace-routes";
import type { SubagentTask } from "@/hooks/useChatSocket";

export function BranchAgents({ workspaceId, reloadKey }: { workspaceId: string; reloadKey: number }) {
  const { open, openSubagent } = useTabs();
  const [stored, setStored] = useState<WorkspaceAgent[]>([]);
  const [live, setLive] = useState<SubagentTask[]>([]);
  const [leadRunning, setLeadRunning] = useState(false);
  const [liveStartedAt, setLiveStartedAt] = useState(0);
  useEffect(() => {
    let alive = true;
    const load = () => { void fetchWorkspaceAgents(workspaceId).then((agents) => { if (alive) setStored(agents); })
      .catch(() => { /* A new branch may have no history yet. */ }); };
    load();
    const timer = window.setInterval(load, 10000);
    return () => { alive = false; clearInterval(timer); };
  }, [workspaceId, reloadKey]);
  useEffect(() => {
    const update = () => {
      const state = readLiveAgents(workspaceId);
      setLive(state?.tasks ?? []);
      setLeadRunning(state?.running ?? false);
      setLiveStartedAt(state?.startedAt ?? 0);
    };
    update();
    const onChange = (event: Event) => { if ((event as CustomEvent<string>).detail === workspaceId) update(); };
    let channel: BroadcastChannel | null = null;
    try {
      channel = new BroadcastChannel(AGENTS_CHANGED);
      channel.onmessage = (event) => { if (event.data === workspaceId) update(); };
    } catch { /* Same-document events still work. */ }
    window.addEventListener(AGENTS_CHANGED, onChange);
    return () => { window.removeEventListener(AGENTS_CHANGED, onChange); channel?.close(); };
  }, [workspaceId]);
  const tasks = live.filter((task) => task.status === "running" && (
    !liveStartedAt || !stored.some((item) => item.id === task.id && Date.parse(item.created_at) >= liveStartedAt && item.status !== "running")
  ));
  return <div className="ml-7 border-l pl-2 pb-1" aria-label="브랜치 에이전트">
    <button type="button" onClick={() => open(`/${encodeURIComponent(workspaceId)}`)} className="flex h-7 w-full items-center gap-1.5 rounded px-2 text-left text-[11px] text-sidebar-foreground/80 hover:bg-sidebar-accent">
      <BotIcon className="size-3 shrink-0" /><span>대장 에이전트</span>
      {leadRunning && <CircleIcon className="ml-auto size-2.5 fill-emerald-500 text-emerald-500" />}
    </button>
    {tasks.map((task) => <button key={`live-${liveStartedAt}-${task.id}`} type="button" onClick={() => openSubagent(agentHref(workspaceId, task.id, undefined, liveStartedAt), { title: task.title, detail: task.agent })}
      title={`${task.agent} · ${task.status}`} className="flex h-7 w-full items-center gap-1.5 rounded px-2 text-left text-[11px] text-sidebar-foreground/80 hover:bg-sidebar-accent">
      <CircleIcon className="size-2.5 shrink-0 fill-emerald-500 text-emerald-500" />
      <span className="min-w-0 flex-1 truncate">{task.title}</span>
    </button>)}
  </div>;
}
