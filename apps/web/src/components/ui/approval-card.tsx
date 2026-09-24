"use client";

import { CheckIcon, ShieldAlertIcon, ShieldQuestionIcon, XIcon } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import type { ApprovalRequest } from "@/hooks/useChatSocket";

/** Rule categories where saying yes cannot be walked back. Everything else the
 *  gate asks about — reaching the network, touching a path outside the
 *  workspace — is a judgement call, not an emergency, and painting all of them
 *  red taught the reader to stop reading. */
const DESTRUCTIVE_CATEGORIES = new Set([
  "filesystem-destruction",
  "database-destruction",
  "git-history-destruction",
  "system-control",
  "credential-exfiltration",
  "privilege-escalation",
  "result-tampering",
]);

const CATEGORY_LABELS: Record<string, string> = {
  "filesystem-destruction": "파일 삭제·덮어쓰기",
  "database-destruction": "DB 파괴",
  "git-history-destruction": "git 이력 변경",
  "system-control": "시스템 제어",
  "credential-exfiltration": "자격증명 유출",
  "privilege-escalation": "권한 상승",
  "result-tampering": "결과 조작",
  "network-egress": "외부망 접근",
  "session-isolation": "워크스페이스 밖 경로",
  "scope-partitioning": "권한 범위 밖",
  "privilege-scope": "권한 범위 밖",
  "self-modification": "AnA 자체 수정",
  delegation: "위임",
};

/** One tool call the governance gate is holding.
 *
 *  Three things decide whether this card is any good: whether the reader can
 *  tell at a glance how bad a yes would be, whether they can read the command
 *  *and why it was stopped* without hunting, and whether they know what happens
 *  if they walk away. No answer means the gate denies it when its timeout
 *  expires, so the card says that rather than implying it waits forever.
 *
 *  Laid out as its own card rather than an Alert: Alert puts its icon in a grid
 *  column and its body in the next one, so a command block and a button row
 *  inside that body sat indented under the title and drifted out of line with
 *  everything else in the transcript. The pieces here are plain rows. */
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
      : JSON.stringify(request.tool_input, null, 2);

  const severe = request.category ? DESTRUCTIVE_CATEGORIES.has(request.category) : false;
  const categoryLabel = request.category
    ? (CATEGORY_LABELS[request.category] ?? request.category)
    : null;
  const where = request.workspace ?? request.cwd;

  return (
    <div
      className={cn(
        "flex w-full min-w-0 flex-col gap-3 rounded-lg border bg-card p-3",
        severe ? "border-destructive/50" : "border-border"
      )}
    >
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        {severe ? (
          <ShieldAlertIcon className="size-4 shrink-0 text-destructive" />
        ) : (
          <ShieldQuestionIcon className="size-4 shrink-0 text-muted-foreground" />
        )}
        <span className={cn("text-sm font-medium", severe && "text-destructive")}>
          {severe ? "되돌릴 수 없는 작업입니다" : "실행해도 될까요?"}
        </span>
        {categoryLabel ? (
          <Badge variant={severe ? "destructive" : "secondary"}>{categoryLabel}</Badge>
        ) : null}
        <Badge variant="outline" className="font-mono">
          {request.tool_name}
        </Badge>
        {request.agent ? (
          <span className="text-xs text-muted-foreground">{request.agent}</span>
        ) : null}
      </div>

      {/* The command and the reason in one block: the rule's own words above
          what it stopped. They used to be in different places — the command in
          a code block, the rule only as an id in the footnote row — so the
          reader had to know the rule set to know what they were being asked. */}
      <div className="flex min-w-0 flex-col overflow-hidden rounded-md border border-border bg-muted">
        {request.description ? (
          <p
            className={cn(
              "border-b border-border px-3 py-2 text-xs",
              severe ? "text-destructive" : "text-muted-foreground"
            )}
          >
            {request.description}
          </p>
        ) : null}
        {/* Wraps instead of scrolling sideways: a command you have to drag to
            read is a command people approve without reading. */}
        <pre className="max-h-48 min-w-0 overflow-y-auto px-3 py-2 font-mono text-xs whitespace-pre-wrap break-all text-foreground">
          {command}
        </pre>
      </div>

      <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
        {request.rule_id ? (
          <Tooltip>
            <TooltipTrigger render={<span className="font-mono" />}>
              {request.rule_id}
            </TooltipTrigger>
            <TooltipContent>config/governance.yaml 의 규칙 이름</TooltipContent>
          </Tooltip>
        ) : null}
        {where ? (
          <Tooltip>
            <TooltipTrigger render={<span className="max-w-64 truncate font-mono" />}>
              {where}
            </TooltipTrigger>
            <TooltipContent>{where}</TooltipContent>
          </Tooltip>
        ) : null}
        <span>답하지 않으면 자동으로 차단됩니다</span>
      </div>

      {/* The safe answer is the wide one and sits where the eye lands first;
          the one that cannot be undone needs a deliberate move to the right. */}
      <div className="flex flex-col gap-2 sm:flex-row sm:justify-end">
        <Button
          size="sm"
          variant="outline"
          onClick={() => onAnswer(request.request_id, "deny")}
        >
          <XIcon data-icon="inline-start" />
          차단
        </Button>
        <Button
          size="sm"
          variant={severe ? "destructive" : "default"}
          onClick={() => onAnswer(request.request_id, "allow")}
        >
          <CheckIcon data-icon="inline-start" />
          이번만 실행
        </Button>
      </div>
    </div>
  );
}
