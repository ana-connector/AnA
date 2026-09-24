"use client";

import { useCallback, useEffect, useState } from "react";
import { ArrowDownIcon, ArrowUpIcon, PlusIcon, XIcon } from "lucide-react";
import { apiBase } from "@/lib/api";
import { AGENT_LABELS } from "@/components/ui/agent-toggle";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Item, ItemContent, ItemGroup } from "@/components/ui/item";
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

type Rung = {
  agent: string;
  model: string;
  effort: string;
  window: string;
  utilization: number | null;
};

type Vocabulary = {
  agents: string[];
  efforts: Record<string, string[]>;
  windows: { key: string; label: string }[];
};

type Ladders = { lead: Rung[]; workers: Record<string, Rung[]> };

const NONE = "__none__";

function emptyRung(agent: string): Rung {
  return { agent, model: "", effort: "", window: "", utilization: null };
}

/** The routing ladder, as rungs instead of YAML.
 *
 *  It used to be a text box holding the rules file, which asked whoever wanted
 *  to move one threshold to know the format — and made a typo a routing outage
 *  discovered on the next turn. The vocabulary comes from the server with the
 *  rules, so every field offers exactly what the router accepts. */
export function RoutingEditor() {
  const [ladders, setLadders] = useState<Ladders | null>(null);
  const [vocabulary, setVocabulary] = useState<Vocabulary | null>(null);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const body = await (await fetch(`${apiBase()}/routing`)).json();
      setLadders({ lead: body.lead, workers: body.workers ?? {} });
      setVocabulary({ agents: body.agents, efforts: body.efforts, windows: body.windows });
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const save = async () => {
    if (!ladders) return;
    setSaving(true);
    setError(null);
    try {
      const response = await fetch(`${apiBase()}/routing`, {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(ladders),
      });
      if (!response.ok) {
        throw new Error(((await response.json()) as { detail?: string }).detail ?? response.statusText);
      }
      const stored = await response.json();
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
      <div className="flex flex-col gap-2">
        <Skeleton className="h-9 w-full" />
        <Skeleton className="h-24 w-full" />
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
    <FieldGroup>
      {error ? (
        <Alert variant="destructive">
          <AlertTitle>저장하지 못했습니다</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      ) : null}

      <Field>
        <FieldLabel>자동 라우팅 규칙</FieldLabel>
        <FieldDescription>
          위에서부터 아직 여유가 있는 첫 단계가 그 턴을 맡습니다. 마지막 단계에는 한도를 두지
          않습니다 — 앞 단계가 모두 소진됐을 때 여기로 떨어집니다.
        </FieldDescription>
      </Field>

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
            onChange={(rungs) => update("lead", rungs)}
          />
        </TabsContent>
        {profiles.map((name) => (
          <TabsContent key={name} value={name}>
            <Ladder
              rungs={ladders.workers[name]}
              vocabulary={vocabulary}
              onChange={(rungs) => update(name, rungs)}
            />
          </TabsContent>
        ))}
      </Tabs>

      <Field orientation="horizontal">
        <Button size="sm" onClick={save} disabled={!dirty || saving}>
          {saving ? "저장 중…" : "저장"}
        </Button>
        {dirty ? <FieldDescription>저장하지 않은 변경이 있습니다</FieldDescription> : null}
      </Field>
    </FieldGroup>
  );
}

function Ladder({
  rungs,
  vocabulary,
  onChange,
}: {
  rungs: Rung[];
  vocabulary: Vocabulary;
  onChange: (rungs: Rung[]) => void;
}) {
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
    <div className="flex flex-col gap-2">
      <ItemGroup className="gap-2">
        {rungs.map((rung, index) => {
          const last = index === rungs.length - 1;
          const efforts = vocabulary.efforts[rung.agent] ?? [];
          return (
            <Item key={index} variant="outline" className="flex-wrap items-end gap-3 py-3">
              <ItemContent className="min-w-0 flex-row flex-wrap items-end gap-3">
                <Field className="w-36">
                  <FieldLabel>단계 {index + 1}</FieldLabel>
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
                    <SelectTrigger>
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

                <Field className="w-44">
                  <FieldLabel>모델</FieldLabel>
                  <Input
                    value={rung.model}
                    placeholder="opus"
                    onChange={(event) => replace(index, { ...rung, model: event.target.value })}
                  />
                </Field>

                <Field className="w-32">
                  <FieldLabel>사고 강도</FieldLabel>
                  <Select
                    value={rung.effort || NONE}
                    onValueChange={(value) =>
                      replace(index, { ...rung, effort: value === NONE ? "" : String(value) })
                    }
                  >
                    <SelectTrigger>
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

                <Field className="w-36">
                  <FieldLabel>다음으로 넘어갈 창</FieldLabel>
                  <Select
                    value={rung.window || NONE}
                    onValueChange={(value) =>
                      replace(index, {
                        ...rung,
                        window: value === NONE ? "" : String(value),
                        // A threshold without a window is not a rung the router
                        // can read, so the two move together.
                        utilization: value === NONE ? null : (rung.utilization ?? 0.5),
                      })
                    }
                  >
                    <SelectTrigger>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectGroup>
                        <SelectItem value={NONE}>없음 (마지막 단계)</SelectItem>
                        {vocabulary.windows.map((window) => (
                          <SelectItem key={window.key} value={window.key}>
                            {window.label}
                          </SelectItem>
                        ))}
                      </SelectGroup>
                    </SelectContent>
                  </Select>
                </Field>

                <Field className="w-28">
                  <FieldLabel>사용률</FieldLabel>
                  <Input
                    type="number"
                    min={0}
                    max={100}
                    step={5}
                    disabled={!rung.window}
                    value={rung.utilization === null ? "" : Math.round(rung.utilization * 100)}
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
                </Field>
              </ItemContent>

              <div className="flex items-center gap-1">
                <Button
                  size="icon-sm"
                  variant="ghost"
                  aria-label="위로"
                  disabled={index === 0}
                  onClick={() => move(index, -1)}
                >
                  <ArrowUpIcon />
                </Button>
                <Button
                  size="icon-sm"
                  variant="ghost"
                  aria-label="아래로"
                  disabled={last}
                  onClick={() => move(index, 1)}
                >
                  <ArrowDownIcon />
                </Button>
                <Button
                  size="icon-sm"
                  variant="ghost"
                  aria-label="이 단계 삭제"
                  // The ladder always needs somewhere for a turn to land.
                  disabled={rungs.length === 1}
                  onClick={() => onChange(rungs.filter((_, position) => position !== index))}
                >
                  <XIcon />
                </Button>
              </div>
            </Item>
          );
        })}
      </ItemGroup>

      <Button
        size="sm"
        variant="outline"
        className="w-fit"
        onClick={() => onChange([...rungs, emptyRung(vocabulary.agents[0] ?? "claude")])}
      >
        <PlusIcon data-icon="inline-start" />
        단계 추가
      </Button>
    </div>
  );
}
