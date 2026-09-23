"use client";

import { useEffect, useRef, type FormEvent, type KeyboardEvent } from "react";
import { NetworkIcon, SendIcon, SquareIcon, ZapIcon } from "lucide-react";
import {
  InputGroup,
  InputGroupAddon,
  InputGroupButton,
  InputGroupTextarea,
} from "@/components/ui/input-group";

const MAX_ROWS = 3;

/** The message box: a textarea that grows to three lines, with the send button
 *  and the turn's mode inside it.
 *
 *  Enter sends and Shift+Enter makes a newline — the usual chat contract, which
 *  a plain textarea does not give you. */
export function Composer({
  value,
  onChange,
  onSubmit,
  onCancel,
  streaming,
  connected,
  planMode,
  onPlanModeChange,
  placeholder,
}: {
  value: string;
  onChange: (value: string) => void;
  onSubmit: () => void;
  onCancel: () => void;
  streaming: boolean;
  connected: boolean;
  planMode: boolean;
  onPlanModeChange: (value: boolean) => void;
  placeholder: string;
}) {
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  // Grow with the text up to MAX_ROWS, then scroll — measured from the element
  // rather than counted in characters, which wrapping makes meaningless.
  useEffect(() => {
    const element = textareaRef.current;
    if (!element) return;
    element.style.height = "auto";
    const lineHeight = parseFloat(getComputedStyle(element).lineHeight || "20");
    const padding = element.offsetHeight - element.clientHeight;
    element.style.height = `${Math.min(element.scrollHeight, lineHeight * MAX_ROWS + padding) }px`;
  }, [value]);

  const submit = (event?: FormEvent) => {
    event?.preventDefault();
    if (!value.trim() || streaming) return;
    onSubmit();
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      submit();
    }
  };

  return (
    <form onSubmit={submit} className="mx-auto w-full max-w-3xl shrink-0 px-4 py-3">
      <InputGroup>
        <InputGroupTextarea
          ref={textareaRef}
          rows={1}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder={placeholder}
          className="max-h-32 resize-none"
        />
        <InputGroupAddon align="block-end">
          <InputGroupButton
            type="button"
            size="sm"
            variant={planMode ? "default" : "ghost"}
            aria-pressed={planMode}
            onClick={() => onPlanModeChange(!planMode)}
            title={planMode ? "Plan 모드 · 눌러서 Direct로 전환" : "Direct 모드 · 눌러서 Plan으로 전환"}
          >
            {planMode ? <NetworkIcon /> : <ZapIcon />}
            {planMode ? "Plan" : "Direct"}
          </InputGroupButton>

          {streaming ? (
            <InputGroupButton
              type="button"
              size="icon-sm"
              variant="destructive"
              className="ml-auto"
              onClick={onCancel}
              title="이 턴 중단"
            >
              <SquareIcon />
            </InputGroupButton>
          ) : (
            <InputGroupButton
              type="submit"
              size="icon-sm"
              variant="default"
              className="ml-auto"
              disabled={!connected || !value.trim()}
              title="보내기 (Enter)"
            >
              <SendIcon />
            </InputGroupButton>
          )}
        </InputGroupAddon>
      </InputGroup>
    </form>
  );
}
