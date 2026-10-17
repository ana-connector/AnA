"use client";

import { useState, useSyncExternalStore, type FormEvent } from "react";
import { ExternalLinkIcon, RotateCwIcon } from "lucide-react";
import { useTabs } from "@/components/tabs";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyTitle,
} from "@/components/ui/empty";
import { browserHref, canPreview, parseUrl, previewLabel } from "@/lib/preview";

/** One browser tab: an address bar and the page it points at.
 *
 *  Mounted under a key of its address, so pointing the tab somewhere else
 *  starts the address bar and the frame over rather than leaving the old
 *  address typed in the bar.
 *
 *  The frame is sandboxed, and a page gets its own origin's privileges and
 *  nothing of ours. ANA's own origin is refused outright: a frame of the same
 *  origin can reach straight out of the sandbox. */
/** The origin is a client-only fact, and whether a page may be framed
 *  depends on it. Read through a store rather than from `window` during the
 *  render: a render that answers differently on the server and on the client
 *  is a hydration mismatch, and React then throws the whole tree away and
 *  builds it again. The server's answer is "not yet", so the frame appears
 *  once the page is live and the server never promises one it cannot keep. */
const noSubscribe = () => () => {};
const readOrigin = () => window.location.origin;
const noOrigin = () => null;

export function BrowserView({ target }: { target: string }) {
  const { openHere } = useTabs();
  const [draft, setDraft] = useState(target);
  // Bumped to reload: an iframe reloads by being replaced, and its content is
  // another origin's, so there is no reaching in to call location.reload().
  const [reloads, setReloads] = useState(0);
  const url = parseUrl(target);
  const origin = useSyncExternalStore(noSubscribe, readOrigin, noOrigin);
  const allowed = target !== "" && origin !== null && canPreview(target, origin);

  const go = (event: FormEvent) => {
    event.preventDefault();
    const next = parseUrl(draft);
    if (!next) return;
    // Typing an address moves this tab, the way an address bar does; it does
    // not leave the old page open in a tab of its own.
    openHere(browserHref(next.href), { title: previewLabel(next.href) });
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <form
        onSubmit={go}
        className="flex shrink-0 items-center gap-1.5 border-b border-border px-2 py-1.5"
      >
        <Input
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          placeholder="localhost:3000"
          aria-label="주소"
          data-slot="browser-address"
          className="h-7 flex-1 font-mono text-xs"
          spellCheck={false}
        />
        <Button type="submit" size="sm" className="h-7">
          열기
        </Button>
        <Button
          type="button"
          size="sm"
          variant="ghost"
          className="h-7"
          disabled={!allowed}
          aria-label="새로고침"
          onClick={() => setReloads((count) => count + 1)}
        >
          <RotateCwIcon className="size-3.5" />
        </Button>
        <Button
          type="button"
          size="sm"
          variant="ghost"
          className="h-7"
          disabled={!url}
          aria-label="새 창에서 열기"
          onClick={() => url && window.open(url.href, "_blank", "noreferrer")}
        >
          <ExternalLinkIcon className="size-3.5" />
        </Button>
      </form>

      {allowed && url ? (
        <iframe
          key={`${url.href}#${reloads}`}
          src={url.href}
          title={previewLabel(url.href)}
          data-slot="browser-frame"
          className="min-h-0 w-full flex-1 bg-white"
          // allow-same-origin gives the framed page its own origin's
          // storage, which previewing your own dev server needs. It is only
          // safe because ANA's own origin is refused above: a frame that
          // shares our origin can reach straight out of the sandbox.
          sandbox="allow-same-origin allow-scripts allow-forms allow-popups allow-modals"
        />
      ) : (
        <Empty className="flex-1">
          <EmptyHeader>
            <EmptyTitle>
              {target === "" ? "주소를 입력하세요" : origin === null ? "여는 중" : "열 수 없는 주소"}
            </EmptyTitle>
            <EmptyDescription>
              {target === ""
                ? "띄워 둔 개발 서버 주소를 적으면 여기에서 바로 봅니다. 예: localhost:3000"
                : "http 또는 https 주소만 열 수 있고, AnA 자신은 띄울 수 없습니다."}
            </EmptyDescription>
          </EmptyHeader>
        </Empty>
      )}
    </div>
  );
}
