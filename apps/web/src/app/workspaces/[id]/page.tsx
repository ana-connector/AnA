"use client";

import { useCallback, useEffect, useState, use } from "react";
import { ArrowLeftIcon } from "lucide-react";
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

/** One workspace's own page: what it is called, where it works, and whether it
 *  runs in its own container. Reached by hovering the workspace in the sidebar,
 *  which keeps the list itself to one line per project. */
export default function WorkspacePage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const [workspace, setWorkspace] = useState<WorkspaceSummary | null | undefined>(undefined);
  const [name, setName] = useState("");
  const [saved, setSaved] = useState(false);

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
      <main className="mx-auto flex min-h-screen w-full max-w-2xl flex-col gap-3 p-6">
        <Skeleton className="h-8 w-40" />
        <Skeleton className="h-32 w-full" />
      </main>
    );
  }

  if (workspace === null) {
    return (
      <main className="mx-auto flex min-h-screen w-full max-w-2xl flex-col gap-3 p-6">
        <Alert variant="destructive">
          <AlertTitle>없는 워크스페이스</AlertTitle>
          <AlertDescription>{id}는 등록돼 있지 않음.</AlertDescription>
        </Alert>
        <Button size="sm" variant="ghost" render={<a href="/" />}>
          채팅으로
        </Button>
      </main>
    );
  }

  return (
    <main className="mx-auto flex min-h-screen w-full max-w-2xl flex-col gap-4 p-6">
      <header className="flex flex-wrap items-center gap-2">
        <Button size="xs" variant="ghost" render={<a href="/" />}>
          <ArrowLeftIcon data-icon="inline-start" />
          채팅
        </Button>
        <span className="font-heading text-sm font-semibold">{workspace.name}</span>
        <Badge variant="outline">{workspace.turn_count}턴</Badge>
        {saved && <Badge variant="secondary">저장됨</Badge>}
      </header>

      <FieldGroup>
        <Field>
          <FieldLabel htmlFor="ws-rename">이름</FieldLabel>
          <Input id="ws-rename" value={name} onChange={(e) => setName(e.target.value)} />
          <FieldDescription className="font-mono">{workspace.path}</FieldDescription>
        </Field>
        <Field orientation="horizontal">
          <Button
            size="sm"
            onClick={async () => {
              await updateWorkspace(workspace.id, { name });
              setSaved(true);
              await load();
            }}
          >
            이름 저장
          </Button>
        </Field>
      </FieldGroup>

      <WorkspaceSettings
        workspace={workspace}
        onSaved={() => void load()}
        onForget={async () => {
          await forgetWorkspace(workspace.id);
          // The folder stays; only the registration is gone, so going back to
          // the chat is the right landing place.
          window.location.href = "/";
        }}
      />
    </main>
  );
}
