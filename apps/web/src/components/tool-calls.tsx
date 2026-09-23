"use client";

import { TerminalIcon } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import type { ToolEvent } from "@/hooks/useChatSocket";
import { toolDisplay } from "@/lib/tool-display";

function commandOf(tool: ToolEvent): string {
  return toolDisplay(tool.tool_name, tool.input);
}

function verdictVariant(verdict: string) {
  if (verdict === "deny") return "destructive" as const;
  if (verdict === "pending") return "outline" as const;
  return "secondary" as const;
}

/** What the agent actually ran, inline under its reply. Every entry comes from
 *  the governance gate, so this is the same record the audit log keeps — not a
 *  separate, prettier story. */
export function ToolCalls({ tools }: { tools: ToolEvent[] }) {
  if (tools.length === 0) return null;

  return (
    <div className="flex flex-col gap-1 rounded-md border border-border bg-background/60 p-2">
      {tools.map((tool, index) => (
        <div key={index} className="flex items-start gap-2 text-xs">
          <TerminalIcon className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" />
          <code className="min-w-0 flex-1 truncate font-mono" title={commandOf(tool)}>
            {commandOf(tool)}
          </code>
          <Badge variant={verdictVariant(tool.verdict)} className="shrink-0">
            {tool.verdict}
          </Badge>
        </div>
      ))}
    </div>
  );
}
