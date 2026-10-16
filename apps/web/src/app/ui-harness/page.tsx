"use client";

/**
 * A page for driving the two components ANA had to write itself — the message
 * scroller and the stepped question form — from a real browser.
 *
 * They replaced `@shadcn/react` primitives whose behaviour nothing in the
 * repository pinned down, so the replacement is checked the only way that
 * means anything: Chromium, real scrolling, real keystrokes. The checks live
 * in `apps/web/tests/browser/`; see the README there for how to run them.
 *
 * Development only. In a production build the page renders nothing, so the
 * harness cannot be reached on a deployed daemon.
 */

import { useState } from "react";
import { ApprovalCard } from "@/components/ui/approval-card";
import { PlanGraph } from "@/components/plan-graph";
import { QuestionCard } from "@/components/ui/question-card";
import {
  MessageScroller,
  MessageScrollerButton,
  MessageScrollerContent,
  MessageScrollerItem,
  MessageScrollerProvider,
  MessageScrollerViewport,
} from "@/components/ui/message-scroller";
import type {
  ApprovalRequest,
  PlanState,
  QuestionRequest,
  SubagentTask,
} from "@/hooks/useChatSocket";

declare global {
  interface Window {
    /** Every answer the card has handed back, for the checks to read. */
    __answers: unknown[];
  }
}

/** One required single answer, one optional multiple, one free-text: between
 *  them they cover every path the card has. */
const request: QuestionRequest = {
  request_id: "req-1",
  agent: "claude",
  role: "lead",
  questions: [
    {
      question: "배포 대상은?",
      options: ["스테이징", "운영"],
      multi: false,
      note: "",
      required: true,
    },
    {
      question: "함께 켤 기능",
      options: [
        { value: "metrics", label: "지표", description: "수집을 켭니다" },
        { value: "tracing", label: "추적" },
      ],
      multi: true,
      note: "",
      required: false,
    },
    {
      question: "메모",
      options: ["없음"],
      input: { label: "자유 입력", placeholder: "메모" },
      multi: false,
      note: "",
      required: false,
    },
  ],
};

const approval: ApprovalRequest = {
  request_id: "ap-1",
  workspace: "/home/hello/ana",
  tool_name: "Bash",
  tool_input: { command: "git push --dry-run origin main" },
  description: "원격 저장소에 쓰는 명령입니다. 이 턴의 쓰기 범위 밖입니다.",
  rule_id: "git-push",
  category: "network",
  agent: "codex",
  cwd: "/home/hello/ana",
};

/** A fan-out with a dependency, one long failure message, and a task that
 *  arrives after the others — the three things that broke the diagram. */
const baseTasks: SubagentTask[] = [
  { id: "t1", title: "테스트 실행", agent: "codex", model: "gpt-6-astra", status: "done" },
  { id: "t2", title: "실패 원인 조사", agent: "claude", model: "fable", status: "running" },
  {
    id: "t3",
    title: "수정안 작성",
    agent: "claude",
    model: "opus",
    status: "blocked",
    needs: ["t2"],
  },
];

export default function UiHarness() {
  const [messages, setMessages] = useState<string[]>(
    Array.from({ length: 40 }, (_, index) => `메시지 ${index + 1}`)
  );
  // Stands in for an answer arriving token by token: the item keeps its
  // identity and only grows, which is a resize rather than a new child.
  const [grow, setGrow] = useState(0);
  const [tasks, setTasks] = useState<SubagentTask[]>(baseTasks);
  const plan: PlanState = { mode: "delegate", tasks, note: "" };

  if (process.env.NODE_ENV === "production") return null;

  return (
    <div className="flex h-screen flex-col gap-4 p-4">
      <div className="flex gap-2">
        <button
          id="append"
          type="button"
          onClick={() => setMessages((prev) => [...prev, `메시지 ${prev.length + 1}`])}
        >
          append
        </button>
        <button id="grow" type="button" onClick={() => setGrow((value) => value + 1)}>
          grow
        </button>
        <button
          id="fail-task"
          type="button"
          onClick={() =>
            setTasks((current) =>
              current.map((task) =>
                task.id === "t2"
                  ? {
                      ...task,
                      status: "error" as const,
                      error:
                        "uvx 를 찾지 못해 MCP 서버 두 개가 비활성화됐고, 그래서 " +
                        "capabilities 응답의 active 목록이 기대값과 달라졌습니다. " +
                        "PATH 를 확인하세요.",
                    }
                  : task
              )
            )
          }
        >
          fail
        </button>
        <button
          id="add-task"
          type="button"
          onClick={() =>
            setTasks((current) => [
              ...current,
              {
                id: `t${current.length + 1}`,
                title: `추가 작업 ${current.length + 1}`,
                agent: "kimi",
                status: "running" as const,
              },
            ])
          }
        >
          add
        </button>
      </div>
      <div className="h-80 border" id="scroller-host">
        <MessageScrollerProvider autoScroll>
          <MessageScroller className="min-h-0 flex-1">
            <MessageScrollerViewport id="viewport">
              <MessageScrollerContent>
                {messages.map((text, index) => (
                  <MessageScrollerItem key={index} messageId={`m-${index}`}>
                    <p>{text}</p>
                  </MessageScrollerItem>
                ))}
                <MessageScrollerItem messageId="streaming">
                  <p id="streaming">{"스트리밍 ".repeat(grow * 20)}</p>
                </MessageScrollerItem>
              </MessageScrollerContent>
            </MessageScrollerViewport>
            <MessageScrollerButton />
          </MessageScroller>
        </MessageScrollerProvider>
      </div>
      <div id="plan-host" className="w-full max-w-3xl">
        <PlanGraph plan={plan} />
      </div>
      <ApprovalCard
        request={approval}
        connected
        connectionVersion={1}
        onAnswer={(requestId, decision) => {
          window.__answers = window.__answers ?? [];
          window.__answers.push({ requestId, decision });
          return true;
        }}
      />
      <QuestionCard
        request={request}
        connected
        connectionVersion={1}
        onAnswer={(requestId, answers) => {
          window.__answers = window.__answers ?? [];
          window.__answers.push({ requestId, answers });
          return true;
        }}
      />
    </div>
  );
}
