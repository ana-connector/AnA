"use client";

import { useCallback, useEffect, useState, type FormEvent } from "react";
import { createWorkspace, fetchWorkspaces, type WorkspaceSummary } from "@/lib/api";
import { Logo } from "@/components/logo";
import { PageShell } from "@/components/page-shell";
import { Button } from "@/components/ui/button";
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Item, ItemContent, ItemGroup, ItemTitle } from "@/components/ui/item";
import { Skeleton } from "@/components/ui/skeleton";

/** Where the mark in the sidebar goes.
 *
 *  The console opens straight into whichever conversation you were last in,
 *  which is right for coming back to work and wrong for every other reason you
 *  might click the product's own name. So this page answers the two things
 *  that click can mean: carry on with something, or start something new.
 *
 *  No cards: two short lists on the page's own background, because a panel
 *  around each one only draws a box that says nothing. */
export default function StartPage() {
  const [workspaces, setWorkspaces] = useState<WorkspaceSummary[] | null>(null);
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
  }, [refresh]);

  const open = (id: string) => {
    window.localStorage.setItem("ana_thread_id", id);
    window.location.href = "/";
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!name.trim()) return;
    try {
      // Straight into the new conversation: creating a workspace is something
      // you do because you are about to work in it.
      open((await createWorkspace(name, path)).id);
    } catch (e) {
      setError(String(e));
    }
  };

  // Most recently worked in first: this list is for picking up where you left
  // off, unlike the sidebar's, which is sorted by name for finding things.
  const recent = [...(workspaces ?? [])]
    .sort((a, b) =>
      (b.last_turn_at ?? b.last_used_at).localeCompare(a.last_turn_at ?? a.last_used_at)
    )
    .slice(0, 5);

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
              {recent.map((item) => (
                <Item
                  key={item.id}
                  size="sm"
                  // sm is 10px of vertical padding, which is a row and a half
                  // of air around one line of text.
                  className="cursor-pointer px-2 py-1 hover:bg-muted"
                  onClick={() => open(item.id)}
                >
                  <ItemContent className="min-w-0">
                    <ItemTitle className="w-full min-w-0">
                      <span className="truncate">{item.name}</span>
                      <span
                        className="truncate font-mono text-[11px] font-normal text-muted-foreground"
                        title={item.path}
                      >
                        {item.path}
                      </span>
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
