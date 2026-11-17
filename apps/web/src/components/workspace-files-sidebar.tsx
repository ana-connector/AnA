"use client";

import { useCallback, useEffect, useState } from "react";
import { ChevronDownIcon, ChevronRightIcon, FolderIcon, GitBranchIcon, PanelRightCloseIcon, PanelRightOpenIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import { useTabs } from "@/components/tabs";
import { FileTypeIcon } from "@/components/file-type-icon";
import { fetchWorkspaceFiles, type WorkspaceFileEntry, type WorkspaceFileListing } from "@/lib/api";
import { fileHref, workspaceFromHref } from "@/lib/workspace-routes";
import { cn } from "@/lib/utils";

function gitLabel(status: string | null) {
  if (!status) return null;
  if (status === "!!") return null;
  if (status.includes("U") || status === "AA" || status === "DD") return { text: "U", title: "충돌", tone: "text-destructive" };
  if (status === "??" || status.includes("A")) return { text: "A", title: "새 파일", tone: "text-emerald-700 dark:text-emerald-400" };
  if (status.includes("D")) return { text: "D", title: "삭제", tone: "text-destructive" };
  if (status.includes("R")) return { text: "R", title: "이름 변경", tone: "text-sky-700 dark:text-sky-400" };
  if (status.includes("C")) return { text: "C", title: "복사", tone: "text-sky-700 dark:text-sky-400" };
  return { text: "M", title: status === "*" ? "변경 있음" : "수정", tone: "text-amber-700 dark:text-amber-400" };
}

function GitBadge({ entry }: { entry: WorkspaceFileEntry }) {
  const status = gitLabel(entry.git_status);
  if (!status) return null;
  return <span title={`Git 상태: ${status.title}`} className={cn("shrink-0 font-mono text-[11px] font-semibold", status.tone)}>{status.text}</span>;
}

function fileTitle(entry: WorkspaceFileEntry) {
  return entry.git_status === "!!"
    ? `${entry.path}\nGit에서 제외됨: ${entry.git_ignore_rule ?? "상위 폴더 규칙"}`
    : entry.path;
}

function GitOverview({ listing }: { listing: WorkspaceFileListing | null }) {
  if (!listing) return null;
  if (!listing.git) return <div className="border-b px-3 py-2 text-[11px] text-muted-foreground">Git 저장소 아님</div>;
  const { staged, modified, untracked, conflicts } = listing.summary;
  const parts = [staged && `스테이징 ${staged}`, modified && `수정 ${modified}`, untracked && `새 파일 ${untracked}`, conflicts && `충돌 ${conflicts}`].filter(Boolean);
  return <div className="flex flex-col gap-1 border-b px-3 py-2 text-[11px]">
    <div className="flex items-center gap-1.5 font-mono text-foreground"><GitBranchIcon className="size-3" /><span className="truncate">{listing.branch ?? "분리된 HEAD"}</span></div>
    <div className={parts.length ? "text-amber-700 dark:text-amber-400" : "text-muted-foreground"}>{parts.length ? parts.join(" · ") : "변경 사항 없음"}</div>
  </div>;
}

function Folder({ workspaceId, path, depth, revision, onOpenFile, onRootLoaded, entry }: {
  workspaceId: string;
  path: string;
  depth: number;
  revision: number;
  onOpenFile?: () => void;
  onRootLoaded?: (workspaceId: string, listing: WorkspaceFileListing) => void;
  entry?: WorkspaceFileEntry;
}) {
  const [entries, setEntries] = useState<WorkspaceFileEntry[]>([]);
  const [open, setOpen] = useState(depth === 0);
  const [error, setError] = useState(false);
  const { open: openTab, activeHref } = useTabs();
  useEffect(() => {
    if (!open) return;
    let alive = true;
    fetchWorkspaceFiles(workspaceId, path).then((listing) => {
      if (!alive) return;
      setEntries(listing.entries);
      setError(false);
      if (!path) onRootLoaded?.(workspaceId, listing);
    }).catch(() => { if (alive) setError(true); });
    return () => { alive = false; };
  }, [workspaceId, path, open, revision, onRootLoaded]);

  const ignored = entry?.git_status === "!!";
  const status = gitLabel(entry?.git_status ?? null);
  return <div>
    {depth > 0 && <button type="button" onClick={() => setOpen(!open)} aria-expanded={open} title={entry && fileTitle(entry)}
      className={cn("flex h-8 w-full items-center gap-1.5 rounded px-2 text-left text-xs hover:bg-accent", ignored && "text-muted-foreground/60")}
      style={{ paddingLeft: `${depth * 18 + 10}px` }}>
      {open ? <ChevronDownIcon className="size-3 shrink-0" /> : <ChevronRightIcon className="size-3 shrink-0" />}
      <FolderIcon className="size-3.5 shrink-0" />
      <span className={cn("min-w-0 flex-1 truncate", status?.tone)}>{entry?.name ?? path.split("/").pop()}</span>
      {entry && <GitBadge entry={entry} />}
    </button>}
    {open && <>
      {error && <p className="px-3 py-2 text-xs text-destructive">폴더를 읽지 못했습니다.</p>}
      {entries.map((child) => {
        if (child.directory) return <Folder key={child.path} workspaceId={workspaceId} path={child.path} depth={depth + 1}
          revision={revision} onOpenFile={onOpenFile} entry={child} />;
        const status = gitLabel(child.git_status);
        return <button key={child.path} type="button"
          onClick={() => { openTab(fileHref(workspaceId, child.path), { title: child.name, detail: child.path }); onOpenFile?.(); }}
          title={fileTitle(child)}
          className={cn("flex h-8 w-full items-center gap-1.5 rounded px-2 text-left text-xs hover:bg-accent",
            activeHref === fileHref(workspaceId, child.path) && "bg-accent",
            child.git_status === "!!" && "text-muted-foreground/60")}
          style={{ paddingLeft: `${(depth + 1) * 18 + 10}px` }}>
          <FileTypeIcon name={child.name} className={child.git_status === "!!" ? "grayscale opacity-50" : undefined} />
          <span className={cn("min-w-0 flex-1 truncate", status?.tone)}>{child.name}</span>
          <GitBadge entry={child} />
        </button>;
      })}
    </>}
  </div>;
}

export function WorkspaceFilesSidebar() {
  const { activeHref } = useTabs();
  const workspaceId = workspaceFromHref(activeHref);
  const [collapsed, setCollapsed] = useState(false);
  const [mobileOpen, setMobileOpen] = useState(false);
  const [revision, setRevision] = useState(0);
  const [gitMeta, setGitMeta] = useState<{ workspaceId: string; listing: WorkspaceFileListing } | null>(null);
  const refresh = useCallback(() => setRevision((value) => value + 1), []);
  const onRootLoaded = useCallback((id: string, listing: WorkspaceFileListing) => setGitMeta({ workspaceId: id, listing }), []);
  useEffect(() => {
    window.addEventListener("ana:files-changed", refresh);
    return () => window.removeEventListener("ana:files-changed", refresh);
  }, [refresh]);
  useEffect(() => {
    if (!workspaceId) return;
    const tick = () => { if (document.visibilityState === "visible") refresh(); };
    const timer = window.setInterval(tick, 2500);
    window.addEventListener("focus", tick);
    document.addEventListener("visibilitychange", tick);
    return () => {
      clearInterval(timer);
      window.removeEventListener("focus", tick);
      document.removeEventListener("visibilitychange", tick);
    };
  }, [workspaceId, refresh]);
  if (!workspaceId) return null;
  const listing = gitMeta?.workspaceId === workspaceId ? gitMeta.listing : null;
  return <>
    {collapsed ? <Button variant="ghost" size="icon" className="m-1 hidden shrink-0 md:inline-flex" title="파일 사이드바 열기" onClick={() => setCollapsed(false)}><PanelRightOpenIcon /></Button> :
      <aside aria-label="프로젝트 파일" className="hidden w-64 shrink-0 flex-col border-l bg-sidebar md:flex">
        <div className="flex h-9 items-center gap-2 border-b px-2 text-xs font-medium">
          <span className="min-w-0 flex-1 truncate">프로젝트 파일</span>
          <Button variant="ghost" size="icon-xs" title="파일 사이드바 닫기" onClick={() => setCollapsed(true)}><PanelRightCloseIcon /></Button>
        </div>
        <GitOverview listing={listing} />
        <div className="min-h-0 flex-1 overflow-auto py-2" key={workspaceId}>
          <Folder workspaceId={workspaceId} path="" depth={0} revision={revision} onRootLoaded={onRootLoaded} />
        </div>
      </aside>}
    <Button variant="secondary" size="icon" className="fixed right-4 bottom-14 z-30 rounded-full shadow-md md:hidden" title="프로젝트 파일" onClick={() => setMobileOpen(true)}><FolderIcon /></Button>
    <Sheet open={mobileOpen} onOpenChange={setMobileOpen}>
      <SheetContent className="w-72 gap-0 p-0">
        <SheetTitle className="border-b px-4 py-3">프로젝트 파일</SheetTitle>
        <GitOverview listing={listing} />
        <div className="min-h-0 flex-1 overflow-auto py-2" key={workspaceId}>
          <Folder workspaceId={workspaceId} path="" depth={0} revision={revision} onOpenFile={() => setMobileOpen(false)} onRootLoaded={onRootLoaded} />
        </div>
      </SheetContent>
    </Sheet>
  </>;
}
