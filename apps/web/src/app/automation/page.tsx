"use client";

import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { ClockIcon, PlayIcon, TrashIcon } from "lucide-react";
import {
  createSchedule,
  deleteSchedule,
  fetchSchedules,
  fetchWorkspaces,
  patchSchedule,
  runScheduleNow,
  type ScheduleRow,
  type WorkspaceSummary,
} from "@/lib/api";
import { timeAgo, timeExact } from "@/lib/format";
import { notify } from "@/lib/notify";
import { PageShell } from "@/components/page-shell";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from "@/components/ui/empty";
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Item, ItemActions, ItemContent, ItemGroup, ItemTitle } from "@/components/ui/item";
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

/** Common shapes, so the usual job takes one click rather than five fields of
 *  cron. The expression is still shown and still editable — these are a
 *  starting point, not a replacement for it. */
const PRESETS = [
  { label: "매일 아침 9시", cron: "0 9 * * *" },
  { label: "평일 아침 9시", cron: "0 9 * * 1-5" },
  { label: "매시 정각", cron: "0 * * * *" },
  { label: "30분마다", cron: "*/30 * * * *" },
  { label: "월요일 아침 9시", cron: "0 9 * * 1" },
  { label: "매월 1일 자정", cron: "0 0 1 * *" },
];

/** Scheduled turns: a prompt, a workspace and a clock.
 *
 *  When one comes due, ANA opens that workspace's conversation and submits the
 *  prompt as if it had been typed — so the answer lands in the conversation,
 *  and the gate, the sub-agents and the transcript all work as they always do.
 *  Agents can create these themselves through the `schedule` MCP server, which
 *  is why the list says who made each one. */
