"use client";

import { useEffect, useLayoutEffect, useRef, useState, type FormEvent } from "react";
import {
  ExternalLinkIcon,
  MaximizeIcon,
  MonitorIcon,
  RotateCwIcon,
  SmartphoneIcon,
  TabletIcon,
} from "lucide-react";
import { useTabs } from "@/components/tabs";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyTitle,
} from "@/components/ui/empty";
import { fitScale, scaleLabel } from "@/lib/fit";
import {
  browserHref,
  canPreview,
  isOwnOrigin,
  parseUrl,
  previewLabel,
} from "@/lib/preview";
import { cn } from "@/lib/utils";

/** Same-origin frames expose `<title>`; cross-origin ones throw. */
function frameTitle(frame: HTMLIFrameElement | null): string | null {
  try {
    const title = frame?.contentDocument?.title?.trim();
    return title || null;
  } catch {
    return null;
  }
}

/** Ask the previewed server itself for its HTML and parse `<title>`.
 *  No ANA hop — if the server does not allow CORS, the tab keeps host:port. */
async function titleFromTarget(href: string): Promise<string | null> {
  try {
    const response = await fetch(href, {
      method: "GET",
      mode: "cors",
      credentials: "omit",
      headers: { Accept: "text/html,application/xhtml+xml" },
    });
    if (!response.ok) return null;
    const text = await response.text();
    const match = /<title[^>]*>([\s\S]*?)<\/title>/i.exec(text);
    if (!match) return null;
    const title = match[1].replace(/\s+/g, " ").trim();
    return title || null;
  } catch {
    return null;
  }
}

/** One browser tab: an address bar and the page it points at.
 *
 *  Mounted under a key of its address, so pointing the tab somewhere else
 *  starts the address bar and the frame over rather than leaving the old
 *  address typed in the bar.
 *
 *  The frame is sandboxed; a page keeps its own origin's privileges, which
 *  is what a preview of your own dev server needs. http and https only —
 *  `javascript:` and `data:` in an iframe would run in our own page.
 *
 *  The frame can also be given a size, so the page you are building can be
 *  looked at at phone or tablet width without resizing the window around it
 *  — the thing you actually want to check when an agent says it fixed the
 *  layout. */
/** The sizes worth a button each. A preview usually just fills the pane;
 *  the other three are the widths layouts actually break at.
 *
 *  A chosen size is the viewport the framed page gets, not the box it is
 *  drawn in: a frame wider than the pane is scaled down to fit and
 *  letterboxed rather than put behind a horizontal scrollbar (see
 *  `@/lib/fit`). */
const SIZES: { label: string; icon: typeof MaximizeIcon; size: [number, number] | null }[] = [
  { label: "꽉 채움", icon: MaximizeIcon, size: null },
  { label: "모바일", icon: SmartphoneIcon, size: [390, 844] },
  { label: "태블릿", icon: TabletIcon, size: [768, 1024] },
  { label: "노트북", icon: MonitorIcon, size: [1280, 800] },
];

