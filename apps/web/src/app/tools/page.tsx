"use client";

import { useCallback, useEffect, useState } from "react";
import { ArrowLeftIcon, RefreshCwIcon } from "lucide-react";
import { apiBase } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from "@/components/ui/empty";
import { Skeleton } from "@/components/ui/skeleton";
import { PageShell } from "@/components/page-shell";
import { toolDisplay } from "@/lib/tool-display";

type AuditEvent = {
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

const VERDICTS = ["all", "allow", "deny", "pending"] as const;

function commandOf(event: AuditEvent): string {
  return toolDisplay(event.tool_name, event.tool_input);
}

function verdictVariant(verdict: string) {
  if (verdict === "deny") return "destructive" as const;
  if (verdict === "pending") return "outline" as const;
  return "secondary" as const;
}

export default function ToolLogPage() {
  const [events, setEvents] = useState<AuditEvent[] | null>(null);
  const [verdict, setVerdict] = useState<(typeof VERDICTS)[number]>("all");

  const refresh = useCallback(async () => {
    try {
      const response = await fetch(`${apiBase()}/audit?limit=200`);
      if (!response.ok) throw new Error(String(response.status));
      setEvents(((await response.json()) as { events: AuditEvent[] }).events);
    } catch {
      setEvents([]);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // One row per tool call. The log keeps both the "pending" row and the
  // decision that followed (it is append-only, and that history matters), but
  // reading it as two separate calls is wrong — the newest row per
  // tool_use_id is the outcome.
  const collapsed: AuditEvent[] = [];
  const seen = new Set<string>();
  for (const event of events ?? []) {
    const key = event.tool_use_id;
    if (key) {
      if (seen.has(key)) continue;
      seen.add(key);
    }
    collapsed.push(event);
  }
  const shown = collapsed.filter((e) => verdict === "all" || e.verdict === verdict);

  return (
    <PageShell><div className="mx-auto flex w-full max-w-4xl flex-col gap-4 p-6">
      <header className="flex flex-wrap items-center gap-2">
        <Button size="xs" variant="ghost" render={<a href="/" />}>
          <ArrowLeftIcon data-icon="inline-start" />
          채팅
        </Button>
        <span className="font-heading text-sm font-semibold">도구 사용 로그</span>
        <div className="ml-auto flex items-center gap-1">
          {VERDICTS.map((option) => (
            <Button
              key={option}
              size="xs"
              variant={verdict === option ? "default" : "ghost"}
              onClick={() => setVerdict(option)}
            >
              {option}
            </Button>
          ))}
          <Button size="icon-sm" variant="ghost" onClick={() => void refresh()} title="새로고침">
            <RefreshCwIcon />
          </Button>
        </div>
      </header>

      <p className="text-xs text-muted-foreground">
        모든 도구 호출은 거버넌스 게이트를 거치고, 이 목록은 그 append-only 감사 로그를 그대로
        읽은 것. 시크릿 값은 기록 시점에 마스킹됨.
      </p>

      {events === null ? (
        <div className="flex flex-col gap-2">
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-10 w-full" />
        </div>
      ) : shown.length === 0 ? (
        <Empty>
          <EmptyHeader>
            <EmptyTitle>기록 없음</EmptyTitle>
            <EmptyDescription>에이전트가 도구를 쓰면 여기에 쌓임</EmptyDescription>
          </EmptyHeader>
        </Empty>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full border-collapse text-xs">
            <thead>
              <tr className="text-left text-muted-foreground">
                <th className="border-b border-border p-2 font-medium">시각</th>
                <th className="border-b border-border p-2 font-medium">에이전트</th>
                <th className="border-b border-border p-2 font-medium">명령</th>
                <th className="border-b border-border p-2 font-medium">판정</th>
                <th className="border-b border-border p-2 font-medium">규칙</th>
                <th className="border-b border-border p-2 font-medium">세션</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((event) => (
                <tr key={event.id} className="align-top">
                  <td className="border-b border-border p-2 whitespace-nowrap text-muted-foreground">
                    {new Date(event.created_at).toLocaleString()}
                  </td>
                  <td className="border-b border-border p-2">{event.agent ?? "—"}</td>
                  <td className="max-w-[26rem] border-b border-border p-2">
                    <code className="block truncate font-mono" title={commandOf(event)}>
                      {commandOf(event)}
                    </code>
                  </td>
                  <td className="border-b border-border p-2">
                    <Badge variant={verdictVariant(event.verdict)}>{event.verdict}</Badge>
                    <div className="mt-1 text-muted-foreground">{event.decided_by}</div>
                  </td>
                  <td className="border-b border-border p-2 text-muted-foreground">
                    {event.rule_id ?? "—"}
                    {event.category ? <div>{event.category}</div> : null}
                  </td>
                  <td className="max-w-[10rem] border-b border-border p-2">
                    <span className="block truncate text-muted-foreground" title={event.thread_id ?? ""}>
                      {event.thread_id ?? "—"}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div></PageShell>
  );
}
