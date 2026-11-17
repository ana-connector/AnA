import AgentTaskPage from "@/components/agent-task-page";

export default async function Page({ params }: { params: Promise<{ session: string; turn: string; task: string }> }) {
  const { session, turn, task } = await params;
  return <AgentTaskPage sessionId={session} taskId={task}
    turnId={turn.startsWith("live") ? null : Number(turn)}
    startedAt={turn.startsWith("live-") ? Number(turn.slice(5)) : null} />;
}
