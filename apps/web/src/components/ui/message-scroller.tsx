"use client"

/**
 * The chat transcript's scroll container, owned by ANA.
 *
 * This used to be `@shadcn/react/message-scroller`. That primitive carries a
 * lot ANA never asked for — per-message scroll anchors, an IntersectionObserver
 * visibility store, aligned scroll-to-message, a measuring spacer — and the one
 * thing it got wrong for us (`content-visibility` on items) had already been
 * patched out here. What ANA actually needs is small enough to own:
 *
 *   - open at the newest message, without the jump being visible,
 *   - keep following the bottom while the answer streams,
 *   - stop following the moment the reader scrolls up, and
 *   - offer a button back down while they are up there.
 *
 * The parts of the old API nothing used (anchors, the visibility hooks, the
 * spacer) are gone rather than reimplemented. `useMessageScroller` is kept
 * because it is a reasonable way for a screen to drive the scroller, and
 * `useMessageScrollerScrollable` because the same state draws the button.
 */

import * as React from "react"
import { cn } from "cn"

import { Button } from "@/components/ui/button"
import { ArrowDownIcon } from "lucide-react"

/** How far from an edge still counts as being at it, in pixels. */
const EDGE_THRESHOLD = 8
/** How long the viewport keeps `data-autoscrolling` after a scroll we started. */
const AUTOSCROLL_SETTLE_MS = 180

type ScrollDirection = "start" | "end"
type Scrollable = { start: boolean; end: boolean }

type ScrollerContext = {
  autoScroll: boolean
  /** Whether the first scroll to the newest message has happened yet. */
  pending: boolean
  scrollable: Scrollable
  viewportRef: React.RefObject<HTMLDivElement | null>
  contentRef: React.RefObject<HTMLDivElement | null>
  setViewport: (element: HTMLDivElement | null) => void
  setContent: (element: HTMLDivElement | null) => void
  scrollToEnd: (options?: { behavior?: ScrollBehavior }) => boolean
  scrollToStart: (options?: { behavior?: ScrollBehavior }) => boolean
  scrollToMessage: (messageId: string, options?: { behavior?: ScrollBehavior }) => boolean
  /** Called by the viewport on every scroll event, wheel, touch and key. */
  onViewportScroll: () => void
  onUserScrollIntent: () => void
  /** Called when the content's children or size change. */
  onContentChange: () => void
}

const MessageScrollerContext = React.createContext<ScrollerContext | null>(null)

function useScroller(component: string): ScrollerContext {
  const context = React.useContext(MessageScrollerContext)
  if (!context) {
    throw new Error(`${component} must be used within a MessageScrollerProvider.`)
  }
  return context
}

