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
  // Total number of messages appended so far, kept in sync by hand next to
  // every setMessages call that appends one. React state (`messages`) can't
  // serve this purpose here: it's only guaranteed current after a render,
  // but onmessage needs the *next* index synchronously, before that render
  // happens, to record where the in-progress agent message lives.
  const messageCountRef = useRef(0);
  // Index of the in-progress agent message in `messages`, or null when no
  // agent turn is currently streaming. Both this and messageCountRef above
  // are read and written from the onmessage handler itself, never from
  // inside a setMessages updater: React requires updaters to be pure, and
  // React 18+ StrictMode invokes them twice in development specifically to
  // surface impure updaters (a second invocation reading a ref the first
  // invocation just mutated would take the wrong branch and can throw).
  const agentMessageIndexRef = useRef<number | null>(null);

  useEffect(() => {
    const socket = new WebSocket(WS_URL);
    socketRef.current = socket;

    socket.onopen = () => setConnected(true);
    socket.onclose = () => setConnected(false);

    socket.onmessage = (raw) => {
      const event = JSON.parse(raw.data) as ChatEvent;

      if (event.type === "text") {
        if (agentMessageIndexRef.current === null) {
          agentMessageIndexRef.current = messageCountRef.current;
          messageCountRef.current += 1;
          setMessages((prev) => [...prev, { role: "agent", text: event.text }]);
        } else {
          const index = agentMessageIndexRef.current;
          setMessages((prev) => {
            const next = [...prev];
            next[index] = {
              role: "agent",
              text: next[index].text + event.text,
            };
            return next;
          });
        }
      }

      if (event.type === "error") {
        messageCountRef.current += 1;
        setMessages((prev) => [...prev, { role: "agent", text: `⚠️ ${event.text}` }]);
        agentMessageIndexRef.current = null;
      }

      if (event.type === "result") {
        agentMessageIndexRef.current = null;
      }
    };

    return () => socket.close();
  }, []);

  const sendMessage = useCallback((text: string) => {
    messageCountRef.current += 1;
    setMessages((prev) => [...prev, { role: "user", text }]);
    socketRef.current?.send(text);
  }, []);

  return { messages, connected, sendMessage };
}
