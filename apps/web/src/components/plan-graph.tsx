"use client";

import { useMemo } from "react";
import { useTheme } from "next-themes";
import {
  Background,
  Position,
  ReactFlow,
  type Edge,
  type Node,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { Badge } from "@/components/ui/badge";
import type { PlanState, SubagentTask } from "@/hooks/useChatSocket";

const STATUS_LABEL: Record<SubagentTask["status"], string> = {
  running: "실행 중",
  done: "완료",
  error: "실패",
  timeout: "시간 초과",
  unknown: "상태 확인 불가",
};

/** Node colors come from the app's own tokens rather than React Flow's
 *  defaults, which are light-mode constants and turn into dark-on-dark. */
/** "읽기 전용", "docs/" or both — shown only when the lead actually narrowed
 *  the scope, so an ordinary task carries no extra badge. */
function scopeLabel(task: SubagentTask): string | null {
  const scope = task.scope;
  if (!scope) return null;
  const parts: string[] = [];
  if (scope.mode === "read") parts.push("읽기 전용");
  if (scope.paths?.length) parts.push(scope.paths.join(", "));
  return parts.length ? parts.join(" · ") : null;
}

function nodeStyle(status?: SubagentTask["status"]): React.CSSProperties {
  const base: React.CSSProperties = {
    background: "var(--card)",
    color: "var(--card-foreground)",
    border: "1px solid var(--border)",
    borderRadius: "var(--radius-md, 8px)",
    fontSize: 12,
    padding: "6px 10px",
    width: 210,
  };
  if (status === "done") return { ...base, borderColor: "var(--primary)" };
  if (status === "error" || status === "timeout") {
    return { ...base, borderColor: "var(--destructive)", color: "var(--destructive)" };
  }
  return base;
}

/** The lead fanning out to its sub-agents, drawn as it happens. The same plan
 *  data drives the list below, so this is a view rather than the source of
 *  truth. */
export function PlanGraph({ plan }: { plan: PlanState }) {
  const { resolvedTheme } = useTheme();

  const { nodes, edges } = useMemo(() => {
    const laneHeight = 56;
    const height = Math.max(plan.tasks.length, 1) * laneHeight;

    const nodeList: Node[] = [
      {
        id: "lead",
        position: { x: 0, y: height / 2 - 18 },
        data: { label: "lead" },
        // The layout runs left to right, so the handles have to as well —
        // the defaults are top/bottom, which is why the edges left the lead
        // node from its underside and looked wrong.
        sourcePosition: Position.Right,
        targetPosition: Position.Left,
        style: { ...nodeStyle(), width: 110, textAlign: "center" },
      },
      ...plan.tasks.map((task, index) => ({
        id: task.id,
        position: { x: 260, y: index * laneHeight },
        data: { label: `${task.title}\n${task.agent} · ${STATUS_LABEL[task.status]}` },
        sourcePosition: Position.Right,
        targetPosition: Position.Left,
        style: { ...nodeStyle(task.status), whiteSpace: "pre-line" as const },
      })),
    ];

    const edgeList: Edge[] = plan.tasks.map((task) => ({
      id: `lead-${task.id}`,
      source: "lead",
      target: task.id,
      animated: task.status === "running",
      style: { stroke: "var(--border)" },
    }));

    return { nodes: nodeList, edges: edgeList };
  }, [plan]);

  return (
    <div className="flex min-w-0 flex-col gap-2 rounded-lg border border-border p-3">
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant="secondary">Planner-Worker</Badge>
        <span className="text-xs text-muted-foreground">
          서브에이전트 {plan.tasks.length}개 병렬 실행
          {plan.note ? ` · ${plan.note}` : ""}
        </span>
      </div>

      <div className="h-[200px] w-full">
        <ReactFlow
          nodes={nodes}
          edges={edges}
          fitView
          // React Flow ships its own dark palette; without this the canvas and
          // the controls stay light-mode while the app is dark.
          colorMode={resolvedTheme === "dark" ? "dark" : "light"}
          proOptions={{ hideAttribution: true }}
          nodesDraggable={false}
          nodesConnectable={false}
          elementsSelectable={false}
          zoomOnScroll={false}
          panOnDrag={false}
        >
          <Background gap={16} size={1} />
        </ReactFlow>
      </div>

      {/* The list is the accessible, always-readable version of the same data. */}
      <ul className="flex flex-col gap-1">
        {plan.tasks.map((task) => (
          <li key={task.id} className="flex items-center gap-2 text-xs">
            <Badge variant="outline">{task.agent}</Badge>
            {scopeLabel(task) && (
              <Badge variant="outline" title="대장이 이 하위 에이전트에게 준 권한 범위">
                {scopeLabel(task)}
              </Badge>
            )}
            <span className="truncate">{task.title}</span>
            <span className="ml-auto shrink-0 text-muted-foreground">
              {STATUS_LABEL[task.status]}
              {task.error ? ` · ${task.error}` : ""}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
