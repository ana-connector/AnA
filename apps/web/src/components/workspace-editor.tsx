"use client";

import { useState } from "react";
import { RepeatIcon, SaveIcon, TriangleAlertIcon } from "lucide-react";
import { forgetWorkspace, updateWorkspace, type WorkspaceSummary } from "@/lib/api";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogMedia,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { WorkspaceSettings } from "@/components/workspace-settings";

function shortPath(path: string): string {
  const parts = path.split("/").filter(Boolean);
  return parts.length ? `…/${parts[parts.length - 1]}` : path;
}

/** Everything you can do to one workspace: rename it, give it a container, or
 *  delete it.
 *
 *  One component because there are two places that offer this — the dialog in
 *  the sidebar and the workspace's own page — and they had drifted: the page
 *  deleted without asking and without noticing a failure, which is exactly the
 *  part that must not have two implementations. */
export function WorkspaceEditor({
  workspace,
  isSelf = false,
  onSaved,
  onDeleted,
}: {
  workspace: WorkspaceSummary;
  /** Whether this workspace is ANA's own source tree. */
  isSelf?: boolean;
  onSaved: () => void | Promise<void>;
  /** Called after the workspace is gone, so the caller can decide where the
   *  user should be looking now. */
  onDeleted: () => void | Promise<void>;
}) {
  const [name, setName] = useState(workspace.name);
  const [systemPrompt, setSystemPrompt] = useState(workspace.system_prompt ?? "");
  const [error, setError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [deleting, setDeleting] = useState(false);

  const rename = async () => {
    try {
      await updateWorkspace(workspace.id, { name });
      setError(null);
      await onSaved();
    } catch (e) {
      // A failed rename used to reject silently and leave the old name on
      // screen with no explanation.
      setError(String(e));
    }
  };

  const remove = async () => {
    setDeleting(true);
    try {
      await forgetWorkspace(workspace.id);
      setConfirming(false);
      await onDeleted();
    } catch (e) {
      setError(String(e));
      setConfirming(false);
    } finally {
      setDeleting(false);
    }
  };

  const savePrompt = async () => {
    try {
      await updateWorkspace(workspace.id, { system_prompt: systemPrompt });
      setError(null);
      await onSaved();
    } catch (e) {
      setError(String(e));
    }
  };

  return (
    <div className="flex flex-col gap-5">
      {isSelf && (
        <div className="flex items-start gap-2 rounded-md border border-border px-3 py-2 text-xs text-muted-foreground">
          <RepeatIcon className="mt-0.5 size-3.5 shrink-0" />
          <span>
            AnA 자기 자신의 소스 트리다. 규칙과 스킬 파일은 다음 턴부터, 서버 코드는 다시
            빌드한 뒤부터 적용된다. 게이트 규칙 파일을 건드리는 도구 호출은 승인을 받는다.
          </span>
        </div>
      )}

      <FieldGroup>
        <Field>
          <FieldLabel htmlFor={`ws-rename-${workspace.id}`}>이름</FieldLabel>
          <Input
            id={`ws-rename-${workspace.id}`}
            value={name}
            onChange={(event) => setName(event.target.value)}
          />
          <FieldDescription className="font-mono break-all">{workspace.path}</FieldDescription>
        </Field>
        <Field orientation="horizontal">
          <Button
            size="sm"
            disabled={!name.trim() || name === workspace.name}
            onClick={() => void rename()}
          >
            <SaveIcon data-icon="inline-start" />
            이름 저장
          </Button>
        </Field>
        {error && <FieldDescription className="text-destructive">{error}</FieldDescription>}
      </FieldGroup>

      <FieldGroup>
        <Field>
          <FieldLabel htmlFor={`ws-prompt-${workspace.id}`}>워크스페이스 시스템 프롬프트</FieldLabel>
          <Textarea
            id={`ws-prompt-${workspace.id}`}
            value={systemPrompt}
            onChange={(event) => setSystemPrompt(event.target.value)}
            placeholder="이 워크스페이스에서 에이전트가 따를 지침을 입력하세요."
            className="min-h-32"
          />
          <FieldDescription>전역 시스템 프롬프트 뒤에 붙으며 다음 턴부터 적용됩니다.</FieldDescription>
        </Field>
        <Field orientation="horizontal">
          <Button
            size="sm"
            disabled={systemPrompt === (workspace.system_prompt ?? "")}
            onClick={() => void savePrompt()}
          >
            <SaveIcon data-icon="inline-start" />
            프롬프트 저장
          </Button>
        </Field>
      </FieldGroup>

      <WorkspaceSettings
        workspace={workspace}
        onSaved={() => void onSaved()}
        onForget={() => setConfirming(true)}
      />

      <AlertDialog open={confirming} onOpenChange={(open) => { if (!open) setConfirming(false); }}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogMedia>
              <TriangleAlertIcon className="text-destructive" />
            </AlertDialogMedia>
            <AlertDialogTitle>「{workspace.name}」 삭제</AlertDialogTitle>
            <AlertDialogDescription>
              대화가 사라진다: 턴 {workspace.turn_count}개와 체크포인트를 지우므로 되돌릴 수 없다.
              폴더({shortPath(workspace.path)})와 감사 로그는 그대로 남고, 전용 컨테이너는
              정리된다.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel size="sm">취소</AlertDialogCancel>
            <AlertDialogAction
              size="sm"
              variant="destructive"
              disabled={deleting}
              onClick={() => void remove()}
            >
              {deleting ? "삭제 중…" : "삭제"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
