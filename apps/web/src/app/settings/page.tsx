"use client";

import { useCallback, useEffect, useState } from "react";
import { SaveIcon } from "lucide-react";
import { fetchSettings, saveSettings } from "@/lib/api";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { notify } from "@/lib/notify";
import { PageShell } from "@/components/page-shell";
import { RoutingEditor } from "@/components/routing-editor";
import { UsageEstimateEditor } from "@/components/usage-estimate-editor";
import { CapabilityPanel } from "@/components/capability-panel";
import { GovernanceMode } from "@/components/governance-mode";


/** Settings the user should be able to change without touching the repo.
 *
 *  Four unrelated things used to sit in one scroll: the routing ladder, the
 *  system prompt, the billing mode, usage estimates and the capability list.
 *  Tabs put one subject on screen at a time, and the save button now belongs
 *  to the header rather than floating in the middle of the page. */
export default function SettingsPage() {
  const [routing, setRouting] = useState<string | null>(null);
  const [systemPrompt, setSystemPrompt] = useState("");
  // Claude runs on the subscription; the key path is the `tapie` agent now,
  // and the ladder is where you choose between them.
  const authMode = "pro";
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [dirty, setDirty] = useState(false);

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
    setSaving(true);
    try {
      const stored = await saveSettings({
        routing: routing ?? "",
        system_prompt: systemPrompt,
        auth_mode: authMode,
      });
      setRouting(stored.routing);
      setSystemPrompt(stored.system_prompt);
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
            <TabsTrigger value="permissions">권한</TabsTrigger>
          </TabsList>

          <TabsContent value="routing">
            {/* Its own editor, with its own save: the ladder is structured
                data, and the rest of this page is two text fields. */}
            <RoutingEditor />
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

          <TabsContent value="permissions">
            {/* Its own save, like the routing ladder: a switch that decides
                whether anything gets asked at all should not be waiting on the
                header's save button. */}
            <GovernanceMode />
          </TabsContent>
        </Tabs>
      )}
    </PageShell>
  );
}
