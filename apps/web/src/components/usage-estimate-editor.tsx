"use client";

import { useEffect, useState } from "react";
import { apiBase } from "@/lib/api";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";

const rows = [
  { agent: "claude", window: "five_hour", label: "Claude · 5시간" },
  { agent: "claude", window: "seven_day", label: "Claude · 7일" },
  { agent: "codex", window: "five_hour", label: "Codex · 5시간" },
  { agent: "codex", window: "seven_day", label: "Codex · 7일" },
  { agent: "kimi", window: "five_hour", label: "Kimi · 5시간" },
  { agent: "kimi", window: "seven_day", label: "Kimi · 7일" },
  { agent: "tapie", window: "five_hour", label: "TAPIE · 5시간" },
  { agent: "tapie", window: "seven_day", label: "TAPIE · 7일" },
] as const;

type Estimate = { utilization: number; resets_at: number | null };
type Estimates = Record<string, Record<string, Estimate>>;

const pad = (value: number) => String(value).padStart(2, "0");

/** Epoch seconds to the value shape each input wants: a five-hour window
 * resets later today or tomorrow, so the clock time is enough; a weekly one
 * needs the date as well. */
function toInputValue(epochSeconds: number | null, window: string): string {
  if (epochSeconds === null) return "";
  const at = new Date(epochSeconds * 1000);
  const clock = `${pad(at.getHours())}:${pad(at.getMinutes())}`;
  if (window === "five_hour") return clock;
  return `${at.getFullYear()}-${pad(at.getMonth() + 1)}-${pad(at.getDate())}T${clock}`;
}

/** The inverse, as an ISO instant. A bare clock time means the next time it
 * comes round — today if it is still ahead, otherwise tomorrow. */
function toIso(value: string, window: string): string | null {
  const trimmed = value.trim();
  if (trimmed === "") return null;
  if (window !== "five_hour") {
    const at = new Date(trimmed);
    if (Number.isNaN(at.getTime())) throw new Error("초기화 시각을 읽지 못했습니다.");
    return at.toISOString();
  }
  const [hours, minutes] = trimmed.split(":").map(Number);
  if (!Number.isFinite(hours) || !Number.isFinite(minutes)) {
    throw new Error("초기화 시각을 읽지 못했습니다.");
  }
  const at = new Date();
  at.setHours(hours, minutes, 0, 0);
  if (at.getTime() <= Date.now()) at.setDate(at.getDate() + 1);
  return at.toISOString();
}

export function UsageEstimateEditor() {
  const [values, setValues] = useState<Record<string, string>>({});
  const [resets, setResets] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    void fetch(`${apiBase()}/usage/estimates`)
      .then(async (response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const { estimates } = (await response.json()) as { estimates: Estimates };
        setValues(Object.fromEntries(rows.map(({ agent, window }) => {
          const entry = estimates[agent]?.[window];
          return [`${agent}.${window}`, entry === undefined ? "" : String(Math.round(entry.utilization * 100))];
        })));
        setResets(Object.fromEntries(rows.map(({ agent, window }) => {
          const entry = estimates[agent]?.[window];
          return [`${agent}.${window}`, toInputValue(entry?.resets_at ?? null, window)];
        })));
      })
      .catch((cause) => setError(String(cause)));
  }, []);

  const save = async () => {
    setSaved(false);
    try {
      for (const { agent, window } of rows) {
        const key = `${agent}.${window}`;
        const raw = values[key] ?? "";
        const amount = raw.trim() === "" ? null : Number(raw) / 100;
        if (amount !== null && (!Number.isFinite(amount) || amount < 0 || amount > 1)) {
          throw new Error("사용량은 0~100%로 입력해 주세요.");
        }
        // A reset time on its own says nothing; it belongs to a figure.
        const resetsAt = amount === null ? null : toIso(resets[key] ?? "", window);
        const response = await fetch(`${apiBase()}/usage/estimates`, {
          method: "PUT",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ agent, window, utilization: amount, resets_at: resetsAt }),
        });
        if (!response.ok) {
          const detail = await response.json().then((body) => body?.detail).catch(() => null);
          throw new Error(detail ? `${label(agent, window)}: ${detail}` : `사용량 저장 실패: HTTP ${response.status}`);
        }
      }
      setError(null);
      setSaved(true);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  };

  return (
    <FieldGroup>
      <Field>
        <FieldLabel>현재 사용량 추정치</FieldLabel>
        <FieldDescription>
          이미 쓴 양을 %로 입력하면 다음 자동 라우팅부터 반영됩니다. 비우면 AnA가 관측한 토큰만 사용합니다.
          이후 AnA에서 쓴 토큰은 입력값에 더해집니다. 제공자가 보고한 수치가 있으면 그 수치가 우선합니다.
        </FieldDescription>
        <FieldDescription>
          초기화 시각을 함께 넣으면 그 시점에 입력값이 사라집니다(창이 리셋되므로).
          5시간 창은 시각만, 7일 창은 날짜까지 지정합니다. 비워두면 입력 시점부터 창 길이만큼만 유지됩니다.
        </FieldDescription>
      </Field>
      <div className="grid gap-3 sm:grid-cols-2">
        {rows.map(({ agent, window, label: rowLabel }) => {
          const key = `${agent}.${window}`;
          return (
            <Field key={key}>
              <FieldLabel htmlFor={key}>{rowLabel}</FieldLabel>
              <Input id={key} type="number" min="0" max="100" step="1" placeholder="자동 추정"
                value={values[key] ?? ""} onChange={(event) => { setValues((current) => ({ ...current, [key]: event.target.value })); setSaved(false); }} />
              <Input id={`${key}.resets`} aria-label={`${rowLabel} 초기화 시각`}
                type={window === "five_hour" ? "time" : "datetime-local"}
                value={resets[key] ?? ""}
                onChange={(event) => { setResets((current) => ({ ...current, [key]: event.target.value })); setSaved(false); }} />
              <FieldDescription>
                {window === "five_hour" ? "초기화 시각 (오늘/내일 중 먼저 오는 시각)" : "초기화 일시"}
              </FieldDescription>
            </Field>
          );
        })}
      </div>
      {error && <Alert variant="destructive"><AlertDescription>{error}</AlertDescription></Alert>}
      <Field orientation="horizontal">
        <Button onClick={() => void save()}>추정치 저장</Button>
        {saved && <FieldDescription>저장됨 · 다음 턴부터 적용</FieldDescription>}
      </Field>
    </FieldGroup>
  );
}

function label(agent: string, window: string): string {
  return rows.find((row) => row.agent === agent && row.window === window)?.label ?? `${agent} ${window}`;
}
