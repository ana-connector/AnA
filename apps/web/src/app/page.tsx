"use client";

import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import Link from "next/link";
import { createWorkspace, fetchWorkspaces, type WorkspaceSummary } from "@/lib/api";
import { Logo } from "@/components/logo";
import { PageShell } from "@/components/page-shell";
import { useTabs } from "@/components/tabs";
import { Button } from "@/components/ui/button";
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Item, ItemContent, ItemGroup, ItemTitle } from "@/components/ui/item";
import { Skeleton } from "@/components/ui/skeleton";
import { shortPath } from "@/lib/format";
import { ShortcutKey } from "@/components/shortcut-key";

/** The root page: resume a workspace or create one.
 *
 *  No cards: two short lists on the page's own background, because a panel
 *  around each one only draws a box that says nothing. */
export default function StartPage() {
  // Opening something from this page hands this tab over to it, the way a
  // browser's new-tab page does: the start page is where a tab waits to be
  // given a job, not a page you keep open beside the job. Opening a
  // workspace from the sidebar instead adds a tab and leaves this one alone.
  const { openHere } = useTabs();
  const [workspaces, setWorkspaces] = useState<WorkspaceSummary[] | null>(null);
  const [name, setName] = useState("");
  const [path, setPath] = useState("");
  const [error, setError] = useState<string | null>(null);
  const loadState = useRef({ mounted: false, request: 0 });

  const refresh = useCallback(() => {
    const request = ++loadState.current.request;
    return fetchWorkspaces().then(
      (items) => {
        if (loadState.current.mounted && loadState.current.request === request) setWorkspaces(items);
      },
      () => {
        if (loadState.current.mounted && loadState.current.request === request) setWorkspaces([]);
      },
    );
  }, []);

  useEffect(() => {
    const state = loadState.current;
    state.mounted = true;
    void refresh();
    return () => {
      state.mounted = false;
      state.request++;
    };
  }, [refresh]);

  const open = useCallback((id: string, name?: string) => {
    window.localStorage.setItem("ana_thread_id", id);
    openHere(`/${encodeURIComponent(id)}`, name);
  }, [openHere]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!name.trim()) return;
    try {
      // Straight into the new conversation: creating a workspace is something
      // you do because you are about to work in it.
      const created = await createWorkspace(name, path);
      open(created.id, created.name);
    } catch (e) {
      setError(String(e));
    }
  };

  // Most recently worked in first: this list is for picking up where you left
  // off, unlike the sidebar's, which is sorted by name for finding things.
  // Archived ones are left out — "resume this" is the one thing they are not.
  const recent = useMemo(() => (workspaces ?? [])
    .filter((item) => !item.archived)
    .sort((a, b) =>
      (b.last_turn_at ?? b.last_used_at).localeCompare(a.last_turn_at ?? a.last_used_at)
    )
    .slice(0, 5), [workspaces]);

  useEffect(() => {
    const onRecentShortcut = (event: KeyboardEvent) => {
      if (!event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
      const index = Number(event.code.replace(/^Digit|^Numpad/, "")) - 1;
      if (!Number.isInteger(index) || index < 0 || !recent[index]) return;
      event.preventDefault();
      open(recent[index].id, recent[index].name);
    };
    window.addEventListener("keydown", onRecentShortcut);
    return () => window.removeEventListener("keydown", onRecentShortcut);
  }, [open, recent]);

  return (
    <PageShell title="시작" center>
      {/* Its own side room on top of the shell's: this page is two short
          columns, and letting them run to the shell's full width left the
          text further out than anything else on it. */}
      <div className="flex flex-col gap-8 px-2 sm:px-8 lg:px-16">
      {/* The same arrangement as the sidebar header, at the size a page can
          afford: mark, name, tagline, left-aligned. */}
      <div className="flex items-center gap-3">
        <div className="flex aspect-square size-12 shrink-0 items-center justify-center rounded-lg bg-sidebar-primary text-sidebar-primary-foreground">
          <Logo className="size-7" />
        </div>
        <div className="flex flex-col leading-tight">
          <span className="font-heading text-xl font-semibold">AnA</span>
          <span className="text-sm text-muted-foreground">AnA is Not Agent</span>
        </div>
      </div>

      <div className="grid gap-8 md:grid-cols-2">
        <section className="flex min-w-0 flex-col gap-2">
          <h2 className="text-xs font-medium text-muted-foreground">최근 워크스페이스</h2>
          {workspaces === null ? (
            <div className="flex flex-col gap-2">
              <Skeleton className="h-8 w-full" />
              <Skeleton className="h-8 w-full" />
            </div>
          ) : recent.length === 0 ? (
            <p className="text-sm text-muted-foreground">아직 없다. 옆에서 하나 만들자.</p>
          ) : (
            <ItemGroup className="gap-0.5">
              {recent.map((item, index) => (
                <Item
                  key={item.id}
                  size="sm"
                  // sm is 10px of vertical padding, which is a row and a half
                  // of air around one line of text.
                  className="cursor-pointer px-2 py-1 hover:bg-muted"
                  // Still a link, so the browser's own middle-click and
                  // open-in-new-window keep working; a plain click is handled
                  // here so the tab is replaced rather than added.
                  render={
                    <Link
                      href={`/${encodeURIComponent(item.id)}`}
                      aria-keyshortcuts={`Alt+${index + 1}`}
                      onClick={(event) => {
                        if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return;
                        event.preventDefault();
                        open(item.id, item.name);
                      }}
                    />
                  }
                >
                  <ItemContent className="min-w-0">
                    <ItemTitle className="w-full min-w-0 flex-nowrap">
                      <span className="min-w-0 flex-1 truncate">{item.name}</span>
                      <span
                        className="max-w-[20%] min-w-0 shrink truncate font-mono text-[11px] font-normal text-muted-foreground"
                        title={item.path}
                      >
                        {shortPath(item.path)}
                      </span>
                      <ShortcutKey>⌥{index + 1}</ShortcutKey>
                    </ItemTitle>
                  </ItemContent>
                </Item>
              ))}
            </ItemGroup>
          )}
        </section>

        <section className="flex min-w-0 flex-col gap-2">
          <h2 className="text-xs font-medium text-muted-foreground">새 워크스페이스</h2>
          <form onSubmit={submit}>
            <FieldGroup>
              <Field>
                <FieldLabel htmlFor="start-name">이름</FieldLabel>
                <Input
                  id="start-name"
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                  placeholder="내 프로젝트"
                />
              </Field>
              <Field>
                <FieldLabel htmlFor="start-path">폴더 (선택)</FieldLabel>
                <Input
                  id="start-path"
                  value={path}
                  onChange={(event) => setPath(event.target.value)}
                  placeholder="/home/hello/projects/foo"
                />
                <FieldDescription>
                  비우면 워크스페이스 루트 아래에 새로 만든다. 기존 폴더를 적으면 그대로 쓴다.
                </FieldDescription>
              </Field>
              {error && <FieldDescription className="text-destructive">{error}</FieldDescription>}
              <Field orientation="horizontal">
                <Button type="submit" size="sm">
                  만들고 열기
                </Button>
              </Field>
            </FieldGroup>
          </form>
        </section>
      </div>
      </div>
    </PageShell>
  );
}