function MessageScrollerProvider({
  autoScroll = false,
  children,
}: {
  autoScroll?: boolean
  children?: React.ReactNode
}) {
  const viewportRef = React.useRef<HTMLDivElement | null>(null)
  const contentRef = React.useRef<HTMLDivElement | null>(null)
  // Following the bottom, or left where the reader put it. Held in a ref as
  // well as state because the scroll handler reads it before React re-renders.
  const followingRef = React.useRef(autoScroll)
  const itemCountRef = React.useRef(0)
  const settleTimerRef = React.useRef<number | null>(null)
  // True while a scroll this component started is still running, so the
  // scroll events it produces are not read as the reader taking over.
  const autoscrollingRef = React.useRef(false)

  const [pending, setPending] = React.useState(true)
  const [scrollable, setScrollable] = React.useState<Scrollable>({ start: false, end: false })

  const markAutoscrolling = React.useCallback(() => {
    const viewport = viewportRef.current
    if (!viewport) return
    autoscrollingRef.current = true
    viewport.setAttribute("data-autoscrolling", "")
    if (settleTimerRef.current !== null) window.clearTimeout(settleTimerRef.current)
    settleTimerRef.current = window.setTimeout(() => {
      settleTimerRef.current = null
      autoscrollingRef.current = false
      viewportRef.current?.removeAttribute("data-autoscrolling")
    }, AUTOSCROLL_SETTLE_MS)
  }, [])

  const syncScrollable = React.useCallback(() => {
    const viewport = viewportRef.current
    if (!viewport) return
    const distanceToEnd = viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight
    const next = {
      start: viewport.scrollTop > EDGE_THRESHOLD,
      // While the view is following the stream the button has nothing to
      // offer, so it is reported as "nothing below" even mid-animation.
      end: !followingRef.current && distanceToEnd > EDGE_THRESHOLD,
    }
    setScrollable((current) =>
      current.start === next.start && current.end === next.end ? current : next
    )
    const flags = [next.start && "start", next.end && "end"].filter(Boolean).join(" ")
    if (flags) viewport.setAttribute("data-scrollable", flags)
    else viewport.removeAttribute("data-scrollable")
  }, [])

  const scrollToEnd = React.useCallback(
    ({ behavior = "auto" }: { behavior?: ScrollBehavior } = {}) => {
      const viewport = viewportRef.current
      if (!viewport) return false
      followingRef.current = true
      markAutoscrolling()
      viewport.scrollTo({ top: viewport.scrollHeight - viewport.clientHeight, behavior })
      syncScrollable()
      return true
    },
    [markAutoscrolling, syncScrollable]
  )

  const scrollToStart = React.useCallback(
    ({ behavior = "auto" }: { behavior?: ScrollBehavior } = {}) => {
      const viewport = viewportRef.current
      if (!viewport) return false
      followingRef.current = false
      markAutoscrolling()
      viewport.scrollTo({ top: 0, behavior })
      syncScrollable()
      return true
    },
    [markAutoscrolling, syncScrollable]
  )

  const scrollToMessage = React.useCallback(
    (messageId: string, { behavior = "auto" }: { behavior?: ScrollBehavior } = {}) => {
      const viewport = viewportRef.current
      const content = contentRef.current
      if (!viewport || !content) return false
      const target = content.querySelector<HTMLElement>(
        `[data-message-id="${CSS.escape(messageId)}"]`
      )
      if (!target) return false
      followingRef.current = false
      markAutoscrolling()
      viewport.scrollTo({
        top: target.getBoundingClientRect().top - content.getBoundingClientRect().top,
        behavior,
      })
      syncScrollable()
      return true
    },
    [markAutoscrolling, syncScrollable]
  )

  const onViewportScroll = React.useCallback(() => {
    const viewport = viewportRef.current
    if (!viewport) return
    // The position decides, not how it was reached. A scroll that lands at
    // the bottom resumes following; one that leaves it hands the position
    // back to the reader — unless this component is the one scrolling.
    // Reading the wheel and the arrow keys instead meant any other way of
    // moving (a focus jump, a trackpad fling the page handled, an
    // anchor) left the view "following" while it sat halfway up.
    const distanceToEnd = viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight
    if (distanceToEnd <= EDGE_THRESHOLD) followingRef.current = autoScroll
    else if (!autoscrollingRef.current) followingRef.current = false
    syncScrollable()
  }, [autoScroll, syncScrollable])

  const onUserScrollIntent = React.useCallback(() => {
    // Wheel, touch or an arrow key: the reader is driving, so stop following
    // before the scroll even lands. The scroll handler above would reach the
    // same answer a frame later; this just avoids the frame.
    followingRef.current = false
    autoscrollingRef.current = false
  }, [])

  const onContentChange = React.useCallback(() => {
    const content = contentRef.current
    if (!content) return
    const count = content.childElementCount
    const previous = itemCountRef.current
    itemCountRef.current = count
    if (previous === 0 && count > 0) {
      // The first messages of a conversation, or a history load: open at the
      // newest one. `pending` hides the viewport until this has happened, so
      // the jump is never drawn.
      followingRef.current = autoScroll
      scrollToEnd()
      setPending(false)
      return
    }
    if (followingRef.current) {
      scrollToEnd()
      return
    }
    syncScrollable()
  }, [autoScroll, scrollToEnd, syncScrollable])

  const setViewport = React.useCallback(
    (element: HTMLDivElement | null) => {
      viewportRef.current = element
      if (element) syncScrollable()
    },
    [syncScrollable]
  )

  const setContent = React.useCallback((element: HTMLDivElement | null) => {
    contentRef.current = element
  }, [])

  React.useEffect(
    () => () => {
      if (settleTimerRef.current !== null) window.clearTimeout(settleTimerRef.current)
    },
    []
  )

  // An empty conversation has nothing to scroll to, so the viewport must not
  // stay hidden waiting for a first message that is not coming.
  React.useEffect(() => {
    if (pending && itemCountRef.current === 0) setPending(false)
  }, [pending])

  const value = React.useMemo<ScrollerContext>(
    () => ({
      autoScroll,
      pending,
      scrollable,
      viewportRef,
      contentRef,
      setViewport,
      setContent,
      scrollToEnd,
      scrollToStart,
      scrollToMessage,
      onViewportScroll,
      onUserScrollIntent,
      onContentChange,
    }),
    [
      autoScroll,
      pending,
      scrollable,
      setViewport,
      setContent,
      scrollToEnd,
      scrollToStart,
      scrollToMessage,
      onViewportScroll,
      onUserScrollIntent,
      onContentChange,
    ]
  )

  return (
    <MessageScrollerContext.Provider value={value}>{children}</MessageScrollerContext.Provider>
  )
}

