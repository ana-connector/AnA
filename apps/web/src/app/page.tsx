"use client";

import { useState, type FormEvent } from "react";
import { useChatSocket } from "@/hooks/useChatSocket";

export default function ChatPage() {
  const { messages, connected, sendMessage } = useChatSocket();
  const [draft, setDraft] = useState("");

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault();
    if (!draft.trim()) return;
    sendMessage(draft);
    setDraft("");
  };

  return (
    <main className="mx-auto flex h-screen max-w-2xl flex-col p-4">
      <header className="mb-2 text-sm text-gray-500">
        {connected ? "connected" : "disconnected"}
      </header>
      <div className="flex-1 space-y-3 overflow-y-auto">
        {messages.map((m, i) => (
          <div
            key={i}
            className={
              m.role === "user"
                ? "ml-auto max-w-[80%] rounded-lg bg-blue-600 px-3 py-2 text-white"
                : "mr-auto max-w-[80%] rounded-lg bg-gray-200 px-3 py-2 text-gray-900"
            }
          >
            {m.text}
          </div>
        ))}
      </div>
      <form onSubmit={handleSubmit} className="mt-3 flex gap-2">
        <input
          className="flex-1 rounded border px-3 py-2"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="Message the lead agent..."
        />
        <button type="submit" className="rounded bg-blue-600 px-4 py-2 text-white">
          Send
        </button>
      </form>
    </main>
  );
}
