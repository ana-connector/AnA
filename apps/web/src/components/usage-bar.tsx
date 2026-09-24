"use client";

import { useCallback, useEffect, useState } from "react";
import { apiBase } from "@/lib/api";
import { cn } from "@/lib/utils";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

type Window = {
  label: string;
  tokens: number;
  budget: number;
  utilization: number | null;
  resets_at: number | null;
  source: "reported" | "estimated" | "manual";
};

type UsageSnapshot = Record<string, { windows: Window[] }>;

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
 *  Both CLIs state their real utilization — Claude on stdout, Codex only in
 *  its rollout log, which ANA reads back — so an unlabeled figure is the
 *  provider's own. The fallbacks are labeled rather than blended in: showing
 *  ANA's token estimate as if it were the provider's number would be the
 *  worst of both. */
export function UsageBar({
  refreshKey,
  totalCostUsd,
  totalTokens,
}: {
  refreshKey: number;
  totalCostUsd: number;
  totalTokens: number;
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

  const agents = Object.entries(usage);

  return (
    // Generous vertical padding: this is the last row on screen, and a tight
    // one reads as clipped against the window edge.
    <footer className="flex shrink-0 flex-wrap items-center gap-x-6 gap-y-2 border-t border-border px-4 py-3 text-xs text-muted-foreground">
      {agents.length === 0 ? (
        <span>아직 집계된 사용량이 없습니다</span>
      ) : (
        agents.map(([agent, data]) => (
          <div key={agent} className="flex items-center gap-2">
            <span className="font-medium text-foreground">{agent}</span>
            {data.windows.map((window) => (
              // One tooltip per window carries what used to be three separate
              // marks in the row: where the number came from, and when the
              // window rolls over. The bar and the percentage are the only
              // things that stay on screen.
              <Tooltip key={window.label}>
                <TooltipTrigger
                  render={
                    <span className="flex cursor-default items-center gap-1.5">
                      <span>{window.label}</span>
                      <span className="h-1.5 w-14 overflow-hidden rounded-full bg-muted">
                        <span
                          className={cn(
                            "block h-full",
                            barClass(window.utilization),
                            window.source !== "reported" && "opacity-60"
                          )}
                          style={{ width: `${Math.round((window.utilization ?? 0) * 100)}%` }}
                        />
                      </span>
                      <span className="tabular-nums">{percent(window.utilization)}</span>
                    </span>
                  }
                />
                <TooltipContent className="flex flex-col gap-0.5">
                  <span>
                    {window.source === "reported"
                      ? "제공자가 보고한 실제 사용률"
                      : window.source === "manual"
                        ? "직접 입력한 값에 이후 사용량을 더한 추정"
                        : "ANA가 집계한 토큰 기준 추정"}
                  </span>
                  {resetLabel(window.resets_at) ? (
                    <span className="text-muted-foreground">
                      초기화 {resetLabel(window.resets_at)}
                    </span>
                  ) : null}
                </TooltipContent>
              </Tooltip>
            ))}
          </div>
        ))
      )}

      <span className="ml-auto tabular-nums">
        이 대화 ${totalCostUsd.toFixed(4)} · {totalTokens.toLocaleString()} tok
      </span>
    </footer>
  );
}
