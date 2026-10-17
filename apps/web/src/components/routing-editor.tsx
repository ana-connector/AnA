"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ArrowDownIcon, ArrowUpIcon, PlusIcon, XIcon } from "lucide-react";
import {
  fetchRouting,
  fetchUsage,
  saveRouting,
  type RoutingRung as Rung,
} from "@/lib/api";
import { cn } from "@/lib/utils";
import {
  ClaudeIcon,
  CursorIcon,
  KimiIcon,
  OpenAIIcon,
  OpenCodeIcon,
  TapieIcon,
} from "@/components/agent-icons";
import { AGENT_LABELS } from "@/components/ui/agent-toggle";
import { ModelPicker } from "@/components/model-picker";
import { MODEL_PICKER_THRESHOLD } from "@/lib/models";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Field, FieldDescription, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";


type Vocabulary = {
  agents: string[];
  efforts: Record<string, string[]>;
  /** What each CLI will accept, discovered where the CLI can be asked. */
  models: Record<string, string[]>;
  windows: { key: string; label: string }[];
  /** CLIs the settings page has switched off: still offered, because a rung
   *  may be prepared before its CLI is turned back on, but marked. */
  disabledAgents?: string[];
  /** Which of those windows each CLI actually has. */
  windowsByAgent: Record<string, string[]>;
};

type Ladders = { lead: Rung[]; workers: Record<string, Rung[]> };

type Usage = Record<string, { windows: { key: string; utilization: number | null }[] }>;

const NONE = "__none__";
const CUSTOM = "__custom__";

const AGENT_ICON: Record<string, (props: { className?: string }) => React.ReactElement> = {
  claude: ClaudeIcon,
  codex: OpenAIIcon,
  kimi: KimiIcon,
  tapie: TapieIcon,
  cursor: CursorIcon,
  opencode: OpenCodeIcon,
};

function spent(usage: Usage, agent: string, window: string): number | null {
  const found = (usage[agent]?.windows ?? []).find((entry) => entry.key === window);
  return found?.utilization ?? null;
}

/** Which rung answers right now, by the rule the router itself uses: the first
 *  one whose window still has room, and the last one when none does. */
function activeIndex(rungs: Rung[], usage: Usage, disabled: string[] = []): number {
  const off = new Set(disabled);
  for (let index = 0; index < rungs.length; index += 1) {
    const rung = rungs[index];
    // The router skips a switched-off CLI before it looks at any window, so
    // the mark has to as well, or the page points at a rung that cannot run.
    if (off.has(rung.agent)) continue;
    if (!rung.window || rung.utilization === null) return index;
    const used = spent(usage, rung.agent, rung.window) ?? 0;
    if (used < rung.utilization) return index;
  }
  return Math.max(rungs.length - 1, 0);
}

/** The routing ladder, as a ladder.
 *
 *  It was a YAML text box, then a row of five selects per rung — both of which
 *  asked the reader to hold the rule in their head: "the first rung with room
 *  takes the turn". So the rule is on screen instead. Each rung says what it
 *  costs, what would make ANA leave it, and how full that window is right now,
 *  and the one that would answer this second is marked. Editing a threshold
 *  then has a visible consequence rather than being a number in a box. */
