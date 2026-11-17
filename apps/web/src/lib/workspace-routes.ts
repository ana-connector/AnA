export function fileHref(workspaceId: string, path: string) {
  return `/files/${encodeURIComponent(workspaceId)}/${path.split("/").map(encodeURIComponent).join("/")}`;
}

export function agentHref(workspaceId: string, taskId: string, turnId?: number, startedAt?: number) {
  return `/agents/${encodeURIComponent(workspaceId)}/${turnId ?? `live-${startedAt ?? 0}`}/${encodeURIComponent(taskId)}`;
}

export function workspaceFromHref(href: string): string | null {
  const parts = href.split("/").filter(Boolean);
  if (parts[0] === "files" || parts[0] === "agents") {
    if (!parts[1]) return null;
    try { return decodeURIComponent(parts[1]); } catch { return null; }
  }
  if (parts.length !== 1 || ["settings", "tools", "secrets", "automation", "start", "browser", "ui-harness"].includes(parts[0])) return null;
  try { return decodeURIComponent(parts[0]); } catch { return null; }
}