/** Imperative scrolling for a screen that owns the conversation. */
function useMessageScroller() {
  const { scrollToEnd, scrollToMessage, scrollToStart } = useScroller("useMessageScroller")
  return React.useMemo(
    () => ({ scrollToEnd, scrollToMessage, scrollToStart }),
    [scrollToEnd, scrollToMessage, scrollToStart]
  )
}

/** Whether there is anything above or below what the viewport is showing. */
function useMessageScrollerScrollable(): Scrollable {
  return useScroller("useMessageScrollerScrollable").scrollable
}

function MessageScroller({ className, ...props }: React.ComponentProps<"div">) {
  const { pending } = useScroller("MessageScroller")
  return (
    <div
      data-slot="message-scroller"
      className={cn(
        "group/message-scroller relative flex size-full min-h-0 flex-col overflow-hidden",
        className
      )}
      {...(pending ? { "data-pending-scroll": "" } : null)}
      {...props}
    />
  )
}

function MessageScrollerViewport({
  className,
  onKeyDown,
  onScroll,
  onTouchMove,
  onWheel,
  ref,
  ...props
}: React.ComponentProps<"div">) {
  const { onUserScrollIntent, onViewportScroll, pending, setViewport } =
    useScroller("MessageScrollerViewport")

  const attach = React.useCallback(
    (element: HTMLDivElement | null) => {
      setViewport(element)
      if (typeof ref === "function") ref(element)
      else if (ref) ref.current = element
    },
    [ref, setViewport]
  )

  return (
    <div
      ref={attach}
      data-slot="message-scroller-viewport"
      role={props.role ?? "region"}
      aria-label={props["aria-label"] ?? "Messages"}
      tabIndex={props.tabIndex ?? 0}
      onScroll={(event) => {
        onViewportScroll()
        onScroll?.(event)
      }}
      onWheel={(event) => {
        onUserScrollIntent()
        onWheel?.(event)
      }}
      onTouchMove={(event) => {
        onUserScrollIntent()
        onTouchMove?.(event)
      }}
      onKeyDown={(event) => {
        if (SCROLL_KEYS.has(event.key)) onUserScrollIntent()
        onKeyDown?.(event)
      }}
      className={cn(
        "size-full min-h-0 min-w-0 scroll-fade-b scrollbar-thin scrollbar-gutter-stable overflow-y-auto overscroll-contain contain-content data-autoscrolling:scrollbar-thumb-transparent data-autoscrolling:scrollbar-track-transparent data-pending-scroll:invisible",
        className
      )}
      {...(pending ? { "data-pending-scroll": "" } : null)}
      {...props}
    />
  )
}

/** Keys that scroll a focused viewport, so pressing one hands it to the reader. */
const SCROLL_KEYS = new Set(["ArrowDown", "ArrowUp", "End", "Home", "PageDown", "PageUp", " "])

