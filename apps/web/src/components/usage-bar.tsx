"use client";

import { useCallback, useEffect, useState } from "react";
import { CircleDotIcon } from "lucide-react";
import { ClaudeIcon, KimiIcon, OpenAIIcon, TapieIcon } from "@/components/agent-icons";
import { apiBase } from "@/lib/api";
import { cn } from "@/lib/utils";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

type Window = {
  label: string;
  key: string;
  tokens: number;
  budget: number;
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

  const agents = Object.entries(usage);

  return (
    // Generous vertical padding: this is the last row on screen, and a tight
    // one reads as clipped against the window edge.
    <footer className="flex shrink-0 flex-wrap items-center gap-x-5 gap-y-2 border-t border-border px-4 py-3 text-xs text-muted-foreground">
      {agents.length === 0 ? (
        <span>아직 집계된 사용량이 없습니다</span>
      ) : (
        agents.map(([agent, data]) => {
          const fiveHour = data.windows.find((w) => w.key === "five_hour") ?? data.windows[0];
          const sevenDay = data.windows.find((w) => w.key === "seven_day");
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
                {resetLabel(fiveHour.resets_at) ? (
                  <span>5시간 초기화 {resetLabel(fiveHour.resets_at)}</span>
                ) : null}
                {sevenDay ? (
                  <span>
                    7일 {percent(sevenDay.utilization)}
                    {resetLabel(sevenDay.resets_at)
                      ? ` · 초기화 ${resetLabel(sevenDay.resets_at)}`
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
