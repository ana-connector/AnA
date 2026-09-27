import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

/** The same small keycap wherever a keyboard shortcut is shown. */
export function ShortcutKey({ children, className }: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <kbd className={cn(
      "inline-flex shrink-0 items-center rounded border border-border px-1 font-mono text-[10px] font-normal leading-4 text-muted-foreground",
      className
    )}>
      {children}
    </kbd>
  );
}
