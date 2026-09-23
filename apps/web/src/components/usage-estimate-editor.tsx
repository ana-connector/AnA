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
] as const;

type Estimates = Record<string, Record<string, number>>;

export function UsageEstimateEditor() {
  const [values, setValues] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    void fetch(`${apiBase()}/usage/estimates`)
      .then(async (response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const { estimates } = (await response.json()) as { estimates: Estimates };
        setValues(Object.fromEntries(rows.map(({ agent, window }) => {
          const value = estimates[agent]?.[window];
          return [`${agent}.${window}`, value === undefined ? "" : String(Math.round(value * 100))];
        })));
      })
      .catch((cause) => setError(String(cause)));
  }, []);

  const save = async () => {
    setSaved(false);
    try {
      for (const { agent, window } of rows) {
        const raw = values[`${agent}.${window}`] ?? "";
        const amount = raw.trim() === "" ? null : Number(raw) / 100;
        if (amount !== null && (!Number.isFinite(amount) || amount < 0 || amount > 1)) {
          throw new Error("사용량은 0~100%로 입력해 주세요.");
        }
        const response = await fetch(`${apiBase()}/usage/estimates`, {
          method: "PUT",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ agent, window, utilization: amount }),
        });
        if (!response.ok) throw new Error(`사용량 저장 실패: HTTP ${response.status}`);
      }
      setError(null);
      setSaved(true);
    } catch (cause) {
      setError(String(cause));
    }
  };

  return (
    <FieldGroup>
      <Field>
        <FieldLabel>현재 사용량 추정치</FieldLabel>
        <FieldDescription>
          이미 쓴 양을 %로 입력하면 다음 자동 라우팅부터 반영됩니다. 비우면 ANA가 관측한 토큰만 사용합니다.
          이후 ANA에서 쓴 토큰은 입력값에 더해집니다. 제공자가 보고한 수치가 있으면 그 수치가 우선합니다.
        </FieldDescription>
      </Field>
      <div className="grid gap-3 sm:grid-cols-2">
        {rows.map(({ agent, window, label }) => {
          const key = `${agent}.${window}`;
          return (
            <Field key={key}>
              <FieldLabel htmlFor={key}>{label}</FieldLabel>
              <Input id={key} type="number" min="0" max="100" step="1" placeholder="자동 추정"
                value={values[key] ?? ""} onChange={(event) => { setValues((current) => ({ ...current, [key]: event.target.value })); setSaved(false); }} />
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
