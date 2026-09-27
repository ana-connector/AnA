"use client";

import { useCallback, useEffect, useState } from "react";
import { CircleDotIcon, RefreshCwIcon } from "lucide-react";
import { ClaudeIcon, KimiIcon, OpenAIIcon, TapieIcon } from "@/components/agent-icons";
import { apiBase, syncUsage } from "@/lib/api";
import { cn } from "@/lib/utils";
import { notify } from "@/lib/notify";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

type Window = {
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

type UsageSnapshot = Record<string, { windows: Window[] }>;

/** How often the bar re-asks while the tab is open. Fast enough that a window
 *  filling up during a long turn is visible, slow enough that it is not a
 *  request every few seconds for a number that moves in percents. */
const POLL_SECONDS = 20;

/** One mark per CLI. The row is a glance at how much room is left, not a
 *  legend — the name is one hover away, and three spelled-out names crowded
 *  out the bars they were labelling. */
type AgentIconProps = { className?: string; "aria-label"?: string; mono?: boolean };

const AGENT_ICON: Record<string, (props: AgentIconProps) => React.ReactElement> = {
  claude: ClaudeIcon,
  codex: OpenAIIcon,
  kimi: KimiIcon,
  tapie: TapieIcon,
};

const AGENT_NAME: Record<string, string> = {
  claude: "Claude Code",
  codex: "Codex",
  kimi: "Kimi Code",
  tapie: "TAPIE",
};

/** "4.3M", "10.0K", "840" — the magnitude is what a status bar is read for. */
function compactTokens(tokens: number): string {
  if (tokens >= 1_000_000) return `${(tokens / 1_000_000).toFixed(1)}M`;
  if (tokens >= 1_000) return `${(tokens / 1_000).toFixed(1)}K`;
  return String(tokens);
}

/** "$12.50 / $50" for a plan capped in dollars, "" for one capped in tokens:
 *  a dollar figure is the thing you actually watch on a metered key, and a
 *  token count against a guessed budget is not worth the room. */
function spentLabel(window: Window): string {
  if (window.unit !== "usd" || !window.budget) return "";
  return `$${window.usd.toFixed(2)} / $${Math.round(window.budget)}`;
}

function percent(value: number | null): string {
  return value === null ? "—" : `${Math.round(value * 100)}%`;
}

/** "14:30" for a reset later today, "9/28 14:30" when it is another day —
 *  a weekly window resets on a date, and the date is the useful half. */
function resetLabel(epochSeconds: number | null): string | null {
  if (epochSeconds === null) return null;
  const at = new Date(epochSeconds * 1000);
  if (Number.isNaN(at.getTime())) return null;
  const clock = at.toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit", hour12: false });
  const sameDay = at.toDateString() === new Date().toDateString();
  return sameDay ? clock : `${at.getMonth() + 1}/${at.getDate()} ${clock}`;
}

function barClass(value: number | null): string {
  if (value === null) return "bg-muted-foreground/40";
  if (value >= 0.9) return "bg-destructive";
  if (value >= 0.7) return "bg-primary";
  return "bg-primary/60";
}

/** Status bar: how close each CLI is to its limits.
 *
 *  Only the five-hour window is on screen. It is the one that decides whether
 *  the next turn routes somewhere else, while the weekly figure changes slowly
 *  enough to be something you look up — so it lives in the tooltip, next to
 *  when each window rolls over. */
export function UsageBar({
  refreshKey,
  totalCostUsd,
  totalTokens,
  failingAgents = {},
}: {
  refreshKey: number;
  totalCostUsd: number;
  totalTokens: number;
  /** agent -> what its last attempt said. Its mark turns red until the agent
   *  answers again. */
  failingAgents?: Record<string, string>;
}) {
  const [usage, setUsage] = useState<UsageSnapshot>({});
  const [syncing, setSyncing] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const response = await fetch(`${apiBase()}/usage`);
      if (!response.ok) return;
      setUsage(((await response.json()) as { agents: UsageSnapshot }).agents);
    } catch {
      // A missing status bar is not worth surfacing an error for.
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh, refreshKey]);

  // The windows move while a turn runs, and a turn runs for minutes. Without
  // this the bar only changed when the next prompt was sent, which is what
  // made it look stale. Paused while the tab is hidden: nobody is reading a
  // status bar they cannot see, and the poll keeps a CLI log being re-read on
  // the server side.
  useEffect(() => {
    const tick = () => {
      if (document.visibilityState === "visible") void refresh();
    };
    const timer = setInterval(tick, POLL_SECONDS * 1000);
    document.addEventListener("visibilitychange", tick);
    return () => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", tick);
    };
  }, [refresh]);

  // Claude only states its windows while a turn runs, so "now" costs one short
  // turn (see apps/server/app/usage_sync.py). A button, therefore, and not part
  // of the poll above.
  const sync = async () => {
    setSyncing(true);
    try {
      const results = await syncUsage();
      await refresh();
      const stale = results.filter((result) => !result.refreshed);
      if (stale.length === 0) {
        notify.success("사용량 동기화됨", "공급자가 보고한 실제 수치입니다.");
      } else {
        // Named rather than swallowed: the number on screen is still the old
        // one for those agents, and the probe may have cost something anyway.
        notify.info(
          "일부만 동기화됨",
          stale.map((result) => `${AGENT_NAME[result.agent] ?? result.agent}: ${result.reason ?? "갱신 없음"}`).join(" · "),
        );
      }
    } catch (e) {
      notify.error("동기화 실패", e instanceof Error ? e.message : String(e));
    } finally {
      setSyncing(false);
    }
  };

  const agents = Object.entries(usage);

  return (
    // Generous vertical padding: this is the last row on screen, and a tight
    // one reads as clipped against the window edge.
    <footer className="flex shrink-0 flex-wrap items-center gap-x-5 gap-y-2 border-t border-border px-4 py-3 text-xs text-muted-foreground">
      <Tooltip>
        <TooltipTrigger
          render={
            <button
              type="button"
              onClick={() => void sync()}
              disabled={syncing}
              aria-label="사용량 즉시 동기화"
              className="-m-1 rounded p-1 text-muted-foreground transition-colors hover:text-foreground disabled:opacity-50"
            >
              <RefreshCwIcon className={cn("size-3.5", syncing && "animate-spin")} />
            </button>
          }
        />
        <TooltipContent>
          사용량 즉시 동기화 · Claude는 짧은 턴을 한 번 돌려 실제 수치를 받아옵니다
        </TooltipContent>
      </Tooltip>

      {agents.length === 0 ? (
        <span>아직 집계된 사용량이 없습니다</span>
      ) : (
        agents.map(([agent, data]) => {
          // The short window first, then whatever the plan's long one is:
          // a week for Claude and Codex, the calendar month for Kimi and
          // TAPIE. Reading the second one by name assumed every plan had the
          // same shape.
          const fiveHour = data.windows.find((w) => w.key === "five_hour") ?? data.windows[0];
          const longer = data.windows.find((w) => w.key !== fiveHour?.key);
          if (!fiveHour) return null;
          const Icon = AGENT_ICON[agent] ?? CircleDotIcon;
          const failure = failingAgents[agent];
          return (
            <Tooltip key={agent}>
              <TooltipTrigger
                render={
                  <span className="flex cursor-default items-center gap-1.5">
                    <Icon
                      className={cn("size-3.5 shrink-0", failure && "text-destructive")}
                      aria-label={AGENT_NAME[agent] ?? agent}
                      // The brand marks with fixed fills ignore a text colour,
                      // so the failing state asks for the monochrome variant.
                      mono={!!failure}
                    />
                    <span className="h-1.5 w-14 overflow-hidden rounded-full bg-muted">
                      <span
                        className={cn(
                          "block h-full",
                          barClass(fiveHour.utilization),
                          fiveHour.source !== "reported" && "opacity-60"
                        )}
                        style={{ width: `${Math.round((fiveHour.utilization ?? 0) * 100)}%` }}
                      />
                    </span>
                    <span className="tabular-nums">{percent(fiveHour.utilization)}</span>
                  </span>
                }
              />
              <TooltipContent className="flex flex-col gap-0.5">
                <span className="font-medium">{AGENT_NAME[agent] ?? agent}</span>
                {failure ? (
                  <span className="max-w-64 text-destructive">직전 호출 실패: {failure}</span>
                ) : null}
                <span>
                  {fiveHour.label} {percent(fiveHour.utilization)}
                  {spentLabel(fiveHour) ? ` (${spentLabel(fiveHour)})` : ""}
                  {resetLabel(fiveHour.resets_at)
                    ? ` · 초기화 ${resetLabel(fiveHour.resets_at)}`
                    : ""}
                </span>
                {longer ? (
                  <span>
                    {longer.label} {percent(longer.utilization)}
                    {spentLabel(longer) ? ` (${spentLabel(longer)})` : ""}
                    {resetLabel(longer.resets_at)
                      ? ` · 초기화 ${resetLabel(longer.resets_at)}`
                      : ""}
                  </span>
                ) : null}
              </TooltipContent>
            </Tooltip>
          );
        })
      )}

      {/* This conversation, in two figures. The caveat that Codex and Kimi
          report no price was a sentence explaining a number nobody had asked
          about yet. */}
      <span className="ml-auto tabular-nums">
        ${totalCostUsd.toFixed(2)} · {compactTokens(totalTokens)} tok
      </span>
    </footer>
  );
}
