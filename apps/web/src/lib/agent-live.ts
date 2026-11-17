import type { SubagentTask } from "@/hooks/useChatSocket";

export type LiveAgents = { tasks: SubagentTask[]; running: boolean; startedAt?: number };
const key = (workspaceId: string) => `ana:agents:${workspaceId}`;
export const AGENTS_CHANGED = "ana:agents-changed";

export function readLiveAgents(workspaceId: string): LiveAgents | null {
  try {
    const raw = sessionStorage.getItem(key(workspaceId));
    return raw ? JSON.parse(raw) as LiveAgents : null;
  } catch { return null; }
}

export function publishLiveAgents(workspaceId: string, state: LiveAgents) {
  try { sessionStorage.setItem(key(workspaceId), JSON.stringify(state)); } catch { /* storage may be off */ }
  window.dispatchEvent(new CustomEvent(AGENTS_CHANGED, { detail: workspaceId }));
  try {
    const channel = new BroadcastChannel(AGENTS_CHANGED);
    channel.postMessage(workspaceId);
    channel.close();
  } catch { /* BroadcastChannel may be unavailable */ }
}
