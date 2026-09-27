"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { FolderIcon, SearchIcon } from "lucide-react";
import { fetchWorkspaces, type WorkspaceSummary } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { shortPath } from "@/lib/format";
import { ShortcutKey } from "@/components/shortcut-key";
import {
  Command,
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";

/** Switching workspaces without reaching for the sidebar.
 *
 *  The sidebar list is for browsing; this is for the case where you already
 *  know the name. ⌘K / Ctrl+K opens it, typing filters,
 *  Enter switches — the same motion every editor has trained people in. */
export function WorkspaceSearch({
  onOpenWorkspace,
  reloadKey,
}: {
  onOpenWorkspace: (id: string) => void;
  /** Bumped when a turn finishes, so a newly named workspace is findable. */
  reloadKey: number;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [workspaces, setWorkspaces] = useState<WorkspaceSummary[]>([]);
  const matches = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase();
    return workspaces.filter((item) =>
      !needle || `${item.name} ${item.path}`.toLocaleLowerCase().includes(needle)
    );
  }, [query, workspaces]);

  const select = useCallback((id: string) => {
    setOpen(false);
    setQuery("");
    onOpenWorkspace(id);
  }, [onOpenWorkspace]);

  const refresh = useCallback(async () => {
    try {
      setWorkspaces(await fetchWorkspaces());
    } catch {
      // The sidebar already reports a list that cannot be loaded.
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh, reloadKey]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.code !== "KeyK" || !(event.metaKey || event.ctrlKey) || event.altKey) return;
      // Before anything else claims it: the browser's own find-in-page is not
      // what ⌘K means in an app with a palette.
      event.preventDefault();
      setOpen((previous) => !previous);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  useEffect(() => {
    if (!open) return;
    const onNumber = (event: KeyboardEvent) => {
      if (!event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
      const index = Number(event.code.replace(/^Digit|^Numpad/, "")) - 1;
      if (!Number.isInteger(index) || index < 0 || index > 8 || !matches[index]) return;
      event.preventDefault();
      select(matches[index].id);
    };
    window.addEventListener("keydown", onNumber);
    return () => window.removeEventListener("keydown", onNumber);
  }, [open, matches, select]);

  return (
    <>
      {/* Shaped like the input it opens, because that is what people look for.
          It is a button: a real input here would take focus and swallow the
          first keystrokes before the dialog exists. */}
      <Button
        variant="outline"
        size="sm"
        onClick={() => setOpen(true)}
        aria-keyshortcuts="Meta+K Control+K"
        className="h-8 w-44 justify-start gap-2 px-2 font-normal text-muted-foreground lg:w-56"
      >
        <SearchIcon data-icon="inline-start" />
        <span className="truncate">워크스페이스 검색</span>
        <ShortcutKey className="ml-auto hidden lg:inline-flex">⌘K</ShortcutKey>
      </Button>

      <CommandDialog
        open={open}
        onOpenChange={(next) => { setOpen(next); if (!next) setQuery(""); }}
        title="워크스페이스 검색"
        description="이름이나 경로로 찾고 Option+숫자로 결과를 엽니다"
      >
        <Command shouldFilter={false}>
          <CommandInput placeholder="이름 또는 경로 · ⌥1~9 선택" value={query} onValueChange={setQuery} />
          <CommandList>
            <CommandEmpty>맞는 워크스페이스 없음</CommandEmpty>
            <CommandGroup>
              {matches.map((item, index) => (
                <CommandItem
                  key={item.id}
                  // The path is searchable too: two projects often share a name
                  // and differ only in where they live.
                  value={`${item.name} ${item.path}`}
                  aria-keyshortcuts={index < 9 ? `Alt+${index + 1}` : undefined}
                  // onSelect, not onClick: cmdk drives both the click and the
                  // Enter key through this one, and an onClick handler is
                  // never reached by the keyboard.
                  onSelect={() => select(item.id)}
                >
                  <FolderIcon />
                  <span className="min-w-0 flex-1 truncate">{item.name}</span>
                  <span className="max-w-[20%] min-w-0 shrink truncate font-mono text-[11px] text-muted-foreground" title={item.path}>
                    {shortPath(item.path)}
                  </span>
                  {index < 9 && <ShortcutKey className="ml-auto">⌥{index + 1}</ShortcutKey>}
                </CommandItem>
              ))}
            </CommandGroup>
          </CommandList>
        </Command>
      </CommandDialog>
    </>
  );
}
