"use client";

import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Markdown } from "@/components/markdown";
import { PageShell } from "@/components/page-shell";
import { ApiError, readWorkspaceFile, saveWorkspaceFile, workspaceFilePreviewUrl } from "@/lib/api";

export default function FilePage({ sessionId, path }: { sessionId: string; path: string }) {
  const [content, setContent] = useState("");
  const [saved, setSaved] = useState("");
  const [revision, setRevision] = useState("");
  const [mode, setMode] = useState<"edit" | "preview">("preview");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const markdown = /\.mdx?$/i.test(path);
  const image = /\.(png|jpe?g|gif|webp|avif|svg)$/i.test(path);
  const name = path.split("/").pop() ?? path;
  const draftKey = `ana:file-draft:${sessionId}:${path}`;

  const reload = useCallback(async (discardDraft = false) => {
    if (image) { setError(""); return; }
    if (discardDraft) try { sessionStorage.removeItem(draftKey); } catch { /* Storage may be off. */ }
    try {
      const result = await readWorkspaceFile(sessionId, path);
      let draft: { content: string; revision: string } | null = null;
      if (!discardDraft) try {
        const raw = sessionStorage.getItem(draftKey);
        if (raw) draft = JSON.parse(raw) as { content: string; revision: string };
      } catch { /* Storage may be off. */ }
      setContent(draft?.content ?? result.content);
      setSaved(result.content);
      setRevision(draft?.revision ?? result.revision);
      setError(draft && draft.revision !== result.revision ? "파일이 다른 곳에서 변경됐습니다. 초안을 복사한 뒤 다시 불러오세요." : "");
    } catch (cause) { setError(cause instanceof ApiError ? cause.detail ?? cause.message : "파일을 열지 못했습니다."); }
  }, [sessionId, path, image, draftKey]);

  useEffect(() => { void Promise.resolve().then(() => reload()); }, [reload]);
  useEffect(() => {
    if (!revision || image) return;
    try {
      if (content === saved) sessionStorage.removeItem(draftKey);
      else sessionStorage.setItem(draftKey, JSON.stringify({ content, revision }));
    } catch { /* Storage may be off. */ }
  }, [content, saved, revision, draftKey, image]);

  const save = async () => {
    setBusy(true);
    try {
      const result = await saveWorkspaceFile(sessionId, path, content, revision);
      setSaved(content);
      setRevision(result.revision);
      setError("");
      try { sessionStorage.removeItem(draftKey); } catch { /* Storage may be off. */ }
      window.dispatchEvent(new Event("ana:files-changed"));
    } catch (cause) {
      setError(cause instanceof ApiError && cause.status === 409
        ? "에이전트나 다른 탭에서 파일을 수정했습니다. 내용을 복사해 둔 뒤 다시 불러오세요."
        : cause instanceof ApiError ? cause.detail ?? cause.message : "저장하지 못했습니다.");
    } finally { setBusy(false); }
  };

  return (
    <PageShell title={name} fill>
      <div className="flex min-h-0 flex-1 flex-col">
        <div className="flex shrink-0 items-center gap-2 border-b px-4 py-2">
          <span className="min-w-0 flex-1 truncate font-mono text-xs text-muted-foreground" title={path}>{path}</span>
          {content !== saved && <span className="text-xs text-amber-600">수정됨</span>}
          {!image && <>
            <Button variant={mode === "preview" ? "secondary" : "ghost"} size="sm" onClick={() => setMode("preview")}>미리보기</Button>
            <Button variant={mode === "edit" ? "secondary" : "ghost"} size="sm" onClick={() => setMode("edit")}>편집</Button>
          </>}
          <Button variant="outline" size="sm" onClick={() => void reload(true)}>다시 불러오기</Button>
          {!image && <Button size="sm" onClick={() => void save()} disabled={busy || content === saved || !revision}>저장</Button>}
        </div>
        {error && <p role="alert" className="border-b px-4 py-2 text-sm text-destructive">{error}</p>}
        {image ? (
          <div className="flex min-h-0 flex-1 items-center justify-center overflow-auto bg-muted/30 p-6">
            {/* eslint-disable-next-line @next/next/no-img-element -- The API serves workspace images at runtime. */}
            <img src={workspaceFilePreviewUrl(sessionId, path)} alt={name} className="max-h-full max-w-full object-contain" />
          </div>
        ) : mode === "preview" ? (
          <div className="min-h-0 flex-1 overflow-auto px-6 py-5">{markdown ? <Markdown>{content}</Markdown> : <pre className="whitespace-pre-wrap break-words font-mono text-sm">{content}</pre>}</div>
        ) : (
          <textarea
            aria-label={`${name} 편집`}
            spellCheck={false}
            value={content}
            onChange={(event) => setContent(event.target.value)}
            className="min-h-0 w-full flex-1 resize-none bg-background px-5 py-4 font-mono text-sm leading-6 outline-none"
          />
        )}
      </div>
    </PageShell>
  );
}
