"use client";

import { useEffect, useMemo, useRef, useState, type RefObject } from "react";
import { useTheme } from "next-themes";
import {
  Background,
  Controls,
  Position,
  ReactFlow,
  useReactFlow,
  type Edge,
  type Node,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import { ChevronDownIcon, ChevronRightIcon } from "lucide-react";
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

function FitWhenReady({ container, taskIds }: {
  container: RefObject<HTMLDivElement | null>;
  taskIds: string;
}) {
  const flow = useReactFlow();

  // Deliberately not gated on useNodesInitialized(). That hook reports the
  // sizes React Flow has *measured*, and measurements only reach its store
  // through onNodesChange — which a flow driven by a computed `nodes` prop,
  // as this one is, never sends. It stayed false forever, so the fit below
  // never ran at all: the canvas sat at translate(0,0) with the plan in its
  // corner. The nodes carry explicit width and height instead, which is all
  // fitView needs and is true anyway: they are a fixed size.
  useEffect(() => {
    if (!container.current) return;
    const element = container.current;
    let cancelled = false;
    let frame = 0;
    // fitView answers whether it actually did anything, and it does nothing
    // while the pane has no size — which is the normal case here, because the
    // transcript mounts the graph before laying it out. The previous version
    // called it once behind a latch and cancelled the pending frame whenever
    // React re-ran the effect, so in practice it often never ran at all: the
    // canvas stayed at translate(0,0), the plan sat in its top-left corner,
    // and a wider fan-out was simply off screen.
    const attempt = (tries: number) => {
      if (cancelled || tries > 60) return;
      if (element.clientWidth === 0 || element.clientHeight === 0) {
        frame = requestAnimationFrame(() => attempt(tries + 1));
        return;
      }
      void Promise.resolve(flow.fitView({ padding: 0.16 })).then((fitted) => {
        if (cancelled || fitted) return;
        frame = requestAnimationFrame(() => attempt(tries + 1));
      });
    };
    // A resize is the other moment the view needs re-centring: the sidebar
    // opening, or the window changing, moves the pane out from under it.
    const observer = new ResizeObserver(() => attempt(0));
    observer.observe(element);
    attempt(0);
    return () => {
      cancelled = true;
      observer.disconnect();
      cancelAnimationFrame(frame);
    };
  }, [container, flow, taskIds]);

  return null;
}

/** The plan as a graph, drawn as it happens: the lead, its tasks, and the
 *  order they run in. A task that waits for another is drawn behind it rather
 *  than beside it, because "these two are parallel and that one is next" is
 *  the whole point of the plan. The same data drives the list below, so this
 *  is a view rather than the source of truth. */
export function PlanGraph({
  plan,
  onSelectTask,
  defaultOpen = true,
  autoCollapse = false,
}: {
  plan: PlanState;
  /** Opens one sub-agent's own transcript. The task you want to read is the
   *  one you are already looking at, so the graph and the list are where you
   *  ask for it — this used to be a dropdown in the window's title bar,
   *  listing every task of every turn by number. */
  onSelectTask?: (task: SubagentTask) => void;
  /** Finished turns start folded: scrolling back through a conversation should
   *  show what was said, not a stack of canvases. */
  defaultOpen?: boolean;
  /** Folds the card while it is still the live plan — set once the lead starts
   *  writing its answer. The 200px canvas plus the task list used to sit below
   *  the streaming reply and push it out of the viewport, so you watched the
   *  graph and read the answer by scrolling up. A manual toggle wins over this.
   */
  autoCollapse?: boolean;
}) {
  const { resolvedTheme } = useTheme();
  const [toggled, setToggled] = useState<boolean | null>(null);
  const canvasRef = useRef<HTMLDivElement>(null);
  const open = toggled ?? (autoCollapse ? false : defaultOpen);
  const running = plan.tasks.filter((task) => task.status === "running").length;
  const done = plan.tasks.filter((task) => task.status === "done").length;
  const failed = plan.tasks.filter(
    (task) => task.status === "error" || task.status === "timeout"
  ).length;

  const { nodes, edges, rows } = useMemo(() => {
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
        // Given rather than measured. Every status change rebuilds this array
        // from scratch, and a rebuilt node arrives without the size React Flow
        // had measured for it — which is what left the canvas blank until a
        // reload.
        width: 110,
        height: 32,
        style: { ...nodeStyle(), width: 110, textAlign: "center" },
      },
      ...plan.tasks.map((task) => ({
        id: task.id,
        position: {
          x: 260 + (placement.get(task.id)?.column ?? 0) * columnWidth,
          y: (placement.get(task.id)?.row ?? 0) * laneHeight,
        },
        // The model that ran it, when the CLI said which — "codex" alone does
        // not say what answered, and a plan routes tasks to different models.
        data: {
          label: `${task.title}\n${task.model ?? task.agent} · ${STATUS_LABEL[task.status]}`,
        },
        sourcePosition: Position.Right,
        targetPosition: Position.Left,
        width: 210,
        height: 50,
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

    return { nodes: nodeList, edges: edgeList, rows: Math.max(...[...rows.values(), 1]) };
  }, [plan]);

  // The canvas grows with the plan instead of fitting everything into 200px:
  // a ten-task fan-out used to shrink until the titles were unreadable. Capped
  // so a huge plan does not push the reply off the screen either — past that,
  // panning and zoom take over.
  const canvasHeight = Math.min(Math.max(200, rows * 72 + 60), 420);

  return (
    <div className="flex min-w-0 flex-col gap-2 rounded-lg border border-border p-3">
      <button
        type="button"
        onClick={() => setToggled(!open)}
        aria-expanded={open}
        className="flex min-w-0 flex-wrap items-center gap-2 rounded-sm text-left hover:opacity-80"
      >
        <ChevronDownIcon
          className={cn("size-3 shrink-0 text-muted-foreground transition-transform", !open && "-rotate-90")}
        />
        <Badge variant="secondary">Planner-Worker</Badge>
        <span className="text-xs text-muted-foreground">
          서브에이전트 {plan.tasks.length}개
          {running > 0 ? ` · 실행 중 ${running}` : ""}
          {done > 0 ? ` · 완료 ${done}` : ""}
          {failed > 0 ? ` · 실패 ${failed}` : ""}
          {plan.note ? ` · ${plan.note}` : ""}
        </span>
      </button>

      {!open ? null : (
      <>
      {/* Draggable and pannable: the layout is ANA's guess at a readable
          arrangement, and the person reading it is allowed to disagree —
          especially once the fan-out is wide enough that nodes overlap. */}
      <div ref={canvasRef} className="w-full" style={{ height: canvasHeight }}>
        <ReactFlow
          nodes={nodes}
          edges={edges}
          // React Flow ships its own dark palette; without this the canvas and
          // the controls stay light-mode while the app is dark.
          colorMode={resolvedTheme === "dark" ? "dark" : "light"}
          proOptions={{ hideAttribution: true }}
          onNodeClick={(_, node) => {
            const task = plan.tasks.find((item) => item.id === node.id);
            if (task) onSelectTask?.(task);
          }}
          nodesDraggable
          nodesConnectable={false}
          elementsSelectable={false}
          // Wheel keeps scrolling the transcript rather than zooming the graph:
          // a canvas that swallows the page scroll is a trap halfway through a
          // long conversation. Zoom is the buttons, pinch, or double-click.
          zoomOnScroll={false}
          preventScrolling={false}
          panOnDrag
          minZoom={0.2}
          maxZoom={2}
        >
          <FitWhenReady container={canvasRef} taskIds={plan.tasks.map((task) => task.id).join("|")} />
          <Background gap={16} size={1} />
          <Controls showInteractive={false} className="!shadow-none" />
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
              {task.model && task.model !== task.agent && (
                <span className="shrink-0 truncate font-mono text-[11px] text-muted-foreground">
                  {task.model}
                </span>
              )}
              {scopeLabel(task) && (
                <Badge variant="outline" title="대장이 이 하위 에이전트에게 준 권한 범위">
                  {scopeLabel(task)}
                </Badge>
              )}
              <span className="shrink-0 truncate">{task.title}</span>
              {/* The reason a task failed is a sentence, sometimes a
                  paragraph: a CLI's own error text. Shown on one line and cut
                  off, with the whole of it on hover, because left to run it
                  pushed the row past the width of the transcript. */}
              {task.error ? (
                <span
                  className="min-w-0 flex-1 truncate text-destructive"
                  title={task.error}
                >
                  {task.error}
                </span>
              ) : null}
              <span
                className={cn(
                  "flex shrink-0 items-center gap-1 text-muted-foreground",
                  !task.error && "ml-auto"
                )}
              >
                {STATUS_LABEL[task.status]}
                {onSelectTask && <ChevronRightIcon className="size-3" />}
              </span>
            </button>
          </li>
        ))}
      </ul>
      </>
      )}
    </div>
  );
}
