"use client";

import {
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
  type Ref,
} from "react";
import { SendIcon, SquareIcon } from "lucide-react";
import {
  InputGroup,
  InputGroupAddon,
  InputGroupButton,
  InputGroupTextarea,
} from "@/components/ui/input-group";

const MAX_ROWS = 3;

/** The message box: a textarea that grows to three lines, with the send button
 *  inside it.
 *
 *  No plan/direct switch: the lead has no tools of its own, so whether a
 *  request needs sub-agents is a judgement about the request, not a setting —
 *  and offering it meant the user could pick the mode that cannot do the job.
 *  It decides per turn now (see packages/orchestrator/planner.py).
 *
 *  Enter sends and Shift+Enter makes a newline — the usual chat contract, which
 *  a plain textarea does not give you.
 *
 *  The draft lives here, not in the page. Held by the chat page, every
 *  keystroke re-rendered the whole transcript — every reply's markdown, the
 *  plan graph — and typing lagged more the longer the conversation got. The
 *  page only hears about the text when it is sent, and fills it through
 *  `ref` (the starter suggestions). */
export type ComposerHandle = { fill: (text: string) => void };

export function Composer({
  ref,
  onSubmit,
  onCancel,
  streaming,
  connected,
  placeholder,
}: {
  ref?: Ref<ComposerHandle>;
  /** Resolves after the server confirms receipt; the box is cleared only then. */
  onSubmit: (text: string) => Promise<boolean>;
  onCancel: () => void;
  streaming: boolean;
  connected: boolean;
  placeholder: string;
}) {
  const [value, setValue] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  useImperativeHandle(ref, () => ({
    fill: (text: string) => {
      setValue(text);
      textareaRef.current?.focus();
    },
  }), []);

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

  const submit = async (event?: FormEvent) => {
    event?.preventDefault();
    if (!value.trim() || streaming || submitting) return;
    const sent = value;
    setSubmitting(true);
    try {
      if (await onSubmit(sent)) setValue((current) => current === sent ? "" : current);
    } finally {
      setSubmitting(false);
    }
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    // Some IMEs report isComposing=false on the Enter that confirms the final
    // syllable. keyCode 229 still marks that key as part of composition.
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing
      && event.nativeEvent.keyCode !== 229) {
      event.preventDefault();
      void submit();
    }
  };

  return (
    <form onSubmit={submit} className="mx-auto w-full max-w-3xl shrink-0 px-4 py-3">
      <InputGroup>
        <InputGroupTextarea
          ref={textareaRef}
          rows={1}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder={placeholder}
          className="max-h-32 resize-none"
        />
        <InputGroupAddon align="block-end">
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
              disabled={!connected || !value.trim() || submitting}
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
