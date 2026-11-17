import {
  FileArchiveIcon,
  FileAudioIcon,
  FileCode2Icon,
  FileCogIcon,
  FileIcon,
  FileImageIcon,
  FileJson2Icon,
  FileSpreadsheetIcon,
  FileTerminalIcon,
  FileTextIcon,
  FileVideoIcon,
} from "lucide-react";
import { cn } from "@/lib/utils";

/** Familiar file families, with one fallback for extensions we do not know. */
export function FileTypeIcon({ name, className }: { name: string; className?: string }) {
  const lower = name.toLowerCase();
  const ext = lower.split(".").pop() ?? "";
  const common = cn("size-3.5 shrink-0", className);
  if (["png", "jpg", "jpeg", "gif", "webp", "avif", "svg", "ico", "bmp"].includes(ext))
    return <FileImageIcon className={cn(common, "text-emerald-600")} />;
  if (["md", "mdx", "txt", "rst", "log"].includes(ext))
    return <FileTextIcon className={cn(common, "text-sky-600")} />;
  if (["json", "jsonc", "yaml", "yml", "toml", "xml"].includes(ext))
    return <FileJson2Icon className={cn(common, "text-amber-600")} />;
  if (["ts", "tsx", "js", "jsx", "mjs", "cjs", "py", "rs", "go", "java", "c", "h", "cpp", "cs", "rb", "php", "swift", "kt", "html", "css", "scss", "sql"].includes(ext))
    return <FileCode2Icon className={cn(common, "text-violet-600")} />;
  if (["sh", "bash", "zsh", "fish", "ps1", "bat"].includes(ext))
    return <FileTerminalIcon className={cn(common, "text-teal-600")} />;
  if (["csv", "tsv", "xls", "xlsx", "ods"].includes(ext))
    return <FileSpreadsheetIcon className={cn(common, "text-green-700")} />;
  if (["zip", "tar", "gz", "7z", "rar", "bz2"].includes(ext))
    return <FileArchiveIcon className={cn(common, "text-orange-600")} />;
  if (["mp3", "wav", "ogg", "flac", "m4a"].includes(ext))
    return <FileAudioIcon className={cn(common, "text-pink-600")} />;
  if (["mp4", "webm", "mov", "avi", "mkv"].includes(ext))
    return <FileVideoIcon className={cn(common, "text-pink-600")} />;
  if ([".gitignore", ".dockerignore", ".env", ".editorconfig"].includes(lower) || lower.startsWith(".env."))
    return <FileCogIcon className={cn(common, "text-slate-500")} />;
  return <FileIcon className={cn(common, "text-muted-foreground")} />;
}
