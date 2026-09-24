"use client";

import { useCallback, useEffect, useState, type FormEvent } from "react";
import { usePathname } from "next/navigation";
import {
  FolderIcon,
  KeyRoundIcon,
  PlusIcon,
  RefreshCwIcon,
  SettingsIcon,
  SlidersHorizontalIcon,
  TerminalIcon,
} from "lucide-react";
import { createWorkspace, fetchWorkspaces, forgetWorkspace, updateWorkspace, type WorkspaceSummary } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from "@/components/ui/empty";
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { WorkspaceSettings } from "@/components/workspace-settings";
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupAction,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuAction,
  SidebarMenuButton,
  SidebarMenuItem,
} from "@/components/ui/sidebar";

function shortPath(path: string | null): string {
  if (!path) return "—";
  const parts = path.split("/").filter(Boolean);
  return parts.length ? `…/${parts[parts.length - 1]}` : path;
}

/** Workspaces are what the user manages: one folder, one lead agent, one
 *  ongoing conversation. Selecting one opens its conversation — the workspace
 *  id *is* the thread id, so its CLI sessions, history and directory all line
 *  up behind the same name. */
export function SessionSidebar({
  workspaceId,
  workspace,
  workspaceName,
  onOpenWorkspace,
  reloadKey,
}: {
  workspaceId: string | null;
  workspace: string | null;
  workspaceName: string | null;
  onOpenWorkspace: (id: string | null) => void;
  /** Changes when a turn finishes, so activity shows up without a refresh. */
  reloadKey: number;
}) {
  // Which utility page is open, so the footer nav can mark it: a link that
  // never looks selected leaves the user guessing where they are.
  const pathname = usePathname();
  const [workspaces, setWorkspaces] = useState<WorkspaceSummary[] | null>(null);
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState<WorkspaceSummary | null>(null);
  const [editName, setEditName] = useState("");
  const [name, setName] = useState("");
  const [path, setPath] = useState("");
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setWorkspaces(await fetchWorkspaces());
    } catch {
      setWorkspaces([]);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh, reloadKey]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!name.trim()) return;
    try {
      const created = await createWorkspace(name, path);
      setName("");
      setPath("");
      setAdding(false);
      setError(null);
      await refresh();
      onOpenWorkspace(created.id);
    } catch (e) {
      setError(String(e));
    }
  };

  return (
    <>
    <Sidebar variant="inset">
      <SidebarHeader>
        <div className="flex items-center justify-between gap-2 px-2 py-1">
          <span className="font-heading text-sm font-semibold">ANA</span>
          <Button size="xs" variant="outline" onClick={() => setAdding((open) => !open)}>
            <PlusIcon data-icon="inline-start" />
            워크스페이스
          </Button>
        </div>

        <Dialog open={adding} onOpenChange={setAdding}>
          <DialogContent>
            <DialogHeader><DialogTitle>워크스페이스 추가</DialogTitle></DialogHeader>
          <form onSubmit={submit}>
            <FieldGroup>
              <Field>
                <FieldLabel htmlFor="ws-name">이름</FieldLabel>
                <Input
                  id="ws-name"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="내 프로젝트"
                  autoFocus
                />
              </Field>
              <Field>
                <FieldLabel htmlFor="ws-path">폴더 (선택)</FieldLabel>
                <Input
                  id="ws-path"
                  value={path}
                  onChange={(e) => setPath(e.target.value)}
                  placeholder="/home/hello/projects/foo"
                />
                <FieldDescription>
                  비우면 워크스페이스 루트 아래에 새로 만듭니다. 기존 폴더를 적으면 그대로 사용합니다.
                </FieldDescription>
              </Field>
              {error && <FieldDescription className="text-destructive">{error}</FieldDescription>}
              <Field orientation="horizontal">
                <Button type="submit" size="sm">
                  만들기
                </Button>
                <Button type="button" size="sm" variant="ghost" onClick={() => setAdding(false)}>
                  취소
                </Button>
              </Field>
            </FieldGroup>
          </form>
          </DialogContent>
        </Dialog>
      </SidebarHeader>

      <SidebarContent>
        <SidebarGroup>
          <SidebarGroupLabel>지금 작업 중</SidebarGroupLabel>
          <SidebarGroupContent className="px-2">
            <div className="flex items-start gap-2 rounded-md border border-border px-2 py-1.5">
              <FolderIcon className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
              <div className="min-w-0">
                <div className="truncate text-xs font-medium">
                  {workspaceName ?? workspaceId ?? "연결 중…"}
                </div>
                {/* Both CLIs run here, and the gate escalates anything that
                    reaches outside it. */}
                <div className="truncate font-mono text-[11px] text-muted-foreground" title={workspace ?? undefined}>
                  {shortPath(workspace)}
                </div>
              </div>
            </div>
          </SidebarGroupContent>
        </SidebarGroup>

        <SidebarGroup>
          <SidebarGroupLabel>모든 워크스페이스</SidebarGroupLabel>
          <SidebarGroupAction title="목록 새로고침" onClick={() => void refresh()}>
            <RefreshCwIcon />
          </SidebarGroupAction>
          <SidebarGroupContent>
            {workspaces === null ? (
              <div className="flex flex-col gap-2 px-2">
                <Skeleton className="h-8 w-full" />
                <Skeleton className="h-8 w-full" />
              </div>
            ) : workspaces.length === 0 ? (
              <Empty className="py-6">
                <EmptyHeader>
                  <EmptyTitle>워크스페이스 없음</EmptyTitle>
                  <EmptyDescription>폴더를 하나 등록하면 대장 에이전트가 거기서 일합니다.</EmptyDescription>
                </EmptyHeader>
              </Empty>
            ) : (
              <SidebarMenu>
                {workspaces.map((item) => (
                  <SidebarMenuItem key={item.id}>
                    <SidebarMenuButton
                      isActive={item.id === workspaceId}
                      onClick={() => onOpenWorkspace(item.id)}
                      className="h-auto py-1.5"
                      title={`${item.id}\n${item.path}`}
                    >
                      <div className="flex min-w-0 flex-col gap-0.5">
                        <span className="truncate text-xs">{item.name}</span>
                        <span className="truncate text-[11px] text-muted-foreground">
                          {shortPath(item.path)} · {item.turn_count}턴
                        </span>
                      </div>
                    </SidebarMenuButton>
                    <SidebarMenuAction
                      showOnHover
                      title="워크스페이스 설정"
                      onClick={() => { setEditing(item); setEditName(item.name); }}
                    >
                      <SettingsIcon />
                    </SidebarMenuAction>
                  </SidebarMenuItem>
                ))}
              </SidebarMenu>
            )}
          </SidebarGroupContent>
        </SidebarGroup>
      </SidebarContent>

      <SidebarFooter>
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton isActive={pathname === "/settings"} render={<a href="/settings" />}>
              <SlidersHorizontalIcon />
              설정
            </SidebarMenuButton>
          </SidebarMenuItem>
          <SidebarMenuItem>
            <SidebarMenuButton isActive={pathname === "/tools"} render={<a href="/tools" />}>
              <TerminalIcon />
              도구 로그
            </SidebarMenuButton>
          </SidebarMenuItem>
          <SidebarMenuItem>
            <SidebarMenuButton isActive={pathname === "/secrets"} render={<a href="/secrets" />}>
              <KeyRoundIcon />
              시크릿
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarFooter>
    </Sidebar>
    <Dialog open={editing !== null} onOpenChange={(open) => { if (!open) setEditing(null); }}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-lg">
        <DialogHeader><DialogTitle>워크스페이스 설정</DialogTitle></DialogHeader>
        {editing && (
          <div className="flex flex-col gap-5">
            <FieldGroup>
              <Field>
                <FieldLabel htmlFor="ws-rename">이름</FieldLabel>
                <Input id="ws-rename" value={editName} onChange={(event) => setEditName(event.target.value)} />
                <FieldDescription className="font-mono">{editing.path}</FieldDescription>
              </Field>
              <Field orientation="horizontal">
                <Button size="sm" onClick={async () => {
                  await updateWorkspace(editing.id, { name: editName });
                  await refresh();
                  setEditing((current) => current ? { ...current, name: editName } : null);
                }}>이름 저장</Button>
              </Field>
            </FieldGroup>
            <WorkspaceSettings
              workspace={editing}
              onSaved={() => void refresh()}
              onForget={async () => {
                await forgetWorkspace(editing.id);
                const removedCurrent = editing.id === workspaceId;
                setEditing(null);
                await refresh();
                if (removedCurrent) onOpenWorkspace(null);
              }}
            />
          </div>
        )}
      </DialogContent>
    </Dialog>
    </>
  );
}
