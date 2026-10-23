import {
  resolveApiBase,
  resolveSocketUrl,
  type AddressEnv,
} from "@/lib/urls";

/** Where the FastAPI server is reachable from the browser.
 *
 * The rules themselves are in `urls.ts`, with their own tests; these two are
 * where they meet the real page. Both the WebSocket and the HTTP endpoints
 * derive their host from the page's own location unless overridden, so one
 * build works on localhost and over the LAN alike (a hardcoded "localhost"
 * means the *viewer's* machine when the UI is opened from another device).
 *
 * The names are read whole rather than out of a loop: Next replaces each
 * `process.env.NEXT_PUBLIC_*` literal at build time and cannot substitute a
 * computed one.
 */

const ADDRESS_ENV: AddressEnv = {
  apiUrl: process.env.NEXT_PUBLIC_API_URL,
  wsUrl: process.env.NEXT_PUBLIC_WS_URL,
  apiPort: process.env.NEXT_PUBLIC_WS_PORT,
  mode: process.env.NEXT_PUBLIC_SERVER_MODE,
};

export function apiBase(): string {
  return resolveApiBase(ADDRESS_ENV, window.location);
}

export function wsBase(): string {
  return resolveSocketUrl(ADDRESS_ENV, window.location);
}

/** One place that knows how a request to ANA is made and how it fails.
 *
 * Screens used to call `fetch` themselves, each with its own idea of what a
 * non-2xx response meant — some threw, some returned empty, some showed the
 * status and some did not. The URL was assembled at every one of those call
 * sites too, which is exactly what has to be in one place for the address to
 * be changeable.
 */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    /** FastAPI's `detail`, when the body had one. Screens that can say
     *  something better than the status code read this. */
    readonly detail: string | null
  ) {
    super(detail ? `HTTP ${status}: ${detail}` : `HTTP ${status}`);
    this.name = "ApiError";
  }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${apiBase()}${path}`, init);
  if (!response.ok) {
    const body = await response.text().catch(() => "");
    let detail: string | null = body || null;
    try {
      const parsed = JSON.parse(body) as { detail?: unknown };
      if (typeof parsed.detail === "string") detail = parsed.detail;
    } catch {
      // Not JSON; the raw text is the best there is.
    }
    throw new ApiError(response.status, detail);
  }
  if (response.status === 204 || response.headers.get("content-length") === "0") {
    return undefined as T;
  }
  return (await response.json()) as T;
}

export type HistoryTool = {
  tool_use_id: string | null;
  tool_name: string;
  input: Record<string, unknown>;
  verdict: string;
  decided_by: string;
  rule_id: string | null;
  category: string | null;
  /** The server sent only the label-sized part of `input`; the whole record
   *  comes from `fetchTurnDetail`. */
  input_truncated?: boolean;
};

export type HistoryTurn = {
  /** Row id, and the cursor history pages with. */
  id?: number;
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
  | { kind: "notice"; text: string }
  /** A run of calls, summarized: what a paginated page carries instead of the
   *  calls themselves. The rows come from `fetchTurnDetail`. */
  | {
      kind: "tool_run";
      run: {
        index: number;
        count: number;
        denied: number;
        pending: number;
        last_tool_name?: string | null;
        last_label: string;
      };
    };

export type ThreadHistory = {
  turns: HistoryTurn[];
  /** The whole thread's, not this page's. */
  totals: { cost_usd: number; tokens: number };
  /** Whether there are older turns above this page. */
  hasMore: boolean;
  /** The id of the oldest turn in this page — the cursor for the next one. */
  oldestId: number | null;
  /** Whether the tool calls in this page carry only the label-sized part of
   *  their arguments (see `fetchTurnDetail`). */
  toolsTrimmed: boolean;
};

/** One page of a thread's transcript, newest-last.
 *
 *  Paginated because a long conversation's history is mostly tool calls: a
 *  50-turn thread shipped 2.4 MB, 1.9 MB of it arguments to cards that render
 *  collapsed. `before` asks for the page above the one on screen. */
export async function fetchThreadHistory(
  threadId: string,
  options: { limit?: number; before?: number } = {}
): Promise<ThreadHistory> {
  const query = new URLSearchParams();
  if (options.limit) query.set("limit", String(options.limit));
  if (options.before) query.set("before", String(options.before));
  const suffix = query.size > 0 ? `?${query.toString()}` : "";
  const response = await fetch(
    `${apiBase()}/threads/${encodeURIComponent(threadId)}/turns${suffix}`
  );
  if (!response.ok) throw new Error(`history fetch failed: ${response.status}`);
  const payload = (await response.json()) as {
    turns: HistoryTurn[];
    totals?: { cost_usd: number; tokens: number };
    has_more?: boolean;
    oldest_id?: number | null;
    tools_trimmed?: boolean;
  };
  return {
    turns: payload.turns,
    totals: payload.totals ?? { cost_usd: 0, tokens: 0 },
    hasMore: Boolean(payload.has_more),
    oldestId: payload.oldest_id ?? null,
    toolsTrimmed: Boolean(payload.tools_trimmed),
  };
}

/** One turn with nothing trimmed. Asked for when someone opens a tool call
 *  whose arguments the history page only carried a label for. */
export async function fetchTurnDetail(
  threadId: string,
  turnId: number
): Promise<HistoryTurn | null> {
  const response = await fetch(
    `${apiBase()}/threads/${encodeURIComponent(threadId)}/turns/${turnId}`
  );
  if (!response.ok) return null;
  const payload = (await response.json()) as { turn?: HistoryTurn };
  return payload.turn ?? null;
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
  /** Out of the main sidebar list, but still registered and still openable. */
  archived: boolean;
  /** An emoji, or a small image as a `data:` URL. "" when it has none. */
  icon: string;
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
    archived?: boolean;
    icon?: string;
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
  /** Every CLI this build knows about, and the ones the user switched off.
   *  Stored as the disabled set so a CLI added by a later version arrives
   *  switched on. The router, the failover chain and the composer all skip a
   *  disabled CLI; at least one has to stay on. */
  agents?: string[];
  disabled_agents?: string[];
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

// --- the screens' own endpoints ------------------------------------------
//
// These used to be `fetch` calls inside the six screens that have one each.
// They are here for the same reason the rest of this file is: the address is
// decided in one place, and so is what a failed response means. What each
// screen does with the answer — its loading state, its error text, how it
// lays the data out — stays in the screen.

/** The gate's record of one tool call. */
export type AuditEvent = {
  id: number;
  created_at: string;
  thread_id: string | null;
  agent: string | null;
  tool_name: string;
  tool_input: Record<string, unknown> | null;
  rule_id: string | null;
  category: string | null;
  tool_use_id: string | null;
  verdict: string;
  decided_by: string;
  reason: string | null;
};

export async function fetchAuditEvents(limit = 200): Promise<AuditEvent[]> {
  const body = await api<{ events: AuditEvent[] }>(`/audit?limit=${limit}`);
  return body.events;
}

export type SecretRow = { scope: string; name: string; updated_at: string };

export async function fetchSecrets(): Promise<{ secrets: SecretRow[]; keyPersistent: boolean }> {
  const body = await api<{ secrets: SecretRow[]; key_persistent?: boolean }>("/secrets");
  return { secrets: body.secrets, keyPersistent: body.key_persistent !== false };
}

export async function saveSecret(secret: {
  name: string;
  value: string;
  scope: string;
}): Promise<void> {
  await api("/secrets", {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(secret),
  });
}

export async function deleteSecret(scope: string, name: string): Promise<void> {
  await api(`/secrets/${encodeURIComponent(scope)}/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
}

