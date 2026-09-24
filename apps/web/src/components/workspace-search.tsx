"use client";

import { useCallback, useEffect, useState } from "react";
import { FolderIcon, SearchIcon } from "lucide-react";
import { fetchWorkspaces, type WorkspaceSummary } from "@/lib/api";
import { Button } from "@/components/ui/button";
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
 *  know the name. ⌘K / Ctrl+K anywhere in the console opens it, typing filters,
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
  const [workspaces, setWorkspaces] = useState<WorkspaceSummary[]>([]);

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
      if (event.key.toLowerCase() !== "k" || !(event.metaKey || event.ctrlKey)) return;
      // Before anything else claims it: the browser's own find-in-page is not
      // what ⌘K means in an app with a palette.
      event.preventDefault();
      setOpen((previous) => !previous);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  return (
    <>
      {/* Shaped like the input it opens, because that is what people look for.
          It is a button: a real input here would take focus and swallow the
          first keystrokes before the dialog exists. */}
      <Button
        variant="outline"
        size="sm"
        onClick={() => setOpen(true)}
        className="h-8 w-44 justify-start gap-2 px-2 font-normal text-muted-foreground lg:w-56"
      >
        <SearchIcon data-icon="inline-start" />
        <span className="truncate">워크스페이스 검색</span>
        <kbd className="ml-auto hidden rounded border border-border px-1 font-mono text-[10px] lg:inline">
          ⌘K
        </kbd>
      </Button>

      <CommandDialog
        open={open}
        onOpenChange={setOpen}
        title="워크스페이스 검색"
        description="이름이나 경로로 찾아 대화를 엽니다"
      >
        <Command>
          <CommandInput placeholder="이름 또는 경로" />
          <CommandList>
            <CommandEmpty>맞는 워크스페이스 없음</CommandEmpty>
            <CommandGroup>
              {workspaces.map((item) => (
                <CommandItem
                  key={item.id}
                  // The path is searchable too: two projects often share a name
                  // and differ only in where they live.
                  value={`${item.name} ${item.path}`}
                  // onSelect, not onClick: cmdk drives both the click and the
                  // Enter key through this one, and an onClick handler is
                  // never reached by the keyboard.
                  onSelect={() => {
                    setOpen(false);
                    onOpenWorkspace(item.id);
                  }}
                >
                  <FolderIcon />
                  <span className="truncate">{item.name}</span>
                  <span className="truncate font-mono text-[11px] text-muted-foreground">
                    {item.path}
                  </span>
                </CommandItem>
              ))}
            </CommandGroup>
          </CommandList>
        </Command>
      </CommandDialog>
    </>
  );
}
