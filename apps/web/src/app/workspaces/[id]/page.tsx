"use client";

import { useCallback, useEffect, useRef, useState, use } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  fetchWorkspaceCapabilities,
  fetchWorkspaces,
  type WorkspaceSummary,
} from "@/lib/api";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { WorkspaceEditor } from "@/components/workspace-editor";
import { PageShell } from "@/components/page-shell";
import { notify } from "@/lib/notify";

/** One workspace's own page: what it is called, where it works, and whether it
 *  runs in its own container. Reached by hovering the workspace in the sidebar,
 *  which keeps the list itself to one line per project. */
export default function WorkspacePage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const router = useRouter();
  const [workspace, setWorkspace] = useState<WorkspaceSummary | null | undefined>(undefined);
  const [selfWorkspaceId, setSelfWorkspaceId] = useState<string | null>(null);
  const loadState = useRef({ mounted: false, request: 0 });

  const load = useCallback(() => {
    const request = ++loadState.current.request;
    return fetchWorkspaces().then((all) => {
      if (loadState.current.mounted && loadState.current.request === request) {
        setWorkspace(all.find((w) => w.id === id) ?? null);
      }
    });
  }, [id]);

  useEffect(() => {
    const state = loadState.current;
    state.mounted = true;
    void load();
    return () => {
      state.mounted = false;
      state.request++;
    };
  }, [load]);

  useEffect(() => {
    let active = true;
    void fetchWorkspaceCapabilities().then((caps) => {
      if (active) setSelfWorkspaceId(caps.self_workspace);
    });
    return () => { active = false; };
  }, []);

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
            <span className="font-mono">{id}</span> 는 등록돼 있지 않습니다. 콘솔로 돌아가
            다른 워크스페이스를 고르거나 새로 만드세요.
          </AlertDescription>
        </Alert>
        <Button size="sm" variant="outline" render={<Link href="/" />} className="w-fit">
          시작 페이지로 돌아가기
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
      <WorkspaceEditor
        key={workspace.id}
        workspace={workspace}
        isSelf={workspace.id === selfWorkspaceId}
        onSaved={async () => {
          notify.success("워크스페이스 저장됨", "변경 사항은 다음 턴부터 적용됩니다.");
          await load();
        }}
        onDeleted={() => {
          // The stored conversation is gone, so a chat that reopened this id
          // would make the server mint a brand-new workspace under it.
          if (window.localStorage.getItem("ana_thread_id") === workspace.id) {
            window.localStorage.removeItem("ana_thread_id");
          }
          router.push("/");
        }}
      />
    </PageShell>
  );
}
