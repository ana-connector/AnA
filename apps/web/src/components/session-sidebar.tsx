"use client";

import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useTheme } from "next-themes";
import {
  KeyRoundIcon,
  MoonIcon,
  PlusIcon,
  SearchIcon,
  SettingsIcon,
  SlidersHorizontalIcon,
  SunIcon,
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

/** "1.1M", "19.7K", "840" — a sidebar row has space for a magnitude, not for
 *  a nine-digit number, and the magnitude is what the figure is read for. */
function compactTokens(tokens: number): string {
  if (tokens >= 1_000_000) return `${(tokens / 1_000_000).toFixed(1)}M`;
  if (tokens >= 1_000) return `${(tokens / 1_000).toFixed(1)}K`;
  return String(tokens);
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
  onOpenWorkspace,
  reloadKey,
}: {
  workspaceId: string | null;
  onOpenWorkspace: (id: string | null) => void;
  /** Changes when a turn finishes, so activity shows up without a refresh. */
  reloadKey: number;
}) {
  // Which utility page is open, so the footer nav can mark it: a link that
  // never looks selected leaves the user guessing where they are.
  const pathname = usePathname();
  const { resolvedTheme, setTheme } = useTheme();
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
    {/* variant="inset": the sidebar sits on the page's own background and the
        content floats beside it as a rounded panel. SidebarInset styles itself
        off this, so without it the inset element is an ordinary <main>. */}
    <Sidebar variant="inset">
      <SidebarHeader>
        <SidebarMenu>
          <SidebarMenuItem>
            {/* The shadcn header pattern: one large menu row carrying the
                mark and the name. It opens the start page rather than the
                console: "/" resumes whichever conversation you were last in,
                which is not what clicking the product's own name means. */}
            <SidebarMenuButton
              size="lg"
              isActive={pathname === "/start"}
              render={<Link href="/start" />}
            >
              <div className="flex aspect-square size-10 items-center justify-center rounded-lg bg-sidebar-primary text-sidebar-primary-foreground">
                {/* size-6! because the menu button sizes every descendant svg
                    to 16px, and that descendant rule outranks a plain class. */}
                <Logo className="size-6!" />
              </div>
              <div className="grid flex-1 text-left leading-tight">
                <span className="truncate font-heading text-base font-semibold">AnA</span>
                <span className="truncate text-xs text-muted-foreground">AnA is Not Agent</span>
              </div>
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarHeader>

      <SidebarContent>
        <SidebarGroup>
          <SidebarGroupLabel>모든 워크스페이스</SidebarGroupLabel>
          {/* Adding is what people come to this header for; the list
              refetches itself whenever a turn finishes. */}
          <SidebarGroupAction title="워크스페이스 추가" onClick={() => setAdding(true)}>
            <PlusIcon />
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
              <SidebarMenu className="gap-1">
                {visible.map((item) => (
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
                          {shortPath(item.path)}
                          {item.total_tokens > 0 ? ` · ${compactTokens(item.total_tokens)}` : " · 새 대화"}
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
          <SidebarMenuItem>
            {/* Both icons render and CSS picks one, so the row does not change
                between the server and the client render. */}
            <SidebarMenuButton
              onClick={() => setTheme(resolvedTheme === "dark" ? "light" : "dark")}
            >
              <SunIcon className="dark:hidden" />
              <MoonIcon className="hidden dark:block" />
              <span className="dark:hidden">라이트</span>
              <span className="hidden dark:inline">다크</span>
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
