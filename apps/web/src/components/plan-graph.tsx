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
import { ChevronRightIcon } from "lucide-react";
import type { PlanState, SubagentTask } from "@/hooks/useChatSocket";

const STATUS_LABEL: Record<SubagentTask["status"], string> = {
  blocked: "대기 취소",
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
  // Never started, because something it waited for did not finish: dimmed
  // rather than red, since it did not fail — it was not run.
  if (status === "blocked") {
    return { ...base, opacity: 0.55, borderStyle: "dashed" };
  }
  if (status === "error" || status === "timeout") {
    return { ...base, borderColor: "var(--destructive)", color: "var(--destructive)" };
  }
  return base;
}

/** The plan as a graph, drawn as it happens: the lead, its tasks, and the
 *  order they run in. A task that waits for another is drawn behind it rather
 *  than beside it, because "these two are parallel and that one is next" is
 *  the whole point of the plan. The same data drives the list below, so this
 *  is a view rather than the source of truth. */
export function PlanGraph({
  plan,
  onSelectTask,
}: {
  plan: PlanState;
  /** Opens one sub-agent's own transcript. The task you want to read is the
   *  one you are already looking at, so the graph and the list are where you
   *  ask for it — this used to be a dropdown in the window's title bar,
   *  listing every task of every turn by number. */
  onSelectTask?: (task: SubagentTask) => void;
}) {
  const { resolvedTheme } = useTheme();

  const { nodes, edges } = useMemo(() => {
    const laneHeight = 56;
    const columnWidth = 230;
    // A task's column is one past the deepest task it waits for, so the graph
    // reads left to right in the order things actually happen.
    const byId = new Map(plan.tasks.map((task) => [task.id, task]));
    const depthCache = new Map<string, number>();
    const depthOf = (id: string, seen: Set<string> = new Set()): number => {
      const cached = depthCache.get(id);
      if (cached !== undefined) return cached;
      if (seen.has(id)) return 0; // a cycle the server already broke
      seen.add(id);
      const task = byId.get(id);
      const needs = task?.needs?.filter((need) => byId.has(need)) ?? [];
      const depth = needs.length
        ? Math.max(...needs.map((need) => depthOf(need, seen) + 1))
        : 0;
      depthCache.set(id, depth);
      return depth;
    };

    const rows = new Map<number, number>();
    const placement = new Map<string, { column: number; row: number }>();
    for (const task of plan.tasks) {
      const column = depthOf(task.id);
      const row = rows.get(column) ?? 0;
      rows.set(column, row + 1);
      placement.set(task.id, { column, row });
    }
    const height = Math.max(...[...rows.values(), 1]) * laneHeight;

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
      ...plan.tasks.map((task) => ({
        id: task.id,
        position: {
          x: 260 + (placement.get(task.id)?.column ?? 0) * columnWidth,
          y: (placement.get(task.id)?.row ?? 0) * laneHeight,
        },
        data: { label: `${task.title}\n${task.agent} · ${STATUS_LABEL[task.status]}` },
        sourcePosition: Position.Right,
        targetPosition: Position.Left,
        style: { ...nodeStyle(task.status), whiteSpace: "pre-line" as const },
      })),
    ];

    const edgeList: Edge[] = plan.tasks.flatMap((task) => {
      const needs = (task.needs ?? []).filter((need) => byId.has(need));
      if (!needs.length) {
        // Nothing to wait for: it starts with the turn, so it hangs off the
        // lead.
        return [{
          id: `lead-${task.id}`,
          source: "lead",
          target: task.id,
          animated: task.status === "running",
          style: { stroke: "var(--border)" },
        }];
      }
      return needs.map((need) => ({
        id: `${need}-${task.id}`,
        source: need,
        target: task.id,
        animated: task.status === "running",
        style: { stroke: "var(--border)" },
      }));
    });

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
          onNodeClick={(_, node) => {
            const task = plan.tasks.find((item) => item.id === node.id);
            if (task) onSelectTask?.(task);
          }}
          nodesDraggable={false}
          nodesConnectable={false}
          elementsSelectable={false}
          zoomOnScroll={false}
          panOnDrag={false}
        >
          <Background gap={16} size={1} />
        </ReactFlow>
      </div>

      {/* The list is the accessible, always-readable version of the same data,
          and the keyboard path to one sub-agent's transcript. */}
      <ul className="flex flex-col gap-0.5">
        {plan.tasks.map((task) => (
          <li key={task.id} className="min-w-0">
            <button
              type="button"
              onClick={() => onSelectTask?.(task)}
              disabled={!onSelectTask}
              className="flex w-full min-w-0 items-center gap-2 rounded-md px-1.5 py-1 text-left text-xs transition-colors enabled:hover:bg-accent enabled:hover:text-accent-foreground disabled:cursor-default"
              title={onSelectTask ? `${task.title} 하위 에이전트 기록 보기` : undefined}
            >
              <Badge variant="outline">{task.agent}</Badge>
              {scopeLabel(task) && (
                <Badge variant="outline" title="대장이 이 하위 에이전트에게 준 권한 범위">
                  {scopeLabel(task)}
                </Badge>
              )}
              <span className="truncate">{task.title}</span>
              <span className="ml-auto flex shrink-0 items-center gap-1 text-muted-foreground">
                {STATUS_LABEL[task.status]}
                {task.error ? ` · ${task.error}` : ""}
                {onSelectTask && <ChevronRightIcon className="size-3" />}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
