"use client";

import { useRef, useState } from "react";
import {
  ArchiveIcon,
  ArchiveRestoreIcon,
  ImageIcon,
  RepeatIcon,
  SaveIcon,
  TriangleAlertIcon,
} from "lucide-react";
import {
  RequestError,
  forgetWorkspace,
  mergeBranch,
  updateWorkspace,
  type MergeResult,
  type WorkspaceSummary,
} from "@/lib/api";
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
import { WorkspaceIcon } from "@/components/workspace-icon";
import { fileToIcon } from "@/lib/icon-file";
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
  const [icon, setIcon] = useState(workspace.icon ?? "");
  const iconFile = useRef<HTMLInputElement>(null);
  const [systemPrompt, setSystemPrompt] = useState(workspace.system_prompt ?? "");
  const [error, setError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const isBranch = workspace.parent_id !== null;
  const [merging, setMerging] = useState(false);
  const [merge, setMerge] = useState<MergeResult | null>(null);
  // Set when the branch holds commits main does not: the delete came back
  // refused, and the next press is the user saying to drop them anyway.
  const [forceDelete, setForceDelete] = useState(false);

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

  const saveIcon = async () => {
    try {
      await updateWorkspace(workspace.id, { icon });
      setError(null);
      await onSaved();
    } catch (e) {
      setError(String(e));
    }
  };

  /** A picked file becomes the icon as soon as it is small enough to be one;
   *  it is saved with the same button as an emoji, so one preview answers
   *  "what will this look like" for both. */
  const pickIcon = async (file: File | undefined) => {
    if (!file) return;
    try {
      setIcon(await fileToIcon(file));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const remove = async () => {
    setDeleting(true);
    try {
      await forgetWorkspace(workspace.id, forceDelete);
      setConfirming(false);
      setForceDelete(false);
      await onDeleted();
    } catch (e) {
      // A branch that still holds unmerged commits is refused rather than
      // deleted: that branch is the only copy of that work. Offer the force
      // instead of hiding the refusal behind a generic error — but only for
      // that refusal. Arming it after a 500 or a dropped connection would
      // force past a guard the user never actually hit.
      setError(e instanceof Error ? e.message : String(e));
      if (isBranch && e instanceof RequestError && e.status === 409) setForceDelete(true);
      setConfirming(false);
    } finally {
      setDeleting(false);
    }
  };

  /** Brings the branch's work into its project.
   *
   *  Every outcome but a server fault comes back as a status, so this sets
   *  the result rather than throwing: a dirty project and a conflict are
   *  ordinary answers the user acts on, not errors. */
  const mergeIntoMain = async (commitProject = false) => {
    setMerging(true);
    try {
      setMerge(await mergeBranch(workspace.id, commitProject));
      setError(null);
      await onSaved();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setMerging(false);
    }
  };

  const mergeMessage = (result: MergeResult): string => {
    switch (result.status) {
      case "merged":
        return `「${workspace.base_branch}」에 머지했다.`;
      case "empty":
        return "머지할 게 없다. 이 브랜치에는 새 작업이 없다.";
      case "dirty":
        return `「${workspace.base_branch}」 체크아웃에 커밋 안 된 변경이 있다. 먼저 커밋해야 머지된다.`;
      case "moved":
        return result.detail;
      case "missing":
        return `이 브랜치의 체크아웃이 디스크에 없다: ${result.detail}`;
      case "conflict":
        return `충돌: ${result.paths.join(", ")}`;
      case "unresolved":
        return `아직 해결되지 않은 충돌이 있다: ${result.paths.join(", ")} — 이 브랜치의 대화에서 끝내야 머지된다.`;
      case "failed":
        return `git이 머지를 거부했다. 아무것도 바뀌지 않았다:\n${result.detail}`;
      default:
        return result.detail || result.status;
    }
  };

  /** Archiving is the answer to "I am done with this project" that deleting was
   *  being used for. Nothing is destroyed: the row, the folder and every turn
   *  stay, and the workspace is still openable from the archive. */
  const setArchived = async (archived: boolean) => {
    try {
      await updateWorkspace(workspace.id, { archived });
      setError(null);
      await onSaved();
    } catch (e) {
      setError(String(e));
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

      {isBranch && (
        <FieldGroup>
          <Field>
            <FieldLabel>브랜치</FieldLabel>
            <FieldDescription className="font-mono break-all">
              {workspace.branch} → {workspace.base_branch}
            </FieldDescription>
          </Field>
          <Field orientation="horizontal">
            {/* Disabled while a conflict is still open: pressing again
                cannot merge, and the only thing that moves it forward is the
                branch's agent finishing in its own conversation. */}
            <Button
              size="sm"
              disabled={merging || merge?.status === "unresolved"}
              onClick={() => void mergeIntoMain()}
            >
              {merging ? "머지 중…" : `${workspace.base_branch}으로 머지`}
            </Button>
            {/* The project tree is dirty most of the time — main's lead agent
                edits it directly and ANA never commits for it — so this is
                the ordinary second press, not a hidden escape hatch. */}
            {merge?.status === "dirty" && (
              <Button
                size="sm"
                variant="outline"
                disabled={merging}
                onClick={() => void mergeIntoMain(true)}
              >
                커밋하고 머지
              </Button>
            )}
          </Field>
          {merge && (
            <FieldDescription
              className={
                merge.status === "merged" || merge.status === "empty"
                  ? undefined
                  : "text-destructive"
              }
            >
              {mergeMessage(merge)}
              {merge.status === "conflict" &&
                " 이 브랜치의 대화에 충돌 해결을 맡겼다. 끝나면 다시 머지해라."}
            </FieldDescription>
          )}
        </FieldGroup>
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
          <FieldLabel htmlFor={`ws-icon-${workspace.id}`}>아이콘</FieldLabel>
          <div className="flex items-center gap-2">
            {/* The box is always there, so an empty one reads as "no icon
                yet" rather than as a missing control. */}
            <div className="flex size-9 shrink-0 items-center justify-center rounded-md border border-border">
              <WorkspaceIcon icon={icon} className="size-6 text-xl" />
            </div>
            <Input
              id={`ws-icon-${workspace.id}`}
              value={icon.startsWith("data:") ? "" : icon}
              onChange={(event) => setIcon(event.target.value)}
              placeholder="🛠️"
              disabled={icon.startsWith("data:")}
              className="w-24 text-center"
              aria-label="아이콘 이모지"
            />
            <input
              ref={iconFile}
              type="file"
              accept="image/png,image/svg+xml,image/jpeg,image/webp,image/gif"
              hidden
              onChange={(event) => {
                void pickIcon(event.target.files?.[0]);
                // Cleared so picking the same file twice still fires.
                event.target.value = "";
              }}
            />
            <Button size="sm" variant="outline" onClick={() => iconFile.current?.click()}>
              <ImageIcon data-icon="inline-start" />
              이미지 올리기
            </Button>
            {icon && (
              <Button size="sm" variant="ghost" onClick={() => setIcon("")}>
                지우기
              </Button>
            )}
          </div>
          <FieldDescription>
            이모지 한 글자나 작은 이미지(png · svg · jpeg · webp). 올린 이미지는 64px로
            줄여서 워크스페이스와 함께 저장하고, 사이드바·시작 화면·탭에 쓴다.
          </FieldDescription>
        </Field>
        <Field orientation="horizontal">
          <Button
            size="sm"
            disabled={icon === (workspace.icon ?? "")}
            onClick={() => void saveIcon()}
          >
            <SaveIcon data-icon="inline-start" />
            아이콘 저장
          </Button>
        </Field>
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

      <FieldGroup>
        <Field orientation="horizontal">
          <Button size="sm" variant="outline" onClick={() => void setArchived(!workspace.archived)}>
            {workspace.archived ? (
              <>
                <ArchiveRestoreIcon data-icon="inline-start" />
                보관 해제
              </>
            ) : (
              <>
                <ArchiveIcon data-icon="inline-start" />
                보관하기
              </>
            )}
          </Button>
        </Field>
        <FieldDescription>
          {workspace.archived
            ? "목록 맨 아래 보관함에 있다. 해제하면 다시 위 목록으로 올라온다."
            : "사이드바 맨 아래 보관함으로 옮긴다. 대화와 폴더는 그대로 남고 언제든 다시 열 수 있다."}
        </FieldDescription>
      </FieldGroup>

      <WorkspaceSettings
        key={workspace.id}
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
            <AlertDialogTitle>
              {isBranch ? `브랜치 「${workspace.name}」 삭제` : `「${workspace.name}」 삭제`}
            </AlertDialogTitle>
            <AlertDialogDescription>
              대화가 사라진다: 턴 {workspace.turn_count}개와 체크포인트를 지우므로 되돌릴 수 없다.
              {isBranch ? (
                <>
                  {" "}
                  이 브랜치의 체크아웃({shortPath(workspace.path)})과 git 브랜치(
                  {workspace.branch})도 함께 지운다.
                  {forceDelete &&
                    ` ${workspace.base_branch}에 아직 없는 커밋이 있다 — 삭제하면 그 작업은 되찾을 수 없다.`}
                </>
              ) : (
                <>
                  {" "}
                  폴더({shortPath(workspace.path)})와 감사 로그는 그대로 남고, 전용 컨테이너는
                  정리된다. 이 프로젝트의 브랜치는 체크아웃까지 함께 지워진다.
                </>
              )}
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
              {deleting ? "삭제 중…" : forceDelete ? "그래도 삭제" : "삭제"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