export function RoutingEditor() {
  const [ladders, setLadders] = useState<Ladders | null>(null);
  const [vocabulary, setVocabulary] = useState<Vocabulary | null>(null);
  const [usage, setUsage] = useState<Usage>({});
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const loadState = useRef({ mounted: false, request: 0 });

  const load = useCallback(async () => {
    const request = ++loadState.current.request;
    await fetchRouting().then(
      (body) => {
        if (!loadState.current.mounted || loadState.current.request !== request) return;
        setLadders({ lead: body.lead, workers: body.workers ?? {} });
        setVocabulary({
          agents: body.agents,
          efforts: body.efforts,
          models: body.models ?? {},
          windows: body.windows,
          windowsByAgent: body.windows_by_agent ?? {},
          disabledAgents: body.disabled_agents ?? [],
        });
      },
      (e) => {
        if (loadState.current.mounted && loadState.current.request === request) setError(String(e));
      },
    );
    await fetchUsage().then(
      (agents) => {
        if (loadState.current.mounted && loadState.current.request === request) setUsage(agents);
      },
      () => {
        // The ladder is editable without live usage; it just reads flatter.
      },
    );
  }, []);

  useEffect(() => {
    const state = loadState.current;
    state.mounted = true;
    void load();
    return () => {
      state.mounted = false;
      state.request++;
    };
  }, [load]);

  const save = async () => {
    if (!ladders) return;
    setSaving(true);
    setError(null);
    try {
      const stored = await saveRouting(ladders);
      setLadders({ lead: stored.lead, workers: stored.workers ?? {} });
      setDirty(false);
    } catch (e) {
      setError(String(e));
    } finally {
      setSaving(false);
    }
  };

  if (!ladders || !vocabulary) {
    return (
      <div className="flex flex-col gap-3">
        <Skeleton className="h-9 w-56" />
        <Skeleton className="h-20 w-full" />
        <Skeleton className="h-20 w-full" />
      </div>
    );
  }

  const profiles = Object.keys(ladders.workers);

  const update = (name: string, rungs: Rung[]) => {
    setLadders((previous) => {
      if (!previous) return previous;
      return name === "lead"
        ? { ...previous, lead: rungs }
        : { ...previous, workers: { ...previous.workers, [name]: rungs } };
    });
    setDirty(true);
  };

  return (
    <div className="flex flex-col gap-4">
      {error ? (
        <Alert variant="destructive">
          <AlertTitle>저장하지 못했습니다</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      ) : null}

      <div className="flex flex-col gap-1">
        <h2 className="text-sm font-medium">자동 라우팅 규칙</h2>
        <p className="max-w-[70ch] text-sm text-muted-foreground">
          위에서부터 <strong className="font-medium text-foreground">아직 여유가 있는 첫
          단계</strong>가 그 턴을 맡습니다. 각 단계의 막대는 지금 그 창이 얼마나 찼는지이고,
          눈금은 다음 단계로 넘어가는 지점입니다.
        </p>
      </div>

      <Tabs defaultValue="lead">
        <TabsList>
          <TabsTrigger value="lead">대장</TabsTrigger>
          {profiles.map((name) => (
            <TabsTrigger key={name} value={name}>
              하위 에이전트{name === "default" ? "" : ` · ${name}`}
            </TabsTrigger>
          ))}
        </TabsList>

        <TabsContent value="lead">
          <Ladder
            rungs={ladders.lead}
            vocabulary={vocabulary}
            usage={usage}
            onChange={(rungs) => update("lead", rungs)}
          />
        </TabsContent>
        {profiles.map((name) => (
          <TabsContent key={name} value={name}>
            <Ladder
              rungs={ladders.workers[name]}
              vocabulary={vocabulary}
              usage={usage}
              onChange={(rungs) => update(name, rungs)}
            />
          </TabsContent>
        ))}
      </Tabs>

      <div className="flex items-center gap-3">
        <Button size="sm" onClick={save} disabled={!dirty || saving}>
          {saving ? "저장 중…" : "저장"}
        </Button>
        {dirty ? (
          <span className="text-xs text-muted-foreground">저장하지 않은 변경이 있습니다</span>
        ) : null}
      </div>
    </div>
  );
}

