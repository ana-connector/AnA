"use client";

import { useEffect, useState } from "react";
import type { TurnStatus } from "@/hooks/useChatSocket";

/** What each phase is called, for the person waiting. The server sends a key —
 *  the wording lives here with every other Korean string. */
const PHASE_LABELS: Record<TurnStatus["phase"], string> = {
  thinking: "생각하고 있습니다",
  planning: "계획을 세우고 있습니다",
  delegating: "하위 에이전트가 작업하고 있습니다",
  replanning: "결과를 보고 다음 작업을 정하고 있습니다",
  synthesizing: "결과를 모아 답을 쓰고 있습니다",
  answering: "답을 쓰고 있습니다",
};

/** The line that says something is happening.
 *
 *  A turn can spend a minute planning and another few running workers before a
 *  single character of the answer exists. The screen used to say nothing for all
 *  of that, which reads exactly like a broken connection — so this names the
 *  phase, the model doing it, and how long it has been going. The elapsed count
 *  is the part that makes waiting bearable: a number that keeps moving is proof
 *  the thing is alive. */
export function TurnStatusLine({
  status,
  model,
}: {
  status: TurnStatus;
  /** The model actually running, when the CLI has said which. */
  model?: string;
}) {
  // A clock rather than a counter: the effect only starts the interval, and the
  // elapsed seconds are derived, so a new phase counts from its own start
  // without anything being reset from inside an effect.
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);

  const elapsed = Math.max(0, Math.round((now - status.since) / 1000));

  const who = model ?? status.agent;
  const what = PHASE_LABELS[status.phase] ?? PHASE_LABELS.thinking;
  const count = status.phase === "delegating" && status.count > 0 ? ` (${status.count}개)` : "";

  return (
    <div
      className="flex min-w-0 items-center gap-2 text-xs text-muted-foreground"
      aria-live="polite"
    >
      <span className="relative flex size-1.5 shrink-0">
        <span className="absolute inline-flex size-full animate-ping rounded-full bg-primary opacity-60" />
        <span className="relative inline-flex size-1.5 rounded-full bg-primary" />
      </span>
      <span className="truncate">
        {who}이(가) {what}
        {count}
      </span>
      {/* Only once it has been a moment: a counter that appears instantly makes
          a fast turn feel slow. */}
      {elapsed >= 3 && <span className="shrink-0 tabular-nums">{elapsed}초</span>}
    </div>
  );
}
