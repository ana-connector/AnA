"use client";

import { CheckIcon, ShieldAlertIcon, ShieldQuestionIcon, XIcon } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
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
  delegation: "위임",
};

/** One tool call the governance gate is holding.
 *
 *  Three things decide whether this card is any good: whether the reader can
 *  tell at a glance how bad a yes would be, whether they can read the command
 *  without a tooltip, and whether they know what happens if they walk away.
 *  No answer means the gate denies it when its timeout expires, so the card
 *  says that rather than implying the request waits forever. */
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
    <Alert
      variant={severe ? "destructive" : "default"}
      className="w-full min-w-0 max-w-full"
    >
      {severe ? <ShieldAlertIcon /> : <ShieldQuestionIcon />}
      <AlertTitle className="flex flex-wrap items-center gap-2">
        {severe ? "되돌릴 수 없는 작업입니다" : "실행해도 될까요?"}
        {categoryLabel ? (
          <Badge variant={severe ? "destructive" : "secondary"}>{categoryLabel}</Badge>
        ) : null}
        <Badge variant="outline" className="font-mono">
          {request.tool_name}
        </Badge>
        {request.agent ? (
          <span className="text-xs font-normal text-muted-foreground">{request.agent}</span>
        ) : null}
      </AlertTitle>

      {/* min-w-0: Alert lays its body out as a grid column, and a grid item
          defaults to min-width:auto — so a long command made the column wider
          than the card and pushed the buttons past its right edge, no matter
          how they were arranged. */}
      <AlertDescription className="flex min-w-0 flex-col gap-3">
        {/* Wraps instead of scrolling sideways: a command you have to drag to
            read is a command people approve without reading. */}
        <pre className="max-h-48 max-w-full min-w-0 overflow-y-auto rounded-md bg-muted px-3 py-2 font-mono text-xs whitespace-pre-wrap break-all text-foreground">
          {command}
        </pre>

        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
          {request.rule_id ? (
            <Tooltip>
              <TooltipTrigger render={<span className="text-muted-foreground" />}>
                규칙 {request.rule_id}
              </TooltipTrigger>
              <TooltipContent>config/governance.yaml 의 규칙 이름</TooltipContent>
            </Tooltip>
          ) : null}
          {where ? (
            <Tooltip>
              <TooltipTrigger
                render={<span className="max-w-64 truncate font-mono text-muted-foreground" />}
              >
                {where}
              </TooltipTrigger>
              <TooltipContent>{where}</TooltipContent>
            </Tooltip>
          ) : null}
          <span className="text-muted-foreground">답하지 않으면 자동으로 차단됩니다</span>
        </div>

        {/* The safe answer is the wide one and sits where the eye lands first;
            the one that cannot be undone needs a deliberate move to the right.
            Both used to be English words of equal weight. */}
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
      </AlertDescription>
    </Alert>
  );
}