export default function AutomationPage() {
  const [schedules, setSchedules] = useState<ScheduleRow[] | null>(null);
  const [workspaces, setWorkspaces] = useState<WorkspaceSummary[]>([]);
  const [workspaceId, setWorkspaceId] = useState("");
  const [name, setName] = useState("");
  const [cron, setCron] = useState("0 9 * * *");
  const [prompt, setPrompt] = useState("");
  const [error, setError] = useState<string | null>(null);
  const loadState = useRef({ mounted: false, request: 0 });

  const refresh = useCallback(() => {
    const request = ++loadState.current.request;
    return fetchSchedules().then(
      (items) => {
        if (loadState.current.mounted && loadState.current.request === request) setSchedules(items);
      },
      (e) => {
        if (!loadState.current.mounted || loadState.current.request !== request) return;
        setSchedules([]);
        setError(String(e));
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

  useEffect(() => {
    let active = true;
    void fetchWorkspaces()
      .then((items) => {
        if (!active) return;
        setWorkspaces(items);
        // Whichever conversation was last open: the workspace is nearly always
        // the one you were just in.
        const remembered = window.localStorage.getItem("ana_thread_id");
        setWorkspaceId(
          items.find((item) => item.id === remembered)?.id ?? items[0]?.id ?? ""
        );
      })
      .catch(() => {});
    return () => { active = false; };
  }, []);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!workspaceId || !name.trim() || !prompt.trim()) return;
    try {
      await createSchedule({ workspace_id: workspaceId, name, cron, prompt });
      setName("");
      setPrompt("");
      setError(null);
      await refresh();
      notify.success("자동화를 등록했습니다");
    } catch (e) {
      setError(String(e));
    }
  };

  const toggle = async (row: ScheduleRow) => {
    await patchSchedule(row.id, { enabled: !row.enabled });
    await refresh();
  };

  const remove = async (row: ScheduleRow) => {
    await deleteSchedule(row.id);
    await refresh();
  };

  const runNow = async (row: ScheduleRow) => {
    try {
      await runScheduleNow(row.id);
      notify.success(`「${row.name}」를 지금 실행했습니다`);
    } catch (e) {
      notify.error("실행하지 못했습니다", String(e));
    }
  };

  const nameOf = (id: string) => workspaces.find((item) => item.id === id)?.name ?? id;

  return (
    <PageShell
      title="자동화"
      description="정해진 시각에 대화에 프롬프트를 보냅니다. 답은 그 워크스페이스의 대화에 남고, 승인과 하위 에이전트도 평소와 똑같이 동작합니다. 시각은 UTC 기준입니다."
    >
      {error ? (
        <Alert variant="destructive">
          <AlertTitle>처리하지 못했습니다</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      ) : null}

      <section className="flex flex-col gap-2">
        <h2 className="text-xs font-medium text-muted-foreground">새 자동화</h2>
        <form onSubmit={submit}>
          <FieldGroup>
            <div className="grid gap-4 sm:grid-cols-2">
              <Field>
                <FieldLabel htmlFor="auto-name">이름</FieldLabel>
                <Input
                  id="auto-name"
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                  placeholder="아침 브리핑"
                />
              </Field>
              <Field>
                <FieldLabel htmlFor="auto-workspace">워크스페이스</FieldLabel>
                <Select value={workspaceId} onValueChange={(value) => setWorkspaceId(String(value))}>
                  <SelectTrigger id="auto-workspace">
                    <SelectValue placeholder="고르기" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectGroup>
                      {workspaces.map((item) => (
                        <SelectItem key={item.id} value={item.id}>
                          {item.name}
                        </SelectItem>
                      ))}
                    </SelectGroup>
                  </SelectContent>
                </Select>
              </Field>
            </div>

            <Field>
              <FieldLabel htmlFor="auto-cron">언제</FieldLabel>
              <div className="flex flex-wrap gap-1.5">
                {PRESETS.map((preset) => (
                  <Button
                    key={preset.cron}
                    type="button"
                    size="xs"
                    variant={cron === preset.cron ? "default" : "outline"}
                    onClick={() => setCron(preset.cron)}
                  >
                    {preset.label}
                  </Button>
                ))}
              </div>
              <Input
                id="auto-cron"
                value={cron}
                onChange={(event) => setCron(event.target.value)}
                className="font-mono"
                placeholder="0 9 * * *"
              />
              <FieldDescription>
                분 시 일 월 요일. 위 버튼을 누르면 채워지고, 직접 고쳐도 됩니다.
              </FieldDescription>
            </Field>

            <Field>
              <FieldLabel htmlFor="auto-prompt">보낼 요청</FieldLabel>
              <Textarea
                id="auto-prompt"
                value={prompt}
                onChange={(event) => setPrompt(event.target.value)}
                className="min-h-24"
                placeholder="어제 커밋을 읽고 오늘 할 일을 정리해줘"
              />
              <FieldDescription>
                몇 달 뒤에 읽어도 뜻이 통하게 적으세요. 「아까 그거」는 그때 아무 의미가 없습니다.
              </FieldDescription>
            </Field>

            <Field orientation="horizontal">
              <Button type="submit" size="sm" disabled={!workspaceId || !name.trim() || !prompt.trim()}>
                <ClockIcon data-icon="inline-start" />
                등록
              </Button>
            </Field>
          </FieldGroup>
        </form>
      </section>

      <section className="flex flex-col gap-2">
        <h2 className="text-xs font-medium text-muted-foreground">등록된 자동화</h2>
        {schedules === null ? (
          <div className="flex flex-col gap-2">
            <Skeleton className="h-14 w-full" />
            <Skeleton className="h-14 w-full" />
          </div>
        ) : schedules.length === 0 ? (
          <Empty className="py-8">
            <EmptyHeader>
              <EmptyTitle>아직 없음</EmptyTitle>
              <EmptyDescription>
                위에서 하나 만들거나, 대화에서 「매일 아침에 ~해줘」라고 말하면 에이전트가 직접
                등록합니다.
              </EmptyDescription>
            </EmptyHeader>
          </Empty>
        ) : (
          <ItemGroup className="gap-2">
            {schedules.map((row) => (
              <Item key={row.id} variant="outline" className="items-start gap-3 py-3">
                <ItemContent className="min-w-0 gap-1">
                  <ItemTitle className="w-full min-w-0 flex-wrap">
                    <span className="truncate">{row.name}</span>
                    <Badge variant="outline">{row.cron_text}</Badge>
                    {row.created_by === "agent" ? (
                      <Badge variant="secondary">에이전트가 등록</Badge>
                    ) : null}
                    {row.last_status === "error" ? (
                      <Tooltip>
                        <TooltipTrigger render={<Badge variant="destructive">직전 실패</Badge>} />
                        <TooltipContent>{row.last_detail || "이유 없음"}</TooltipContent>
                      </Tooltip>
                    ) : null}
                  </ItemTitle>
                  <p className="line-clamp-2 text-sm text-muted-foreground">{row.prompt}</p>
                  <p className="text-[11px] text-muted-foreground">
                    {nameOf(row.workspace_id)} · <span className="font-mono">{row.cron}</span>
                    {row.next_run_at ? (
                      <>
                        {" · 다음 "}
                        <Tooltip>
                          <TooltipTrigger render={<span>{timeAgo(row.next_run_at)}</span>} />
                          <TooltipContent>{timeExact(row.next_run_at)}</TooltipContent>
                        </Tooltip>
                      </>
                    ) : null}
                    {row.last_run_at ? ` · 마지막 ${timeAgo(row.last_run_at)}` : ""}
                  </p>
                </ItemContent>
                <ItemActions className="gap-1">
                  <Tooltip>
                    <TooltipTrigger
                      render={
                        <Button
                          size="icon-sm"
                          variant="ghost"
                          aria-label="지금 실행"
                          onClick={() => void runNow(row)}
                        >
                          <PlayIcon />
                        </Button>
                      }
                    />
                    <TooltipContent>지금 한 번 실행 (다음 예정 시각은 그대로)</TooltipContent>
                  </Tooltip>
                  <Switch
                    checked={row.enabled}
                    onCheckedChange={() => void toggle(row)}
                    aria-label={row.enabled ? "일시정지" : "다시 켜기"}
                  />
                  <Button
                    size="icon-sm"
                    variant="ghost"
                    aria-label="삭제"
                    onClick={() => void remove(row)}
                  >
                    <TrashIcon />
                  </Button>
                </ItemActions>
              </Item>
            ))}
          </ItemGroup>
        )}
      </section>
    </PageShell>
  );
}
