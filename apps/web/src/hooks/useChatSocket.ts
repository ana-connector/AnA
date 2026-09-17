"use client";

import { useCallback, useEffect, useRef, useState } from "react";

export type ChatEvent = {
  type: "text" | "result" | "usage" | "error";
  text: string;
  session_id: string | null;
  is_error: boolean;
};

export type ChatMessage = {
  role: "user" | "agent";
  text: string;
};

const WS_URL = process.env.NEXT_PUBLIC_WS_URL ?? "ws://localhost:8000/ws/chat";

export function useChatSocket() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [connected, setConnected] = useState(false);
  const socketRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    const socket = new WebSocket(WS_URL);
    socketRef.current = socket;
    let agentMessageIndex: number | null = null;

    socket.onopen = () => setConnected(true);
    socket.onclose = () => setConnected(false);

    socket.onmessage = (raw) => {
      const event = JSON.parse(raw.data) as ChatEvent;

      if (event.type === "text") {
        setMessages((prev) => {
          if (agentMessageIndex === null) {
            agentMessageIndex = prev.length;
            return [...prev, { role: "agent", text: event.text }];
          }
          const next = [...prev];
          next[agentMessageIndex] = {
            role: "agent",
            text: next[agentMessageIndex].text + event.text,
          };
          return next;
        });
      }

      if (event.type === "result") {
        agentMessageIndex = null;
      }
    };

    return () => socket.close();
  }, []);

  const sendMessage = useCallback((text: string) => {
    setMessages((prev) => [...prev, { role: "user", text }]);
    socketRef.current?.send(text);
  }, []);

  return { messages, connected, sendMessage };
}
