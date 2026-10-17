"use client";

import { useState, type FormEvent } from "react";
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
import { browserHref, canPreview, parseUrl, previewLabel } from "@/lib/preview";
import { cn } from "@/lib/utils";

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
 *  the other three are the widths layouts actually break at. */
const SIZES: { label: string; icon: typeof MaximizeIcon; size: [number, number] | null }[] = [
  { label: "꽉 채움", icon: MaximizeIcon, size: null },
  { label: "모바일", icon: SmartphoneIcon, size: [390, 844] },
  { label: "태블릿", icon: TabletIcon, size: [768, 1024] },
  { label: "노트북", icon: MonitorIcon, size: [1280, 800] },
];

export function BrowserView({ target }: { target: string }) {
  const { openHere } = useTabs();
  const [draft, setDraft] = useState(target);
  // Bumped to reload: an iframe reloads by being replaced, and its content is
  // another origin's, so there is no reaching in to call location.reload().
  const [reloads, setReloads] = useState(0);
  // null means the frame fills the pane. A number pair is a viewport to
  // check the page at, scrolled inside the pane when it does not fit.
  const [size, setSize] = useState<[number, number] | null>(null);
  const url = parseUrl(target);
  const allowed = target !== "" && canPreview(target);

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

      {allowed && url ? (
        // Sized, the frame sits on the pane's own background with room to
        // scroll around it; filling, it simply is the pane.
        <div
          className={cn(
            "flex min-h-0 flex-1",
            size && "justify-center overflow-auto bg-muted/40 p-3"
          )}
        >
        <iframe
          key={`${url.href}#${reloads}`}
          src={url.href}
          title={previewLabel(url.href)}
          data-slot="browser-frame"
          style={size ? { width: size[0], height: size[1] } : undefined}
          className={cn(
            "bg-white",
            size
              ? "h-auto w-auto shrink-0 self-start rounded-md border border-border shadow-sm"
              : "min-h-0 w-full flex-1"
          )}
          // allow-same-origin gives the framed page its own origin's
          // storage and sockets, which previewing your own dev server needs.
          // ANA's own pages are allowed in here too, so a frame can in
          // principle script this one — everything framed is something on
          // the user's own machines, reached from their own browser.
          sandbox="allow-same-origin allow-scripts allow-forms allow-popups allow-modals"
        />
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
