"use client";

import { useCallback, useEffect, useState } from "react";
import { apiBase } from "@/lib/api";
import { Badge } from "@/components/ui/badge";

type Window = {
  label: string;
  tokens: number;
  budget: number;
  utilization: number | null;
  source: "reported" | "estimated" | "manual";
};

type UsageSnapshot = Record<string, { windows: Window[] }>;

function percent(value: number | null): string {
  return value === null ? "—" : `${Math.round(value * 100)}%`;
}

function barClass(value: number | null): string {
  if (value === null) return "bg-muted-foreground/40";
  if (value >= 0.9) return "bg-destructive";
  if (value >= 0.7) return "bg-primary";
  return "bg-primary/60";
}

/** Status bar: how close each CLI is to its limits.
 *
 *  The two sources are labeled rather than blended — the Claude CLI reports
 *  real window utilization, while the Codex figure is ANA's own estimate from
 *  the tokens it has seen against a configured budget. Showing an estimate as
 *  if it were the provider's number would be the worst of both. */
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
    <footer className="flex shrink-0 flex-wrap items-center gap-x-5 gap-y-2 border-t border-border px-4 py-4 text-xs text-muted-foreground">
      {agents.length === 0 ? (
        <span>사용량 집계 없음</span>
      ) : (
        agents.map(([agent, data]) => (
          <div key={agent} className="flex items-center gap-2">
            <span className="font-medium text-foreground">{agent}</span>
            {data.windows.map((window) => (
              <span key={window.label} className="flex items-center gap-1.5">
                <span>{window.label}</span>
                <span className="h-1.5 w-16 overflow-hidden rounded-full bg-muted">
                  <span
                    className={`block h-full ${barClass(window.utilization)}`}
                    style={{ width: `${Math.round((window.utilization ?? 0) * 100)}%` }}
                  />
                </span>
                <span>{percent(window.utilization)}</span>
                {window.source === "estimated" && (
                  <Badge variant="outline" title="ANA가 집계한 토큰 기준 추정치">
                    추정
                  </Badge>
                )}
                {window.source === "manual" && (
                  <Badge variant="outline" title="설정에서 입력한 현재 사용량에 이후 ANA 사용량을 더한 추정치">
                    수동 추정
                  </Badge>
                )}
              </span>
            ))}
          </div>
        ))
      )}

      <span className="ml-auto">
        이 세션 ${totalCostUsd.toFixed(4)} · {totalTokens.toLocaleString()} tok
      </span>
    </footer>
  );
}
