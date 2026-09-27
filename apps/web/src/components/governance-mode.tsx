"use client";

import { useCallback, useEffect, useState } from "react";
import { ShieldAlertIcon, ShieldCheckIcon } from "lucide-react";
import { fetchSettings, saveSettings } from "@/lib/api";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { notify } from "@/lib/notify";

const YOLO = "yolo";
const NORMAL = "normal";

/** Whether approvals still reach a human.
 *
 *  Its own control with its own save, rather than a field on the page's save
 *  button: this is the one setting where leaving a change unsaved — or saving
 *  one by accident along with an unrelated edit — is the failure. Turning it
 *  on asks once; turning it off is immediate, because nobody should have to
 *  confirm making things stricter. */
export function GovernanceMode() {
  const [mode, setMode] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const settings = await fetchSettings();
      setMode(settings.governance_mode ?? NORMAL);
    } catch (e) {
      setMode(NORMAL);
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const store = async (next: string) => {
    try {
      const stored = await saveSettings({ governance_mode: next });
      setMode(stored.governance_mode ?? next);
      setError(null);
      notify.success(
        next === YOLO ? "YOLO 모드 켜짐" : "일반 모드로 돌아감",
        // Not "다음 턴부터": the gate reads this per tool call.
        next === YOLO
          ? "다음 도구 호출부터 승인 카드 없이 실행됩니다."
          : "다음 도구 호출부터 다시 물어봅니다.",
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  if (mode === null) {
    return (
      <div className="flex flex-col gap-3">
        <Skeleton className="h-6 w-48" />
        <Skeleton className="h-20 w-full" />
      </div>
    );
  }

  const on = mode === YOLO;

  return (
    <FieldGroup>
      {error ? (
        <Alert variant="destructive">
          <AlertTitle>저장하지 못했습니다</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      ) : null}

      <Field orientation="horizontal">
        <FieldLabel htmlFor="yolo-mode" className="flex items-center gap-1.5">
          {on ? (
            <ShieldAlertIcon className="size-3.5 text-destructive" />
          ) : (
            <ShieldCheckIcon className="size-3.5" />
          )}
          YOLO 모드
        </FieldLabel>
        <Switch
          id="yolo-mode"
          checked={on}
          onCheckedChange={(next) => {
            if (next) setConfirming(true);
            else void store(NORMAL);
          }}
        />
      </Field>
      <FieldDescription>
        켜면 승인 카드를 띄우지 않고 AnA가 대신 &quot;허용&quot;으로 답합니다. 규칙이
        <code className="mx-1 font-mono">deny</code>로 막는 것, 시크릿 값이 명령에 섞인 경우,
        워크스페이스·하위 에이전트 권한 범위는 그대로 막힙니다. 감사 로그에는 사람이 아니라
        <code className="mx-1 font-mono">yolo</code>가 승인했다고 남습니다.
      </FieldDescription>

      {on ? (
        <Alert variant="destructive">
          <AlertTitle>지금 승인 없이 실행됩니다</AlertTitle>
          <AlertDescription>
            <code className="font-mono">git push</code>, 프로세스 종료, 워크스페이스 밖 경로,
            컨테이너 삭제처럼 원래 물어보던 동작이 바로 실행됩니다. 자격증명 파일
            (<code className="font-mono">.env</code>, <code className="font-mono">~/.ssh</code>,
            <code className="font-mono">*.pem</code> 등) 읽기는 이 모드에서도 거부됩니다.
          </AlertDescription>
        </Alert>
      ) : null}

      <AlertDialog open={confirming} onOpenChange={setConfirming}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>YOLO 모드를 켤까요?</AlertDialogTitle>
            <AlertDialogDescription>
              앞으로 승인 카드가 뜨지 않고, 원래 물어보던 도구 호출이 바로 실행됩니다.
              되돌리는 건 이 스위치를 다시 끄면 됩니다.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>취소</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                setConfirming(false);
                void store(YOLO);
              }}
            >
              켠다
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </FieldGroup>
  );
}
