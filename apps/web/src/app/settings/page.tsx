"use client";

import { useCallback, useEffect, useState } from "react";
import { SaveIcon } from "lucide-react";
import { fetchSettings, saveSettings } from "@/lib/api";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@/components/ui/field";
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
import { Textarea } from "@/components/ui/textarea";
import { notify } from "@/lib/notify";
import { PageShell } from "@/components/page-shell";
import { UsageEstimateEditor } from "@/components/usage-estimate-editor";
import { CapabilityPanel } from "@/components/capability-panel";

const AUTH_LABELS: Record<string, string> = {
  pro: "pro · 구독 세션",
  api: "api · 키 + base URL",
};

/** Settings the user should be able to change without touching the repo.
 *
 *  Four unrelated things used to sit in one scroll: the routing ladder, the
 *  system prompt, the billing mode, usage estimates and the capability list.
 *  Tabs put one subject on screen at a time, and the save button now belongs
 *  to the header rather than floating in the middle of the page. */
export default function SettingsPage() {
  const [routing, setRouting] = useState<string | null>(null);
  const [systemPrompt, setSystemPrompt] = useState("");
  const [authMode, setAuthMode] = useState("api");
  const [authModes, setAuthModes] = useState<string[]>(["pro", "api"]);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [dirty, setDirty] = useState(false);

  const load = useCallback(async () => {
    try {
      const settings = await fetchSettings();
      setRouting(settings.routing);
      setSystemPrompt(settings.system_prompt);
      if (settings.auth_mode) setAuthMode(settings.auth_mode);
      if (settings.auth_modes?.length) setAuthModes(settings.auth_modes);
    } catch (e) {
      setRouting("");
      setError(String(e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const save = async () => {
    setSaving(true);
    try {
      const stored = await saveSettings({
        routing: routing ?? "",
        system_prompt: systemPrompt,
        auth_mode: authMode,
      });
      setRouting(stored.routing);
      setSystemPrompt(stored.system_prompt);
      if (stored.auth_mode) setAuthMode(stored.auth_mode);
      setError(null);
      setDirty(false);
      notify.success("저장됨", "다음 턴부터 적용됩니다.");
    } catch (e) {
      // A rule file that does not parse is rejected before it is stored, so
      // routing cannot be taken down from this editor. The message names the
      // line, which is the useful part.
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  const loading = routing === null;

  return (
    <PageShell
      title="설정"
      description="여기서 바꾼 값은 재시작 없이 다음 턴부터 적용됩니다."
      actions={
        <Button size="sm" onClick={() => void save()} disabled={saving || loading}>
          <SaveIcon data-icon="inline-start" />
          {dirty ? "변경사항 저장" : "저장"}
        </Button>
      }
    >
      {error ? (
        <Alert variant="destructive">
          <AlertTitle>저장하지 못했습니다</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      ) : null}

      {loading ? (
        <div className="flex flex-col gap-3">
          <Skeleton className="h-9 w-72" />
          <Skeleton className="h-64 w-full" />
        </div>
      ) : (
        <Tabs defaultValue="routing">
          <TabsList>
            <TabsTrigger value="routing">라우팅</TabsTrigger>
            <TabsTrigger value="prompt">시스템 프롬프트</TabsTrigger>
            <TabsTrigger value="usage">사용량</TabsTrigger>
            <TabsTrigger value="capabilities">MCP · 스킬</TabsTrigger>
          </TabsList>

          <TabsContent value="routing">
            <FieldGroup>
              <Field>
                <FieldLabel htmlFor="routing">자동 라우팅 규칙</FieldLabel>
                <Textarea
                  id="routing"
                  value={routing ?? ""}
                  onChange={(event) => {
                    setRouting(event.target.value);
                    setDirty(true);
                  }}
                  spellCheck={false}
                  className="min-h-80 font-mono text-xs"
                />
                <FieldDescription>
                  각 단계는 CLI와 모델, 그리고 그 단계를 떠나는 사용률(`until`)입니다. 위에서부터
                  아직 여유가 있는 첫 단계가 그 턴을 맡습니다.
                </FieldDescription>
              </Field>

              <Field>
                <FieldLabel htmlFor="auth-mode">Claude 인증 모드</FieldLabel>
                <Select
                  value={authMode}
                  onValueChange={(value) => {
                    setAuthMode((value as string) ?? authMode);
                    setDirty(true);
                  }}
                >
                  <SelectTrigger id="auth-mode" className="w-72">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectGroup>
                      {authModes.map((mode) => (
                        <SelectItem key={mode} value={mode}>
                          {AUTH_LABELS[mode] ?? mode}
                        </SelectItem>
                      ))}
                    </SelectGroup>
                  </SelectContent>
                </Select>
                <FieldDescription>
                  ~/.bashrc의 use-claude-pro / use-claude-api에 해당합니다. 이미 열려 있는 대화는
                  시작할 때의 모드를 유지합니다.
                </FieldDescription>
              </Field>
            </FieldGroup>
          </TabsContent>

          <TabsContent value="prompt">
            <FieldGroup>
              <Field>
                <FieldLabel htmlFor="system-prompt">모든 에이전트에 덧붙일 지시</FieldLabel>
                <Textarea
                  id="system-prompt"
                  value={systemPrompt}
                  onChange={(event) => {
                    setSystemPrompt(event.target.value);
                    setDirty(true);
                  }}
                  placeholder="예: 답변은 한국어로, 코드 설명은 짧게."
                  className="min-h-48"
                />
                <FieldDescription>
                  각 CLI의 자기 시스템 프롬프트 뒤에 붙습니다. 비워두면 AnA는 아무 말도 얹지
                  않습니다.
                </FieldDescription>
              </Field>
            </FieldGroup>
          </TabsContent>

          <TabsContent value="usage">
            <UsageEstimateEditor />
          </TabsContent>

          <TabsContent value="capabilities">
            <CapabilityPanel />
          </TabsContent>
        </Tabs>
      )}
    </PageShell>
  );
}
