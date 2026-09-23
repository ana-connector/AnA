"use client";

import { ShieldAlertIcon } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import type { ApprovalRequest } from "@/hooks/useChatSocket";

/** One tool call the governance gate is holding. No answer means the gate
 *  denies it when its timeout expires, so the card says so rather than
 *  implying the request waits forever. */
export function ApprovalCard({
  request,
  onAnswer,
}: {
  request: ApprovalRequest;
  onAnswer: (requestId: string, decision: "allow" | "deny") => void;
}) {
  const command =
    typeof request.tool_input?.command === "string"
      ? (request.tool_input.command as string)
      : JSON.stringify(request.tool_input);

  return (
    <Alert variant="destructive" className="w-full min-w-0 max-w-full">
      <ShieldAlertIcon />
      <AlertTitle className="flex flex-wrap items-center gap-2">
        승인 필요
        <Badge variant="outline">{request.tool_name}</Badge>
        {request.category && <Badge variant="secondary">{request.category}</Badge>}
        {request.agent && (
          <span className="text-xs font-normal text-muted-foreground">{request.agent}</span>
        )}
      </AlertTitle>
      {/* min-w-0: Alert lays its body out as a grid column, and a grid item
          defaults to min-width:auto — so a long command made the column wider
          than the card and pushed Allow/Deny past its right edge, no matter
          how the buttons themselves were arranged. */}
      <AlertDescription className="flex min-w-0 flex-col gap-2">
        <pre className="max-w-full min-w-0 overflow-x-auto rounded-md bg-muted px-3 py-2 font-mono text-xs text-foreground">
          {command}
        </pre>
        {/* Column, not a row: a long command used to stretch the metadata line
            and push Allow/Deny off the right edge. The buttons now sit on their
            own row and cannot be clipped. */}
        <div className="flex flex-col gap-2">
          <span className="min-w-0 truncate text-xs" title={request.workspace ?? undefined}>
            {request.rule_id ? `rule: ${request.rule_id}` : "rule: default"}
            {request.workspace ? ` · ws: ${request.workspace}` : ""} · 무응답 시 차단
          </span>
          <div className="flex justify-end gap-2">
            <Button
              size="sm"
              variant="destructive"
              onClick={() => onAnswer(request.request_id, "deny")}
            >
              Deny
            </Button>
            <Button size="sm" onClick={() => onAnswer(request.request_id, "allow")}>
              Allow
            </Button>
          </div>
        </div>
      </AlertDescription>
    </Alert>
  );
}
