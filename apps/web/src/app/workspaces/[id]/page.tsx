"use client";

import { useCallback, useEffect, useState, use } from "react";
import { SaveIcon } from "lucide-react";
import {
  fetchWorkspaces,
  forgetWorkspace,
  updateWorkspace,
  type WorkspaceSummary,
} from "@/lib/api";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { WorkspaceSettings } from "@/components/workspace-settings";
import { PageShell } from "@/components/page-shell";
import { notify } from "@/lib/notify";

/** One workspace's own page: what it is called, where it works, and whether it
 *  runs in its own container. Reached by hovering the workspace in the sidebar,
 *  which keeps the list itself to one line per project. */
export default function WorkspacePage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const [workspace, setWorkspace] = useState<WorkspaceSummary | null | undefined>(undefined);
  const [name, setName] = useState("");

  const load = useCallback(async () => {
    const all = await fetchWorkspaces();
    const found = all.find((w) => w.id === id) ?? null;
    setWorkspace(found);
    if (found) setName(found.name);
  }, [id]);

  useEffect(() => {
    void load();
  }, [load]);

  if (workspace === undefined) {
    return (
      <PageShell title="워크스페이스">
        <Skeleton className="h-8 w-40" />
        <Skeleton className="h-32 w-full" />
      </PageShell>
    );
  }

  if (workspace === null) {
    return (
      <PageShell title="워크스페이스">
        <Alert variant="destructive">
          <AlertTitle>없는 워크스페이스</AlertTitle>
          <AlertDescription>
            <span className="font-mono">{id}</span> 는 등록돼 있지 않습니다. 사이드바에서 다른
            워크스페이스를 고르거나 새로 만드세요.
          </AlertDescription>
        </Alert>
        <Button size="sm" variant="outline" render={<a href="/" />} className="w-fit">
          채팅으로 돌아가기
        </Button>
      </PageShell>
    );
  }

  return (
    <PageShell
      title={workspace.name}
      description={
        <>
          이 워크스페이스의 대화와 폴더 설정입니다. 지금까지 {workspace.turn_count}턴을
          진행했습니다.
        </>
      }
    >
      <FieldGroup>
        <Field>
          <FieldLabel htmlFor="ws-rename">이름</FieldLabel>
          <Input id="ws-rename" value={name} onChange={(e) => setName(e.target.value)} />
          <FieldDescription className="font-mono break-all">{workspace.path}</FieldDescription>
        </Field>
        <Field orientation="horizontal">
          <Button
            size="sm"
            disabled={!name.trim() || name === workspace.name}
            onClick={async () => {
              await updateWorkspace(workspace.id, { name });
              notify.success("이름 저장됨");
              await load();
            }}
          >
            <SaveIcon data-icon="inline-start" />
            이름 저장
          </Button>
        </Field>
      </FieldGroup>

      <WorkspaceSettings
        workspace={workspace}
        onSaved={() => {
          notify.success("워크스페이스 설정 저장됨", "다음 턴부터 적용됩니다.");
          void load();
        }}
        onForget={async () => {
          await forgetWorkspace(workspace.id);
          // The folder stays; only the registration is gone, so going back to
          // the chat is the right landing place.
          window.location.href = "/";
        }}
      />
    </PageShell>
  );
}
