"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { RefreshCwIcon } from "lucide-react";
import { fetchAuditEvents, type AuditEvent } from "@/lib/api";
import { timeAgo, timeExact } from "@/lib/format";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from "@/components/ui/empty";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { PageShell } from "@/components/page-shell";
import { toolDisplay } from "@/lib/tool-display";


const FILTERS = [
  { value: "all", label: "전체" },
  { value: "allow", label: "허용" },
  { value: "deny", label: "거부" },
  { value: "pending", label: "대기" },
] as const;

const VERDICT_LABEL: Record<string, string> = {
  allow: "허용",
  deny: "거부",
  pending: "대기",
};

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
  const [verdict, setVerdict] = useState<string>("all");
  const [selected, setSelected] = useState<AuditEvent | null>(null);
  const [failed, setFailed] = useState(false);
  const loadState = useRef({ mounted: false, request: 0 });

  const refresh = useCallback(() => {
    const request = ++loadState.current.request;
    return fetchAuditEvents().then(
      (events) => {
        if (!loadState.current.mounted || loadState.current.request !== request) return;
        setEvents(events);
        setFailed(false);
      },
      () => {
        if (!loadState.current.mounted || loadState.current.request !== request) return;
        setEvents([]);
        setFailed(true);
      },
    );
  }, []);

  useEffect(() => {
    const state = loadState.current;
    state.mounted = true;
    void refresh();
    return () => {
      state.mounted = false;
      state.request++;
    };
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
  const counts = collapsed.reduce<Record<string, number>>((all, event) => {
    all[event.verdict] = (all[event.verdict] ?? 0) + 1;
    return all;
  }, {});

  return (
    <PageShell
      title="도구 사용 로그"
      description="모든 도구 호출은 거버넌스 게이트를 거칩니다. 이 목록은 삭제할 수 없는 감사 로그를 그대로 읽은 것이고, 시크릿 값은 기록 시점에 마스킹됩니다."
      actions={
        <Button size="icon-sm" variant="ghost" onClick={() => void refresh()} aria-label="새로고침">
          <RefreshCwIcon />
        </Button>
      }
    >
      <div className="flex flex-wrap items-center gap-2">
        <ToggleGroup
          value={[verdict]}
          onValueChange={(value) => setVerdict((value as string[])[0] ?? "all")}
          variant="outline"
          size="sm"
        >
          {FILTERS.map((option) => (
            <ToggleGroupItem key={option.value} value={option.value}>
              {option.label}
              {option.value !== "all" && counts[option.value] ? (
                <span className="text-muted-foreground tabular-nums">
                  {counts[option.value]}
                </span>
              ) : null}
            </ToggleGroupItem>
          ))}
        </ToggleGroup>
        {events !== null ? (
          <span className="text-xs text-muted-foreground tabular-nums">
            {shown.length}건
          </span>
        ) : null}
      </div>

      {events === null ? (
        <div className="flex flex-col gap-2">
          <Skeleton className="h-9 w-full" />
          <Skeleton className="h-9 w-full" />
          <Skeleton className="h-9 w-full" />
        </div>
      ) : shown.length === 0 ? (
        <Empty>
          <EmptyHeader>
            <EmptyTitle>{failed ? "로그를 불러오지 못했습니다" : "기록 없음"}</EmptyTitle>
            <EmptyDescription>
              {failed
                ? "서버에 연결할 수 없습니다. 새로고침으로 다시 시도하세요."
                : verdict === "all"
                  ? "에이전트가 도구를 쓰면 여기에 쌓입니다."
                  : "이 판정에 해당하는 기록이 없습니다."}
            </EmptyDescription>
          </EmptyHeader>
        </Empty>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="w-24">시각</TableHead>
              <TableHead className="w-24">에이전트</TableHead>
              <TableHead>명령</TableHead>
              <TableHead className="w-28">판정</TableHead>
              <TableHead className="w-40">규칙</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {shown.map((event) => (
              <TableRow
                key={event.id}
                onClick={() => setSelected(event)}
                className="cursor-pointer"
              >
                <TableCell className="text-muted-foreground">
                  <Tooltip>
                    <TooltipTrigger render={<span className="whitespace-nowrap" />}>
                      {timeAgo(event.created_at)}
                    </TooltipTrigger>
                    <TooltipContent>{timeExact(event.created_at)}</TooltipContent>
                  </Tooltip>
                </TableCell>
                <TableCell className="text-muted-foreground">{event.agent ?? "—"}</TableCell>
                <TableCell className="max-w-0">
                  <code className="block truncate font-mono text-xs">{commandOf(event)}</code>
                </TableCell>
                <TableCell>
                  <Badge variant={verdictVariant(event.verdict)}>
                    {VERDICT_LABEL[event.verdict] ?? event.verdict}
                  </Badge>
                </TableCell>
                <TableCell className="text-xs text-muted-foreground">
                  {event.rule_id ?? "—"}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}

      {/* The row is a summary; the call itself is the evidence. Truncated
          text with a title attribute was the only way to read it before. */}
      <Sheet open={selected !== null} onOpenChange={(open) => !open && setSelected(null)}>
        <SheetContent className="flex w-full flex-col gap-4 overflow-y-auto sm:max-w-xl">
          <SheetHeader>
            <SheetTitle className="font-mono text-sm">{selected?.tool_name}</SheetTitle>
            <SheetDescription>
              {selected ? `${timeExact(selected.created_at)} · ${selected.agent ?? "에이전트 미상"}` : ""}
            </SheetDescription>
          </SheetHeader>

          {selected ? (
            <div className="flex flex-col gap-4 px-4 pb-4 text-sm">
              <div className="flex flex-wrap items-center gap-2">
                <Badge variant={verdictVariant(selected.verdict)}>
                  {VERDICT_LABEL[selected.verdict] ?? selected.verdict}
                </Badge>
                <Badge variant="outline">{selected.decided_by}</Badge>
                {selected.rule_id ? <Badge variant="outline">{selected.rule_id}</Badge> : null}
                {selected.category ? (
                  <Badge variant="outline">{selected.category}</Badge>
                ) : null}
              </div>

              {selected.reason ? (
                <div className="flex flex-col gap-1">
                  <span className="text-xs font-medium text-muted-foreground">사유</span>
                  <p className="whitespace-pre-wrap">{selected.reason}</p>
                </div>
              ) : null}

              <div className="flex flex-col gap-1">
                <span className="text-xs font-medium text-muted-foreground">호출 내용</span>
                <pre className="max-h-96 overflow-auto rounded-md bg-muted p-3 font-mono text-xs whitespace-pre-wrap">
                  {JSON.stringify(selected.tool_input ?? {}, null, 2)}
                </pre>
              </div>

              <div className="flex flex-col gap-1">
                <span className="text-xs font-medium text-muted-foreground">대화</span>
                <code className="font-mono text-xs break-all">{selected.thread_id ?? "—"}</code>
              </div>
            </div>
          ) : null}
        </SheetContent>
      </Sheet>
    </PageShell>
  );
}
