"use client";

import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  BoxIcon,
  FolderIcon,
  KeyRoundIcon,
  PlusIcon,
  RefreshCwIcon,
  RepeatIcon,
  SearchIcon,
  SettingsIcon,
  SlidersHorizontalIcon,
  TerminalIcon,
} from "lucide-react";
import {
  createWorkspace,
  fetchWorkspaceCapabilities,
  fetchWorkspaces,
  type WorkspaceSummary,
} from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from "@/components/ui/empty";
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { InputGroup, InputGroupAddon, InputGroupInput } from "@/components/ui/input-group";
import { Skeleton } from "@/components/ui/skeleton";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { Logo } from "@/components/logo";
import { WorkspaceEditor } from "@/components/workspace-editor";
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

/** Filtering only appears once the list is long enough to need it: a search box
 *  above three rows is furniture. */
const FILTER_THRESHOLD = 7;

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
  // The id of the workspace whose folder is ANA's own source tree, when the
  // repository is mounted. Work there changes ANA, so that row says so.
  const [selfWorkspaceId, setSelfWorkspaceId] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState<WorkspaceSummary | null>(null);
  const [filter, setFilter] = useState("");
  const [name, setName] = useState("");
  const [path, setPath] = useState("");
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async (): Promise<WorkspaceSummary[]> => {
    try {
      const items = await fetchWorkspaces();
      setWorkspaces(items);
      return items;
    } catch {
      setWorkspaces([]);
      return [];
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh, reloadKey]);

  useEffect(() => {
    void fetchWorkspaceCapabilities().then((caps) => setSelfWorkspaceId(caps.self_workspace));
  }, []);

  const visible = useMemo(() => {
    const items = workspaces ?? [];
    const needle = filter.trim().toLowerCase();
    if (!needle) return items;
    return items.filter(
      (item) =>
        item.name.toLowerCase().includes(needle) ||
        item.id.toLowerCase().includes(needle) ||
        item.path.toLowerCase().includes(needle)
    );
  }, [workspaces, filter]);

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

  /** Where to look once a workspace is gone. Deleting the open one used to hand
   *  the chat a null id, which makes the server mint a fresh conversation — so
   *  removing a workspace silently created a new, uuid-named one. Another
   *  existing workspace is the honest destination; a blank chat only when there
   *  is none left. */
  const afterDelete = async (deletedId: string) => {
    setEditing(null);
    const remaining = await refresh();
    if (deletedId === workspaceId) onOpenWorkspace(remaining[0]?.id ?? null);
  };

  return (
    <>
    <Sidebar>
      <SidebarHeader>
        <SidebarMenu>
          <SidebarMenuItem>
            {/* The shadcn header pattern: one large menu row carrying the
                mark and the name. The mark takes the square's foreground
                colour, so it reads white on the sidebar's primary. */}
            <SidebarMenuButton size="lg" render={<Link href="/" />}>
              <div className="flex aspect-square size-8 items-center justify-center rounded-lg bg-sidebar-primary text-sidebar-primary-foreground">
                <Logo className="size-4" />
              </div>
              <div className="grid flex-1 text-left leading-tight">
                <span className="truncate font-heading text-sm font-semibold">AnA</span>
                <span className="truncate text-xs text-muted-foreground">AnA is Not Agent</span>
              </div>
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarHeader>

      <SidebarContent>
        <SidebarGroup>
          <SidebarGroupLabel>지금 작업 중</SidebarGroupLabel>
          <SidebarGroupContent className="px-2">
            <div className="flex items-start gap-2 rounded-md border border-border bg-sidebar-accent/40 px-2 py-1.5">
              <FolderIcon className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
              <div className="min-w-0 flex-1">
                <div className="flex min-w-0 items-center gap-1">
                  <span className="truncate text-xs font-medium">
                    {workspaceName ?? workspaceId ?? "연결 중…"}
                  </span>
                  {workspaceId && workspaceId === selfWorkspaceId && (
                    <RepeatIcon className="size-3 shrink-0 text-muted-foreground" />
                  )}
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
            {(workspaces?.length ?? 0) >= FILTER_THRESHOLD && (
              <div className="px-2 pb-1.5">
                <InputGroup>
                  <InputGroupAddon>
                    <SearchIcon />
                  </InputGroupAddon>
                  <InputGroupInput
                    value={filter}
                    onChange={(event) => setFilter(event.target.value)}
                    placeholder="이름 또는 경로"
                    aria-label="워크스페이스 검색"
                  />
                </InputGroup>
              </div>
            )}

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
            ) : visible.length === 0 ? (
              <p className="px-2 py-3 text-xs text-muted-foreground">
                「{filter}」와 맞는 워크스페이스 없음
              </p>
            ) : (
              <SidebarMenu>
                {visible.map((item) => (
                  <SidebarMenuItem key={item.id}>
                    <SidebarMenuButton
                      isActive={item.id === workspaceId}
                      onClick={() => onOpenWorkspace(item.id)}
                      className="h-auto py-1.5"
                      title={`${item.id}\n${item.path}`}
                    >
                      <div className="flex min-w-0 flex-col gap-0.5">
                        <span className="flex min-w-0 items-center gap-1">
                          <span className="truncate text-xs">{item.name}</span>
                          {item.id === selfWorkspaceId && (
                            // ANA's own repository: the same list, the same
                            // gate, but work here changes ANA itself.
                            <Tooltip>
                              <TooltipTrigger
                                render={<RepeatIcon className="size-3 shrink-0 text-muted-foreground" />}
                              />
                              <TooltipContent>AnA 자기 자신의 소스 트리</TooltipContent>
                            </Tooltip>
                          )}
                          {item.container_enabled && (
                            <Tooltip>
                              <TooltipTrigger
                                render={<BoxIcon className="size-3 shrink-0 text-muted-foreground" />}
                              />
                              <TooltipContent>전용 컨테이너에서 실행</TooltipContent>
                            </Tooltip>
                          )}
                        </span>
                        <span className="truncate text-[11px] text-muted-foreground">
                          {shortPath(item.path)}
                          {item.turn_count > 0 ? ` · ${item.turn_count}턴` : " · 새 대화"}
                          {item.last_agent ? ` · ${item.last_agent}` : ""}
                        </span>
                      </div>
                    </SidebarMenuButton>
                    <SidebarMenuAction
                      showOnHover
                      title="워크스페이스 설정"
                      onClick={() => setEditing(item)}
                    >
                      <SettingsIcon />
                    </SidebarMenuAction>
                  </SidebarMenuItem>
                ))}
              </SidebarMenu>
            )}

            <SidebarMenu>
              <SidebarMenuItem>
                <SidebarMenuButton onClick={() => setAdding(true)}>
                  <PlusIcon />
                  워크스페이스 추가
                </SidebarMenuButton>
              </SidebarMenuItem>
            </SidebarMenu>
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

    <Dialog open={editing !== null} onOpenChange={(open) => { if (!open) setEditing(null); }}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>워크스페이스 설정</DialogTitle>
        </DialogHeader>
        {editing && (
          <>
            <WorkspaceEditor
              workspace={editing}
              isSelf={editing.id === selfWorkspaceId}
              onSaved={async () => {
                // The edited row comes back from the list rather than being
                // patched locally, so the dialog shows what the server stored.
                const items = await refresh();
                setEditing((previous) =>
                  previous ? items.find((item) => item.id === previous.id) ?? null : null
                );
              }}
              onDeleted={() => afterDelete(editing.id)}
            />
            <a
              href={`/workspaces/${encodeURIComponent(editing.id)}`}
              className="text-xs text-muted-foreground underline underline-offset-2 hover:text-foreground"
            >
              전체 페이지에서 열기
            </a>
          </>
        )}
      </DialogContent>
    </Dialog>
    </>
  );
}