export type McpServer = { name: string; description: string; command: string };
export type McpInactive = { name: string; reason: string };
export type McpView = {
  role: string;
  enabled: boolean;
  active: McpServer[];
  inactive: McpInactive[];
};

export function fetchMcpServers(role: string): Promise<McpView> {
  return api<McpView>(`/mcp/servers?role=${encodeURIComponent(role)}`);
}

export type SkillSummary = {
  name: string;
  title: string;
  description: string;
  roles: string[];
  mcp: string[];
};
export type SkillLibrary = {
  skills: SkillSummary[];
  errors: { path: string; message: string }[];
};

export function fetchSkills(): Promise<SkillLibrary> {
  return api<SkillLibrary>("/skills");
}

export type UsageWindow = {
  label: string;
  key: string;
  tokens: number;
  budget: number;
  /** "tokens" or "usd": what this plan is capped in. */
  unit: string;
  usd: number;
  utilization: number | null;
  resets_at: number | null;
  source: "reported" | "estimated" | "manual";
};
export type UsageSnapshot = Record<string, { windows: UsageWindow[] }>;

export async function fetchUsage(): Promise<UsageSnapshot> {
  const body = await api<{ agents: UsageSnapshot }>("/usage");
  return body.agents ?? {};
}

export type UsageEstimate = { utilization: number; resets_at: number | null };
export type UsageEstimates = Record<string, Record<string, UsageEstimate>>;

export async function fetchUsageEstimates(): Promise<UsageEstimates> {
  const body = await api<{ estimates: UsageEstimates }>("/usage/estimates");
  return body.estimates;
}

export async function saveUsageEstimate(estimate: {
  agent: string;
  window: string;
  utilization: number | null;
  resets_at: string | null;
}): Promise<void> {
  await api("/usage/estimates", {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(estimate),
  });
}

export type RoutingRung = {
  agent: string;
  model: string;
  effort: string;
  window: string;
  utilization: number | null;
};
export type RoutingLadders = { lead: RoutingRung[]; workers: Record<string, RoutingRung[]> };
export type RoutingConfig = RoutingLadders & {
  agents: string[];
  efforts: Record<string, string[]>;
  models?: Record<string, string[]>;
  windows: { key: string; label: string }[];
  windows_by_agent?: Record<string, string[]>;
  /** CLIs switched off on the settings page: a rung naming one is kept and
   *  marked rather than dropped. */
  disabled_agents?: string[];
};

export function fetchRouting(): Promise<RoutingConfig> {
  return api<RoutingConfig>("/routing");
}

export function saveRouting(ladders: RoutingLadders): Promise<RoutingConfig> {
  return api<RoutingConfig>("/routing", {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(ladders),
  });
}
