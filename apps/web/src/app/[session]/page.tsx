import ChatPage from "@/components/chat-page";

export default async function SessionPage({ params }: PageProps<"/[session]">) {
  const { session } = await params;
  return <ChatPage key={session} sessionId={session} />;
}
