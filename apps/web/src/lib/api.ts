/** Where the FastAPI server is reachable from the browser.
 *
 * Both the WebSocket and the HTTP endpoints derive their host from the page's
 * own location unless overridden, so one build works on localhost and over
 * the LAN alike (a hardcoded "localhost" means the *viewer's* machine when
 * the UI is opened from another device).
 */

const API_PORT = process.env.NEXT_PUBLIC_WS_PORT ?? "8100";

export function apiBase(): string {
  const configured = process.env.NEXT_PUBLIC_API_URL;
  if (configured) return configured.replace(/\/$/, "");
  return `${window.location.protocol}//${window.location.hostname}:${API_PORT}`;
}

export function wsBase(): string {
  const configured = process.env.NEXT_PUBLIC_WS_URL;
  if (configured) return configured;
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.hostname}:${API_PORT}/ws/chat`;
}

export type HistoryTool = {
  tool_use_id: string | null;
  tool_name: string;
  input: Record<string, unknown>;
  verdict: string;
  decided_by: string;
  rule_id: string | null;
  category: string | null;
};

export type HistoryTurn = {
  prompt: string;
  response: string;
  agent: string;
  cost_usd: number | null;
  created_at: string;
  /** Tool calls the gate recorded during this turn, and the fan-out it ran. */
  tools?: HistoryTool[];
  meta?: {
    model?: string | null;
    rung?: string;
    blocks?: (
      | { kind: "text"; text: string }
      | { kind: "tool"; tool: HistoryTool }
    )[];
    subagents?: {
      id: string;
      title: string;
      prompt?: string;
      agent: string;
      status: string;
      error?: string;
      output?: string;
      tokens?: number;
      cost_usd?: number;
      tools?: HistoryTool[];
    }[];
  };
};

export type ThreadHistory = {
  turns: HistoryTurn[];
  totals: { cost_usd: number; tokens: number };
};

export async function fetchThreadHistory(threadId: string): Promise<ThreadHistory> {
  const response = await fetch(
    `${apiBase()}/threads/${encodeURIComponent(threadId)}/turns`
  );
  if (!response.ok) throw new Error(`history fetch failed: ${response.status}`);
  const payload = (await response.json()) as ThreadHistory;
  return { turns: payload.turns, totals: payload.totals ?? { cost_usd: 0, tokens: 0 } };
}

export type ThreadSummary = {
  thread_id: string;
  turn_count: number;
  last_at: string;
  title: string;
  last_agent: string | null;
  workspace: string;
};

export async function fetchThreads(): Promise<ThreadSummary[]> {
  const response = await fetch(`${apiBase()}/threads`);
  if (!response.ok) throw new Error(`thread list failed: ${response.status}`);
  const payload = (await response.json()) as { threads: ThreadSummary[] };
  return payload.threads;
}

export type PortMapping = { host: number; container: number };

export type WorkspaceSummary = {
  id: string;
  name: string;
  path: string;
  last_used_at: string;
  last_turn_at: string | null;
  turn_count: number;
  last_agent: string | null;
  container_enabled: boolean;
  container_image: string | null;
  container_ports: PortMapping[];
};

export async function fetchWorkspaces(): Promise<WorkspaceSummary[]> {
  const response = await fetch(`${apiBase()}/workspaces`);
  if (!response.ok) throw new Error(`workspace list failed: ${response.status}`);
  return ((await response.json()) as { workspaces: WorkspaceSummary[] }).workspaces;
}

export async function createWorkspace(name: string, path?: string) {
  const response = await fetch(`${apiBase()}/workspaces`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ name, path: path?.trim() || null }),
  });
  if (!response.ok) throw new Error(`workspace create failed: ${response.status}`);
  return (await response.json()) as { id: string; name: string; path: string };
}

export async function forgetWorkspace(id: string): Promise<void> {
  // Deletes the workspace *and* its conversation (turns, checkpoints). The
  // folder and the audit log stay.
  await fetch(`${apiBase()}/workspaces/${encodeURIComponent(id)}`, { method: "DELETE" });
}

export async function updateWorkspace(
  id: string,
  patch: {
    name?: string;
    container_enabled?: boolean;
    container_image?: string | null;
    container_ports?: PortMapping[];
  }
): Promise<void> {
  const response = await fetch(`${apiBase()}/workspaces/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(patch),
  });
  if (!response.ok) throw new Error(`workspace update failed: ${response.status}`);
}

/** Whether per-workspace containers can run here at all: ANA needs the Docker
 *  socket, so the UI disables the switch instead of letting someone turn on
 *  something that would only fail at the first turn. */
export async function fetchWorkspaceCapabilities(): Promise<{ containers: boolean }> {
  try {
    const response = await fetch(`${apiBase()}/workspaces/capabilities`);
    if (!response.ok) return { containers: false };
    return (await response.json()) as { containers: boolean };
  } catch {
    return { containers: false };
  }
}

export type AnaSettings = { routing: string; system_prompt: string };

export async function fetchSettings(): Promise<AnaSettings> {
  const response = await fetch(`${apiBase()}/settings`);
  if (!response.ok) throw new Error(`settings fetch failed: ${response.status}`);
  return (await response.json()) as AnaSettings;
}

export async function saveSettings(patch: Partial<AnaSettings>): Promise<AnaSettings> {
  const response = await fetch(`${apiBase()}/settings`, {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(patch),
  });
  const payload = await response.json();
  if (!response.ok) {
    // The 422 body names the YAML error, which is the useful part.
    throw new Error(payload?.detail ?? `HTTP ${response.status}`);
  }
  return payload as AnaSettings;
}

export type LadderModel = { agent: string; model: string | null; label: string };

/** The (CLI, model) pairs the router itself uses, so the composer's picker
 *  never offers something the ladder could not choose. */
export async function fetchModels(): Promise<LadderModel[]> {
  try {
    const response = await fetch(`${apiBase()}/models`);
    if (!response.ok) return [];
    return ((await response.json()) as { lead: LadderModel[] }).lead;
  } catch {
    return [];
  }
}