function MessageScrollerContent({ className, ref, ...props }: React.ComponentProps<"div">) {
  const { onContentChange, setContent } = useScroller("MessageScrollerContent")
  const element = React.useRef<HTMLDivElement | null>(null)

  const attach = React.useCallback(
    (node: HTMLDivElement | null) => {
      element.current = node
      setContent(node)
      if (typeof ref === "function") ref(node)
      else if (ref) ref.current = node
    },
    [ref, setContent]
  )

  // Two separate signals, and both matter: a new message is a child-list
  // change, while an answer growing token by token is only a size change.
  React.useLayoutEffect(() => {
    const node = element.current
    if (!node) return
    onContentChange()
    if (typeof MutationObserver === "undefined") return
    const observer = new MutationObserver(() => onContentChange())
    observer.observe(node, { childList: true })
    return () => observer.disconnect()
  }, [onContentChange])

  React.useEffect(() => {
    const node = element.current
    if (!node || typeof ResizeObserver === "undefined") return
    let frame = 0
    const observer = new ResizeObserver(() => {
      window.cancelAnimationFrame(frame)
      frame = window.requestAnimationFrame(() => onContentChange())
    })
    observer.observe(node)
    return () => {
      window.cancelAnimationFrame(frame)
      observer.disconnect()
    }
  }, [onContentChange])

  return (
    <div
      ref={attach}
      data-slot="message-scroller-content"
      role={props.role ?? "log"}
      aria-relevant={props["aria-relevant"] ?? "additions"}
      className={cn("flex h-max min-h-full flex-col gap-6", className)}
      {...props}
    />
  )
}

function MessageScrollerItem({
  className,
  messageId,
  ...props
}: React.ComponentProps<"div"> & { messageId?: string }) {
  return (
    <div
      data-slot="message-scroller-item"
      data-message-id={messageId}
      className={cn(
        // No content-visibility here (the primitive this replaced had it): it
        // let the browser skip rendering off-screen items and size them from a
        // hint instead, so every scroll computed against estimated heights and
        // landed short or past the message it was aiming at.
        "min-w-0 shrink-0",
        className
      )}
      {...props}
    />
  )
}

function MessageScrollerButton({
  behavior = "smooth",
  children,
  className,
  direction = "end",
  onClick,
  size = "icon-sm",
  variant = "secondary",
  ...props
}: React.ComponentProps<typeof Button> & {
  behavior?: ScrollBehavior
  direction?: ScrollDirection
}) {
  const { scrollable, scrollToEnd, scrollToStart } = useScroller("MessageScrollerButton")
  const active = direction === "start" ? scrollable.start : scrollable.end

  return (
    <Button
      data-slot="message-scroller-button"
      data-direction={direction}
      data-active={active ? "true" : "false"}
      type="button"
      variant={variant}
      size={size}
      inert={!active}
      tabIndex={active ? props.tabIndex : -1}
      onClick={(event) => {
        if (!active) return
        onClick?.(event)
        if (event.defaultPrevented) return
        event.currentTarget.blur()
        if (direction === "start") scrollToStart({ behavior })
        else scrollToEnd({ behavior })
      }}
      className={cn(
        "absolute inset-s-1/2 -translate-x-1/2 border-border bg-background text-foreground transition-[translate,scale,opacity] duration-200 hover:bg-muted hover:text-foreground data-[active=false]:pointer-events-none data-[active=false]:scale-95 data-[active=false]:opacity-0 data-[active=false]:duration-400 data-[active=false]:ease-[cubic-bezier(0.7,0,0.84,0)] data-[active=true]:translate-y-0 data-[active=true]:scale-100 data-[active=true]:opacity-100 data-[active=true]:ease-[cubic-bezier(0.23,1,0.32,1)] data-[direction=end]:bottom-4 data-[direction=end]:data-[active=false]:translate-y-full data-[direction=start]:top-4 data-[direction=start]:data-[active=false]:-translate-y-full rtl:translate-x-1/2 data-[direction=start]:[&_svg]:rotate-180",
        className
      )}
      {...props}
    >
      {children ?? (
        <>
          <ArrowDownIcon />
          <span className="sr-only">
            {direction === "end" ? "Scroll to end" : "Scroll to start"}
          </span>
        </>
      )}
    </Button>
  )
}

export {
  MessageScrollerProvider,
  MessageScroller,
  MessageScrollerViewport,
  MessageScrollerContent,
  MessageScrollerItem,
  MessageScrollerButton,
  useMessageScroller,
  useMessageScrollerScrollable,
}
