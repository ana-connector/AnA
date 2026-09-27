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
    blocks?: HistoryBlock[];
    subagents?: {
      id: string;
      title: string;
      prompt?: string;
      agent: string;
      model?: string | null;
      status: string;
      error?: string;
      output?: string;
      tokens?: number;
      cost_usd?: number;
      tools?: HistoryTool[];
      /** What this sub-agent said and did, in order. Absent on turns recorded
       *  before sub-agents kept one. */
      blocks?: HistoryBlock[];
    }[];
  };
};

/** One entry of a saved transcript: text an agent wrote, a call the gate
 *  screened, or a divider ANA inserted where the work changed model. */
export type HistoryBlock =
  | { kind: "text"; text: string }
  | { kind: "tool"; tool: HistoryTool }
  | { kind: "notice"; text: string };

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

export type ScheduleRow = {
  id: string;
  workspace_id: string;
  name: string;
  cron: string;
  /** The expression in words, so the list does not require knowing cron. */
  cron_text: string;
  prompt: string;
  enabled: boolean;
  created_by: string;
  created_at: string;
  next_run_at: string | null;
  last_run_at: string | null;
  last_status: string;
  last_detail: string;
};

export async function fetchSchedules(workspaceId?: string): Promise<ScheduleRow[]> {
  const query = workspaceId ? `?workspace_id=${encodeURIComponent(workspaceId)}` : "";
  const response = await fetch(`${apiBase()}/schedules${query}`);
  if (!response.ok) throw new Error(`schedule list failed: ${response.status}`);
  return ((await response.json()) as { schedules: ScheduleRow[] }).schedules;
}

export async function createSchedule(body: {
  workspace_id: string;
  name: string;
  cron: string;
  prompt: string;
}): Promise<ScheduleRow> {
  const response = await fetch(`${apiBase()}/schedules`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    throw new Error(((await response.json()) as { detail?: string }).detail ?? response.statusText);
  }
  return (await response.json()) as ScheduleRow;
}

export async function patchSchedule(
  id: string,
  body: { name?: string; cron?: string; prompt?: string; enabled?: boolean }
): Promise<ScheduleRow> {
  const response = await fetch(`${apiBase()}/schedules/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    throw new Error(((await response.json()) as { detail?: string }).detail ?? response.statusText);
  }
  return (await response.json()) as ScheduleRow;
}

export async function deleteSchedule(id: string): Promise<void> {
  const response = await fetch(`${apiBase()}/schedules/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
  if (!response.ok) throw new Error(`schedule delete failed: ${response.status}`);
}

export async function runScheduleNow(id: string): Promise<void> {
  const response = await fetch(`${apiBase()}/schedules/${encodeURIComponent(id)}/run`, {
    method: "POST",
  });
  if (!response.ok) {
    throw new Error(((await response.json()) as { detail?: string }).detail ?? response.statusText);
  }
}

export type WorkspaceSummary = {
  id: string;
  name: string;
  path: string;
  last_used_at: string;
  last_turn_at: string | null;
  turn_count: number;
  last_agent: string | null;
  /** Every token this conversation has spent, as the sidebar shows it. */
  total_tokens: number;
  container_enabled: boolean;
  container_image: string | null;
  container_ports: PortMapping[];
  system_prompt: string;
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
  const response = await fetch(`${apiBase()}/workspaces/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
  // The response used to be ignored, so a delete that failed (a 404 for a row
  // someone else already removed, a server that is down) closed the dialog and
  // looked exactly like a delete that worked — until the list came back with
  // the workspace still in it.
  if (!response.ok) {
    const detail = await response.json().then((body) => body?.detail).catch(() => null);
    throw new Error(detail ?? `workspace delete failed: ${response.status}`);
  }
}

export async function updateWorkspace(
  id: string,
  patch: {
    name?: string;
    container_enabled?: boolean;
    container_image?: string | null;
    container_ports?: PortMapping[];
    system_prompt?: string;
  }
): Promise<void> {
  const response = await fetch(`${apiBase()}/workspaces/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(patch),
  });
  if (!response.ok) throw new Error(`workspace update failed: ${response.status}`);
}

export type WorkspaceCapabilities = {
  /** Whether per-workspace containers can run here at all: ANA needs the
   *  Docker socket, so the UI disables the switch instead of letting someone
   *  turn on something that would only fail at the first turn. */
  containers: boolean;
  /** The workspace whose folder is ANA's own source tree, or null when the
   *  repository is not mounted. Work done there changes ANA itself, so the
   *  list marks that row instead of letting it look like any other project. */
  self_workspace: string | null;
};

export async function fetchWorkspaceCapabilities(): Promise<WorkspaceCapabilities> {
  try {
    const response = await fetch(`${apiBase()}/workspaces/capabilities`);
    if (!response.ok) return { containers: false, self_workspace: null };
    const payload = (await response.json()) as Partial<WorkspaceCapabilities>;
    return {
      containers: Boolean(payload.containers),
      self_workspace: payload.self_workspace ?? null,
    };
  } catch {
    return { containers: false, self_workspace: null };
  }
}

export type AnaSettings = {
  routing: string;
  system_prompt: string;
  /** Which billing mode the Claude CLI runs under: "pro" (subscription) or
   *  "api" (key + base URL). Applies to conversations opened after the save. */
  auth_mode?: string;
  auth_modes?: string[];
  /** "normal" asks the user whatever the rule set says to ask about; "yolo"
   *  answers those approvals with "allow" on their behalf. Denies, the secret
   *  scan and scope checks are unaffected, and unlike the settings above this
   *  one applies to the very next tool call. */
  governance_mode?: string;
  governance_modes?: string[];
};

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

/** What one agent's half of a manual usage sync did. `refreshed: false` is a
 *  normal answer — Codex may have no log yet, and the Claude probe can come
 *  back without a rate-limit event — so `reason` is what the UI shows. */
export type UsageSyncResult = {
  agent: string;
  refreshed: boolean;
  source?: string;
  reason?: string;
  cost_usd?: number;
  tokens?: number;
};

/** Re-reads the providers' real figures now instead of at the next turn. Costs
 *  a short Claude turn (see apps/server/app/usage_sync.py), which is why it is
 *  a button and not a poll. */
export async function syncUsage(): Promise<UsageSyncResult[]> {
  const response = await fetch(`${apiBase()}/usage/sync`, { method: "POST" });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload?.detail ?? `HTTP ${response.status}`);
  return (payload as { synced: UsageSyncResult[] }).synced;
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