export function BrowserView({
  target,
  onDocumentTitle,
}: {
  target: string;
  /** Called with the framed page's `<title>` when readable (same-origin
   *  frame, or a CORS-allowed fetch to the target). Otherwise the tab keeps
   *  the host:port label — ANA is not a proxy. */
  onDocumentTitle?: (title: string) => void;
}) {
  const { openHere } = useTabs();
  const [draft, setDraft] = useState(target);
  // Bumped to reload: an iframe reloads by being replaced, and its content is
  // another origin's, so there is no reaching in to call location.reload().
  const [reloads, setReloads] = useState(0);
  // null means the frame fills the pane. A number pair is a viewport to
  // check the page at, scrolled inside the pane when it does not fit.
  const [size, setSize] = useState<[number, number] | null>(null);
  const frameRef = useRef<HTMLIFrameElement>(null);
  // The room a sized frame has to fit into, measured rather than assumed:
  // this pane is resized by dragging a split, not only by the window.
  const stageRef = useRef<HTMLDivElement>(null);
  const [stage, setStage] = useState({ width: 0, height: 0 });
  const url = parseUrl(target);
  const allowed = target !== "" && canPreview(target);
  const framingSelf = Boolean(url && isOwnOrigin(url.href));

  // Layout effect, so the first paint already has the scale: measuring in a
  // plain effect showed the frame at full size for a frame or two, which
  // reads as a flash of the overflow this is here to remove.
  useLayoutEffect(() => {
    const stageNode = stageRef.current;
    if (!stageNode) return;
    const measure = () => {
      const box = stageNode.getBoundingClientRect();
      setStage({ width: box.width, height: box.height });
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(stageNode);
    return () => observer.disconnect();
  }, [size, target, framingSelf, allowed]);

  const scale = size ? fitScale(size, stage) : 1;
  const zoom = size ? scaleLabel(scale) : null;

  const publishTitle = () => {
    const title = frameTitle(frameRef.current);
    if (title) onDocumentTitle?.(title);
  };

  // Same-origin frame: read `<title>` (and watch SPA changes). Otherwise ask
  // the previewed server directly — never ANA.
  useEffect(() => {
    if (!onDocumentTitle || !url || framingSelf) return;
    const frame = frameRef.current;
    let observer: MutationObserver | null = null;
    let cancelled = false;

    const fromTarget = async () => {
      const title = await titleFromTarget(url.href);
      if (title && !cancelled) onDocumentTitle(title);
    };

    const watch = () => {
      observer?.disconnect();
      observer = null;
      const local = frameTitle(frame);
      if (local) {
        onDocumentTitle(local);
        try {
          const doc = frame?.contentDocument;
          if (!doc?.head) return;
          observer = new MutationObserver(() => {
            const next = frameTitle(frame);
            if (next) onDocumentTitle(next);
          });
          observer.observe(doc.head, { subtree: true, childList: true, characterData: true });
        } catch {
          void fromTarget();
        }
        return;
      }
      void fromTarget();
    };

    frame?.addEventListener("load", watch);
    watch();
    return () => {
      cancelled = true;
      frame?.removeEventListener("load", watch);
      observer?.disconnect();
    };
  }, [onDocumentTitle, target, reloads, url?.href, framingSelf]);

  const go = (event: FormEvent) => {
    event.preventDefault();
    const next = parseUrl(draft);
    if (!next) return;
    // Typing an address moves this tab, the way an address bar does; it does
    // not leave the old page open in a tab of its own. Title starts as the
    // host label until the frame's `<title>` loads (when readable).
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

      {/* The size the page is being looked at, which is the question a
          preview is usually opened to answer. */}
      <div
        data-slot="browser-sizes"
        className="flex shrink-0 items-center gap-1 border-b border-border px-2 py-1 text-xs"
      >
        {SIZES.map((preset) => {
          const chosen = preset.size
            ? size !== null && size[0] === preset.size[0] && size[1] === preset.size[1]
            : size === null;
          return (
            <button
              key={preset.label}
              type="button"
              data-slot="browser-size"
              data-size={preset.size ? preset.size.join("x") : "fill"}
              data-active={chosen}
              aria-pressed={chosen}
              title={preset.size ? `${preset.label} ${preset.size[0]}×${preset.size[1]}` : preset.label}
              onClick={() => setSize(preset.size)}
              className={cn(
                "flex size-6 items-center justify-center rounded-md",
                chosen
                  ? "bg-muted text-foreground"
                  : "text-muted-foreground hover:bg-muted hover:text-foreground"
              )}
            >
              <preset.icon className="size-3.5" strokeWidth={1.5} />
            </button>
          );
        })}

        <div className="ml-1 flex items-center gap-1 text-muted-foreground">
          <input
            type="number"
            aria-label="너비"
            data-slot="browser-width"
            value={size?.[0] ?? ""}
            placeholder="자동"
            min={200}
            max={4000}
            onChange={(event) => {
              const width = Number(event.target.value);
              setSize(width > 0 ? [width, size?.[1] ?? 844] : null);
            }}
            className="h-6 w-16 rounded-md border border-border bg-transparent px-1.5 text-center tabular-nums outline-none focus-visible:border-ring"
          />
          <span aria-hidden>×</span>
          <input
            type="number"
            aria-label="높이"
            data-slot="browser-height"
            value={size?.[1] ?? ""}
            placeholder="자동"
            min={200}
            max={4000}
            onChange={(event) => {
              const height = Number(event.target.value);
              setSize(height > 0 ? [size?.[0] ?? 390, height] : null);
            }}
            className="h-6 w-16 rounded-md border border-border bg-transparent px-1.5 text-center tabular-nums outline-none focus-visible:border-ring"
          />
          {zoom && (
            // The frame is smaller than the size it reports, and saying so is
            // the difference between "my layout is tiny" and "this is 1280
            // drawn at 72%".
            <span data-slot="browser-zoom" className="tabular-nums" title="창에 맞춰 축소된 비율">
              {zoom}
            </span>
          )}
          {size && (
            <button
              type="button"
              aria-label="가로 세로 바꾸기"
              data-slot="browser-rotate"
              onClick={() => setSize([size[1], size[0]])}
              className="flex size-6 items-center justify-center rounded-md hover:bg-muted hover:text-foreground"
            >
              <RotateCwIcon className="size-3.5" strokeWidth={1.5} />
            </button>
          )}
        </div>
      </div>

      {framingSelf && url ? (
        <Empty className="flex-1">
          <EmptyHeader>
            <EmptyTitle>ANA 자신은 미리볼 수 없습니다</EmptyTitle>
            <EmptyDescription>
              브라우저 탭은 개발 서버처럼 ANA 바깥 주소를 보는 용도입니다. ANA를
              보려면{" "}
              <button
                type="button"
                className="underline underline-offset-2 hover:text-foreground"
                onClick={() => window.open(url.href, "_blank", "noreferrer")}
              >
                새 창에서 열기
              </button>
              .
            </EmptyDescription>
          </EmptyHeader>
        </Empty>
      ) : allowed && url ? (
        // Sized, the frame is centred on the pane's own background and drawn
        // at whatever scale fits — letterboxed, never scrolled sideways.
        // Filling, it simply is the pane.
        <div
          ref={stageRef}
          data-slot="browser-stage"
          className={cn(
            "flex min-h-0 flex-1",
            size && "items-center justify-center overflow-hidden bg-muted/40 p-3"
          )}
        >
        {size ? (
          // Two boxes on purpose: the outer one is the frame's *drawn* size,
          // which is what centres it, while the inner frame keeps the
          // viewport it was given and is scaled into that space. One box
          // cannot do both — a transform does not change layout size, so the
          // frame would be centred as if it were still 1280 wide.
          <div
            data-slot="browser-frame-box"
            style={{ width: size[0] * scale, height: size[1] * scale }}
            className="shrink-0 overflow-hidden rounded-md border border-border shadow-sm"
          >
            <iframe
              key={`${url.href}#${reloads}`}
              ref={frameRef}
              src={url.href}
              title={previewLabel(url.href)}
              data-slot="browser-frame"
              style={{
                width: size[0],
                height: size[1],
                transform: `scale(${scale})`,
                transformOrigin: "top left",
              }}
              className="block border-0 bg-white"
              onLoad={publishTitle}
              // allow-same-origin gives the framed page its own origin's
              // storage and sockets, which previewing your own dev server needs.
              sandbox="allow-same-origin allow-scripts allow-forms allow-popups allow-modals"
            />
          </div>
        ) : (
          <iframe
            key={`${url.href}#${reloads}`}
            ref={frameRef}
            src={url.href}
            title={previewLabel(url.href)}
            data-slot="browser-frame"
            className="min-h-0 w-full flex-1 bg-white"
            onLoad={publishTitle}
            sandbox="allow-same-origin allow-scripts allow-forms allow-popups allow-modals"
          />
        )}
        </div>
      ) : (
        <Empty className="flex-1">
          <EmptyHeader>
            <EmptyTitle>{target === "" ? "주소를 입력하세요" : "열 수 없는 주소"}</EmptyTitle>
            <EmptyDescription>
              {target === ""
                ? "띄워 둔 개발 서버 주소를 적으면 여기에서 바로 봅니다. 예: localhost:3000"
                : "http 또는 https 주소만 열 수 있습니다."}
            </EmptyDescription>
          </EmptyHeader>
        </Empty>
      )}
    </div>
  );
}