function Ladder({
  rungs,
  vocabulary,
  usage,
  onChange,
}: {
  rungs: Rung[];
  vocabulary: Vocabulary;
  usage: Usage;
  onChange: (rungs: Rung[]) => void;
}) {
  const active = useMemo(
    () => activeIndex(rungs, usage, vocabulary.disabledAgents ?? []),
    [rungs, usage, vocabulary.disabledAgents]
  );
  // Which rung is currently typing a model name by hand.
  const [customAt, setCustomAt] = useState<number | null>(null);

  const replace = (index: number, rung: Rung) =>
    onChange(rungs.map((item, position) => (position === index ? rung : item)));

  const move = (index: number, by: number) => {
    const target = index + by;
    if (target < 0 || target >= rungs.length) return;
    const next = [...rungs];
    [next[index], next[target]] = [next[target], next[index]];
    onChange(next);
  };

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-col">
        {rungs.map((rung, index) => {
          const last = index === rungs.length - 1;
          const Icon = AGENT_ICON[rung.agent];
          const efforts = vocabulary.efforts[rung.agent] ?? [];
          const models = vocabulary.models[rung.agent] ?? [];
          // A name the catalog does not carry is edited as text, so it is not
          // quietly replaced by whatever the dropdown lands on.
          const custom = customAt === index || (!!rung.model && !models.includes(rung.model));
          // A CLI with no usage window cannot carry a threshold: OpenCode
          // publishes no quota API, so there is no figure to compare against.
          const agentWindows =
            vocabulary.windowsByAgent[rung.agent] ?? vocabulary.windows.map((w) => w.key);
          const used = rung.window ? spent(usage, rung.agent, rung.window) : null;
          const threshold = rung.utilization;
          const over = used !== null && threshold !== null && used >= threshold;

          return (
            <div key={index} className="flex gap-3">
              {/* The rail: what makes this a ladder rather than a list. */}
              <div className="flex w-6 shrink-0 flex-col items-center">
                <span
                  className={cn(
                    "flex size-6 items-center justify-center rounded-full border text-[11px] tabular-nums",
                    index === active
                      ? "border-primary bg-primary text-primary-foreground"
                      : "border-border text-muted-foreground"
                  )}
                >
                  {index + 1}
                </span>
                {!last ? <span className="w-px flex-1 bg-border" /> : null}
              </div>

              <div
                className={cn(
                  "mb-3 flex min-w-0 flex-1 flex-col gap-3 rounded-lg border p-3",
                  index === active ? "border-primary/60 bg-muted/40" : "border-border"
                )}
              >
                <div className="flex flex-wrap items-center gap-2">
                  {Icon ? <Icon className="size-4 shrink-0" /> : null}
                  <span className="text-sm font-medium">
                    {AGENT_LABELS[rung.agent as keyof typeof AGENT_LABELS] ?? rung.agent}
                  </span>
                  {rung.model ? (
                    <span className="font-mono text-xs text-muted-foreground">{rung.model}</span>
                  ) : null}
                  {(vocabulary.disabledAgents ?? []).includes(rung.agent) ? (
                    // The rule is kept, the router just skips it: saying so
                    // here is the difference between "spent" and "off".
                    <Badge variant="outline">설정에서 꺼둔 CLI · 건너뜁니다</Badge>
                  ) : index === active ? (
                    <Badge variant="secondary">지금 이 단계가 답합니다</Badge>
                  ) : null}
                  {last ? <Badge variant="outline">마지막</Badge> : null}

                  <div className="ml-auto flex items-center gap-1">
                    <Button
                      size="icon-sm"
                      variant="ghost"
                      aria-label="한 단계 위로"
                      disabled={index === 0}
                      onClick={() => move(index, -1)}
                    >
                      <ArrowUpIcon />
                    </Button>
                    <Button
                      size="icon-sm"
                      variant="ghost"
                      aria-label="한 단계 아래로"
                      disabled={last}
                      onClick={() => move(index, 1)}
                    >
                      <ArrowDownIcon />
                    </Button>
                    <Button
                      size="icon-sm"
                      variant="ghost"
                      aria-label="이 단계 삭제"
                      disabled={rungs.length === 1}
                      onClick={() => onChange(rungs.filter((_, position) => position !== index))}
                    >
                      <XIcon />
                    </Button>
                  </div>
                </div>

                <div className="grid gap-3 sm:grid-cols-3">
                  <Field>
                    <FieldLabel>CLI</FieldLabel>
                    <Select
                      value={rung.agent}
                      onValueChange={(value) =>
                        // Effort vocabularies differ per CLI, so a level the new
                        // agent does not have is dropped rather than carried over
                        // to be rejected on save.
                        replace(index, {
                          ...rung,
                          agent: String(value),
                          effort: (vocabulary.efforts[String(value)] ?? []).includes(rung.effort)
                            ? rung.effort
                            : "",
                        })
                      }
                    >
                      <SelectTrigger aria-label={`${index + 1}단계 CLI`}>
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectGroup>
                          {vocabulary.agents.map((agent) => (
                            <SelectItem key={agent} value={agent}>
                              {AGENT_LABELS[agent as keyof typeof AGENT_LABELS] ?? agent}
                            </SelectItem>
                          ))}
                        </SelectGroup>
                      </SelectContent>
                    </Select>
                  </Field>

                  <Field>
                    <FieldLabel>모델</FieldLabel>
                    {/* A picker, because a model name is something the CLI
                        either knows or rejects a turn later. CUSTOM keeps the
                        text box for a name this build has not heard of. */}
                    {custom ? (
                      <Input
                        autoFocus
                        aria-label={`${index + 1}단계 모델 이름`}
                        value={rung.model}
                        placeholder="opus"
                        className="font-mono"
                        onChange={(event) => replace(index, { ...rung, model: event.target.value })}
                        onBlur={() => {
                          if (!rung.model.trim()) setCustomAt(null);
                        }}
                      />
                    ) : models.length > MODEL_PICKER_THRESHOLD ? (
                      // OpenCode's list is dozens of `provider/model` names, so
                      // this rung searches instead of scrolling.
                      <ModelPicker
                        label={`${index + 1}단계 모델`}
                        value={rung.model}
                        models={models}
                        onChange={(model) => replace(index, { ...rung, model })}
                        onCustom={() => setCustomAt(index)}
                      />
                    ) : (
                      <Select
                        value={rung.model || NONE}
                        onValueChange={(value) => {
                          if (value === CUSTOM) {
                            setCustomAt(index);
                            return;
                          }
                          replace(index, { ...rung, model: value === NONE ? "" : String(value) });
                        }}
                      >
                        <SelectTrigger className="font-mono" aria-label={`${index + 1}단계 모델`}>
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectGroup>
                            <SelectItem value={NONE}>CLI 기본값</SelectItem>
                            {models.map((name) => (
                              <SelectItem key={name} value={name} className="font-mono">
                                {name}
                              </SelectItem>
                            ))}
                            <SelectItem value={CUSTOM}>직접 입력…</SelectItem>
                          </SelectGroup>
                        </SelectContent>
                      </Select>
                    )}
                  </Field>

                  <Field>
                    <FieldLabel>사고 강도</FieldLabel>
                    <Select
                      value={rung.effort || NONE}
                      onValueChange={(value) =>
                        replace(index, { ...rung, effort: value === NONE ? "" : String(value) })
                      }
                    >
                      <SelectTrigger aria-label={`${index + 1}단계 사고 강도`}>
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectGroup>
                          <SelectItem value={NONE}>CLI 기본값</SelectItem>
                          {efforts.map((level) => (
                            <SelectItem key={level} value={level}>
                              {level}
                            </SelectItem>
                          ))}
                        </SelectGroup>
                      </SelectContent>
                    </Select>
                  </Field>
                </div>

                {/* The condition as a sentence, because that is how the rule
                    reads: leave this rung when the window passes the mark. */}
                {last ? (
                  <p className="text-xs text-muted-foreground">
                    마지막 단계에는 한도가 없습니다. 앞 단계가 모두 소진되면 여기로 떨어집니다.
                  </p>
                ) : agentWindows.length === 0 ? (
                  <p className="text-xs text-muted-foreground">
                    이 CLI는 한도 API가 없어 사용률을 읽을 수 없습니다. 조건을 걸 수 없으니
                    마지막 단계로 두세요 — 앞 단계가 모두 소진되면 여기로 떨어집니다.
                  </p>
                ) : (
                  <div className="flex flex-col gap-2">
                    <div className="flex flex-wrap items-center gap-2 text-sm">
                      <Select
                        value={rung.window || NONE}
                        onValueChange={(value) =>
                          replace(index, {
                            ...rung,
                            window: value === NONE ? "" : String(value),
                            // A threshold without a window is not a rung the
                            // router can read, so the two move together.
                            utilization: value === NONE ? null : (rung.utilization ?? 0.5),
                          })
                        }
                      >
                        <SelectTrigger size="sm" className="w-28" aria-label={`${index + 1}단계 사용량 창`}>
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectGroup>
                            <SelectItem value={NONE}>한도 없음</SelectItem>
                            {vocabulary.windows
                              .filter((window) =>
                                (vocabulary.windowsByAgent[rung.agent] ?? [window.key]).includes(
                                  window.key
                                )
                              )
                              .map((window) => (
                                <SelectItem key={window.key} value={window.key}>
                                  {window.label}
                                </SelectItem>
                              ))}
                          </SelectGroup>
                        </SelectContent>
                      </Select>
                      <span className="text-muted-foreground">창이</span>
                      <Input
                        type="number"
                        aria-label={`${index + 1}단계에서 다음 단계로 넘어갈 사용률 (%)`}
                        min={0}
                        max={100}
                        step={5}
                        disabled={!rung.window}
                        className="w-20 text-right tabular-nums"
                        value={threshold === null ? "" : Math.round(threshold * 100)}
                        onChange={(event) => {
                          const percent = Number(event.target.value);
                          replace(index, {
                            ...rung,
                            utilization: Number.isFinite(percent)
                              ? Math.min(Math.max(percent, 0), 100) / 100
                              : null,
                          });
                        }}
                      />
                      <span className="text-muted-foreground">% 를 넘으면 다음 단계로</span>
                    </div>

                    {rung.window ? (
                      <Tooltip>
                        <TooltipTrigger
                          render={
                            <div className="flex cursor-default items-center gap-2">
                              <span className="relative h-1.5 w-full max-w-72 overflow-hidden rounded-full bg-muted">
                                <span
                                  className={cn(
                                    "block h-full rounded-full",
                                    over ? "bg-destructive" : "bg-primary/60"
                                  )}
                                  style={{ width: `${Math.round((used ?? 0) * 100)}%` }}
                                />
                                {threshold !== null ? (
                                  <span
                                    className="absolute top-0 h-full w-px bg-foreground/70"
                                    style={{ left: `${Math.round(threshold * 100)}%` }}
                                  />
                                ) : null}
                              </span>
                              <span className="shrink-0 text-[11px] tabular-nums text-muted-foreground">
                                {used === null ? "측정 전" : `${Math.round(used * 100)}%`}
                              </span>
                            </div>
                          }
                        />
                        <TooltipContent>
                          {over
                            ? "지금은 한도를 넘어 다음 단계로 넘어갑니다"
                            : "지금은 한도 안이라 이 단계가 쓰입니다"}
                        </TooltipContent>
                      </Tooltip>
                    ) : null}
                  </div>
                )}
              </div>
            </div>
          );
        })}
      </div>

      <div className="flex items-center gap-3 pl-9">
        <Button
          size="sm"
          variant="outline"
          onClick={() =>
            onChange([
              ...rungs,
              { agent: vocabulary.agents[0] ?? "claude", model: "", effort: "", window: "", utilization: null },
            ])
          }
        >
          <PlusIcon data-icon="inline-start" />
          단계 추가
        </Button>
        <FieldDescription>
          새 단계는 맨 아래에 붙습니다. 화살표로 순서를 바꾸세요.
        </FieldDescription>
      </div>
    </div>
  );
}
