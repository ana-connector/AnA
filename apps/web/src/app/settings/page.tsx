"use client";

import { useCallback, useEffect, useState } from "react";
import { ArrowLeftIcon } from "lucide-react";
import { fetchSettings, saveSettings } from "@/lib/api";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { PageShell } from "@/components/page-shell";
import { UsageEstimateEditor } from "@/components/usage-estimate-editor";

/** Settings the user should be able to change without touching the repo: the
 *  rules that pick a CLI per message, and the instructions appended to every
 *  agent. Both take effect on the next turn — no restart. */
export default function SettingsPage() {
  const [routing, setRouting] = useState<string | null>(null);
  const [systemPrompt, setSystemPrompt] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  const load = useCallback(async () => {
    try {
      const settings = await fetchSettings();
      setRouting(settings.routing);
      setSystemPrompt(settings.system_prompt);
    } catch (e) {
      setRouting("");
      setError(String(e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const save = async () => {
    try {
      const saved = await saveSettings({
        routing: routing ?? "",
        system_prompt: systemPrompt,
      });
      setRouting(saved.routing);
      setSystemPrompt(saved.system_prompt);
      setError(null);
      setSaved(true);
    } catch (e) {
      // A rule file that does not parse is rejected before it is stored, so
      // routing cannot be taken down from this editor.
      setError(e instanceof Error ? e.message : String(e));
      setSaved(false);
    }
  };

  return (
    <PageShell><div className="mx-auto flex w-full max-w-3xl flex-col gap-4 p-6">
      <header className="flex items-center gap-2">
        <Button size="xs" variant="ghost" render={<a href="/" />}>
          <ArrowLeftIcon data-icon="inline-start" />
          채팅
        </Button>
        <span className="font-heading text-sm font-semibold">설정</span>
        {saved && <Badge variant="secondary">저장됨 · 다음 턴부터 적용</Badge>}
      </header>

      {error && (
        <Alert variant="destructive">
          <AlertTitle>저장하지 못함</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      {routing === null ? (
        <div className="flex flex-col gap-2">
          <Skeleton className="h-48 w-full" />
          <Skeleton className="h-24 w-full" />
        </div>
      ) : (
        <FieldGroup>
          <Field>
            <FieldLabel htmlFor="routing">자동 라우팅 규칙</FieldLabel>
            <Textarea
              id="routing"
              value={routing}
              onChange={(e) => setRouting(e.target.value)}
              className="min-h-72 font-mono text-xs"
              spellCheck={false}
            />
            <FieldDescription>
              lead와 workers의 단계를 위에서부터 검사합니다. 각 단계의 사용량이 until 기준에 도달하면
              다음 단계로 넘어갑니다. 저장한 규칙은 다음 턴부터 적용됩니다.
            </FieldDescription>
          </Field>

          <Field>
            <FieldLabel htmlFor="system-prompt">시스템 프롬프트 (추가 지시)</FieldLabel>
            <Textarea
              id="system-prompt"
              value={systemPrompt}
              onChange={(e) => setSystemPrompt(e.target.value)}
              className="min-h-36 text-sm"
              placeholder="예: 답변은 한국어로. 코드 수정 전에는 계획을 먼저 말해라."
            />
            <FieldDescription>
              두 CLI 모두에 적용됨 — Claude는
              <code className="mx-1 font-mono">--append-system-prompt</code>로, Codex는 일회성
              실행에 해당 플래그가 없어 프롬프트 앞에 덧붙임. 비우면 아무것도 추가하지 않음.
            </FieldDescription>
          </Field>

          <Field orientation="horizontal">
            <Button onClick={save}>저장</Button>
            <Button variant="ghost" onClick={() => void load()}>
              되돌리기
            </Button>
          </Field>
        </FieldGroup>
      )}
      <UsageEstimateEditor />
    </div></PageShell>
  );
}
