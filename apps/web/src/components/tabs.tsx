"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useSyncExternalStore,
  type ReactNode,
} from "react";
import { usePathname, useRouter } from "next/navigation";
import { usePaneMode } from "@/hooks/use-pane-mode";
import { BROWSER_ROUTE, previewLabel, previewTarget } from "@/lib/preview";
import {
  adoptIds,
  closeTab as closeTabIn,
  findLeaf,
  leaf,
  leaves,
  mapNode,
  split as splitPaneTree,
  withTab,
  type DropSide,
  type Leaf,
  type PaneNode,
} from "@/lib/panes";

/** One open tab. The route it shows is its identity: two tabs on the same
 *  page would be two views of the same thing, and closing one of them would
 *  be a guess at which. */
export type Tab = {
  href: string;
  title: string;
  /** The workspace's own icon: an emoji, or a small image as a `data:` URL. */
  icon?: string;
  /** The shortened folder, shown after the name on the tab itself. Two
   *  workspaces are often called the same thing in different trees, and the
   *  folder is what tells them apart. */
  detail?: string;
};

/** What a caller knows about the page it is opening. Everything is optional:
 *  a tab names itself from its address until something better arrives. */
export type TabInfo = { title?: string; icon?: string; detail?: string };

const START_HREF = "/";
const START: Tab = { href: START_HREF, title: "시작" };

/** postMessage type from a pane iframe (`?pane=1`) naming its tab in the
 *  outer window. Split panes are iframe-only, so the page cannot call
 *  `setTitle` on the shell's store directly. */
export const TAB_TITLE_MESSAGE = "ana:set-tab-title";
const STORAGE_KEY = "ana_tabs";

/** Pages that belong to the installation rather than to a workspace. A tab
 *  opened by a direct URL needs a name before the page itself can say one. */
const STATIC_TITLES: Record<string, string> = {
  "/": "시작",
  "/settings": "설정",
  "/tools": "도구 로그",
  "/secrets": "시크릿",
  "/automation": "자동화",
};

/** The browser-only harness has no shell and no tab bar; it must not leave a
 *  tab behind either. */
const UNTABBED = ["/ui-harness"];

function titleFor(href: string): string {
  if (STATIC_TITLES[href]) return STATIC_TITLES[href];
  // A browser tab carries its address in its path, so a tab restored from
  // storage or opened from a bookmark can name itself without asking.
  if (href === BROWSER_ROUTE) return "브라우저";
  if (href.startsWith(`${BROWSER_ROUTE}/`)) {
    return previewLabel(previewTarget(href.slice(BROWSER_ROUTE.length + 1)));
  }
  const last = href.split("/").filter(Boolean).pop() ?? href;
  try {
    return decodeURIComponent(last);
  } catch {
    return last;
  }
}

/* -------------------------------------------------------------------------
 * The open tabs outlive every page, so they are held outside React in a small
 * store the components read through `useSyncExternalStore`. Holding them in a
 * provider's state instead meant restoring the saved list was a setState
 * inside an effect — a second render on every load, and either a hydration
 * mismatch or a visible flash. A store also hands React a server snapshot to
 * hydrate against, which is the default view.
 *
 * Two things are stored. `tabs` is the registry: what each open page is
 * called and what it looks like, one entry per address. `layout` is the
 * window: a tree of panes, each with its own list of those addresses and
 * one of them showing. Splitting the screen is how a second list comes
 * about, which is why the tabs live in the panes and not in one bar.
 * ---------------------------------------------------------------------- */

const SERVER_TABS: Tab[] = [START];
const SERVER_LAYOUT: Leaf = { kind: "leaf", id: "p0", tabs: [START_HREF], active: START_HREF };
let tabs: Tab[] = SERVER_TABS;
let layout: PaneNode = SERVER_LAYOUT;
/** The pane the router is driving: whatever it shows is the address bar's
 *  page, and the sidebar acts on that pane rather than on the others. */
let focused: string = SERVER_LAYOUT.id;
let restored = false;
const listeners = new Set<() => void>();

function announce() {
  for (const listener of listeners) listener();
}

/** A pane is the same app in an iframe, and an iframe shares this tab's
 *  session storage. Left to run, it would restore the window's tabs into
 *  itself, add its own address to them and write the result back over the
 *  window's own view. So in a pane the store keeps to itself: it holds
 *  whatever this document needs and never reads or writes the record. */
let paneChecked = false;
let inPane = false;
function isPane(): boolean {
  if (!paneChecked && typeof window !== "undefined") {
    paneChecked = true;
    inPane = new URLSearchParams(window.location.search).get("pane") === "1";
  }
  return inPane;
}

function save() {
  if (isPane()) return;
  try {
    window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify({ tabs, layout, focused }));
  } catch {
    // A browser with storage switched off keeps its tabs for this page only.
  }
}

/** Every address open in any pane. The registry is trimmed to it, so a page
 *  nobody is showing stops being carried around. */
function openHrefs(node: PaneNode): Set<string> {
  return new Set(leaves(node).flatMap((pane) => pane.tabs));
}

function commit(nextLayout: PaneNode | null, focus?: string) {
  layout = nextLayout ?? leaf(START_HREF);
  const all = leaves(layout);
  focused =
    focus && all.some((pane) => pane.id === focus)
      ? focus
      : all.some((pane) => pane.id === focused)
        ? focused
        : all[0].id;
  const open = openHrefs(layout);
  tabs = tabs.filter((tab) => open.has(tab.href));
  for (const href of open) {
    if (!tabs.some((tab) => tab.href === href)) tabs = [...tabs, { href, title: titleFor(href) }];
  }
  save();
  announce();
}

/** Records what a page is called, leaving the window alone. */
function describe(next: Tab[]) {
  if (
    next.length === tabs.length &&
    next.every(
      (tab, index) =>
        tab.href === tabs[index].href &&
        tab.title === tabs[index].title &&
        (tab.icon ?? "") === (tabs[index].icon ?? "") &&
        (tab.detail ?? "") === (tabs[index].detail ?? "")
    )
  ) {
    return;
  }
  tabs = next;
  save();
  announce();
}

function register(href: string, info?: TabInfo) {
  const existing = tabs.find((tab) => tab.href === href);
  const entry: Tab = { href, title: info?.title || existing?.title || titleFor(href), ...info };
  describe(existing ? tabs.map((tab) => (tab.href === href ? { ...tab, ...entry } : tab)) : [...tabs, entry]);
}

/** Prefer the current pane when a sole tab was split into two views. */
function paneHolding(href: string): Leaf | null {
  const current = findLeaf(layout, focused);
  if (current?.tabs.includes(href)) return current;
  return leaves(layout).find((pane) => pane.tabs.includes(href)) ?? null;
}

/** Reads the saved view once, on the client, after hydration has committed.
 *  A record written before panes owned their tabs — or one naming a page
 *  that is no longer open — is dropped rather than half-applied. */
function restore() {
  restored = true;
  if (isPane()) return;
  try {
    const stored = window.sessionStorage.getItem(STORAGE_KEY);
    if (!stored) return;
    const parsed: unknown = JSON.parse(stored);
    const savedTabs = (parsed as { tabs?: unknown })?.tabs;
    if (!Array.isArray(savedTabs)) return;
    const valid = savedTabs.filter(
      (tab): tab is Tab =>
        typeof tab === "object" &&
        tab !== null &&
        typeof (tab as Tab).href === "string" &&
        typeof (tab as Tab).title === "string"
    );
    if (!valid.length) return;
    tabs = valid;

    const savedLayout = (parsed as { layout?: PaneNode })?.layout;
    const known = new Set(valid.map((tab) => tab.href));
    const usable =
      savedLayout &&
      leaves(savedLayout).every(
        (pane) =>
          Array.isArray(pane.tabs) &&
          pane.tabs.length > 0 &&
          pane.tabs.every((href) => known.has(href)) &&
          pane.tabs.includes(pane.active)
      );
    if (usable && savedLayout) {
      adoptIds(savedLayout);
      const focus = (parsed as { focused?: string })?.focused;
      commit(savedLayout, focus && findLeaf(savedLayout, focus) ? focus : undefined);
    } else {
      commit(leaf(valid[0].href, valid.map((tab) => tab.href)));
    }
  } catch {
    // Unreadable storage is no reason to lose the page.
  }
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  if (!restored) restore();
  return () => {
    listeners.delete(listener);
  };
}

const readTabs = () => tabs;
const readServerTabs = () => SERVER_TABS;
const readLayout = () => layout;
const readServerLayout = (): PaneNode => SERVER_LAYOUT;
const readFocused = () => focused;
const readServerFocused = () => SERVER_LAYOUT.id;

type TabsValue = {
  /** What every open page is called. The order a strip shows them in is the
   *  pane's, not this one's. */
  tabs: Tab[];
  /** The page on screen in the focused pane. Derived from the address rather
   *  than stored: the active tab *is* whatever the router is showing, so
   *  there is no second copy of that fact to fall out of step. */
  activeHref: string;
  /** Sidebar navigation: the page opens in the focused pane, beside whatever
   *  else is open there. */
  open: (href: string, info?: TabInfo) => void;
  /** Navigation from inside a page: the tab you are in becomes the new page,
   *  the way a start page hands its own tab over to what you picked on it. */
  openHere: (href: string, info?: TabInfo) => void;
  /** Closes a page in the pane that is showing it. The pane goes with it
   *  when that was its last tab. */
  close: (href: string, paneId?: string) => void;
  /** Raises a tab inside its own pane, and makes that pane the current one. */
  selectTab: (paneId: string, href: string) => void;
  /** The page naming itself once it knows what it is. */
  setTitle: (href: string, title: string) => void;
  /** The panes on screen, and which of them the router is driving. */
  panes: PaneNode;
  focusedPane: string;
  /** A tab dropped on a pane: on an edge it splits that pane and moves the
   *  tab into the new one, in the middle it joins that pane's strip. */
  dropTab: (paneId: string, side: DropSide, href: string, from?: string) => void;
  /** Clicking into a pane makes it the one the sidebar and the router act on. */
  focusPane: (paneId: string) => void;
  resizePane: (splitId: string, ratio: number) => void;
  /** The workspace list, whenever whoever is holding it reloads it. A tab for
   *  a workspace is named and iconed from here, so opening one by its address
   *  does not have to wait for its conversation to connect before the tab
   *  stops reading as a raw id. */
  describeWorkspaces: (
    workspaces: { id: string; name: string; icon?: string; detail?: string }[]
  ) => void;
};

const TabsContext = createContext<TabsValue | null>(null);

export function useTabs(): TabsValue {
  const value = useContext(TabsContext);
  if (!value) throw new Error("useTabs must be used inside <TabsProvider>");
  return value;
}

export function TabsProvider({ children }: { children: ReactNode }) {
  const router = useRouter();
  const pathname = usePathname() ?? START_HREF;
  const openTabs = useSyncExternalStore(subscribe, readTabs, readServerTabs);
  const panes = useSyncExternalStore(subscribe, readLayout, readServerLayout);
  const focusedPane = useSyncExternalStore(subscribe, readFocused, readServerFocused);
  // Where we have asked the router to go. Until the address catches up, the
  // view already shows the destination, and the effect below must not read
  // the page we are leaving as one that went missing and put it back.
  const pending = useRef<string | null>(null);

  const navigate = useCallback(
    (href: string) => {
      pending.current = href;
      router.push(href);
    },
    [router]
  );

  const open = useCallback(
    (href: string, info?: TabInfo) => {
      register(href, info);
      const pane = paneHolding(href) ?? findLeaf(layout, focused) ?? leaves(layout)[0];
      commit(mapNode(layout, pane.id, (found) => withTab(found as Leaf, href)), pane.id);
      navigate(href);
    },
    [navigate]
  );

  const openHere = useCallback(
    (href: string, info?: TabInfo) => {
      register(href, info);
      const elsewhere = paneHolding(href);
      if (elsewhere) {
        // Already open: go to it rather than opening a second way in.
        commit(mapNode(layout, elsewhere.id, (found) => ({ ...(found as Leaf), active: href })), elsewhere.id);
        navigate(href);
        return;
      }
      const pane = findLeaf(layout, focused) ?? leaves(layout)[0];
      // In place of the tab you are on: the start page hands its own tab
      // over to what you picked on it rather than piling up beside it.
      const tabsAfter = pane.tabs.map((tab) => (tab === pane.active ? href : tab));
      commit(
        mapNode(layout, pane.id, (found) => ({ ...(found as Leaf), tabs: tabsAfter, active: href })),
        pane.id
      );
      navigate(href);
    },
    [navigate]
  );

  const close = useCallback(
    (href: string, paneId?: string) => {
      const pane = (paneId && findLeaf(layout, paneId)) || paneHolding(href);
      if (!pane) return;
      const next = closeTabIn(layout, pane.id, href);
      const stillFocused = next && findLeaf(next, focused) ? focused : undefined;
      commit(next, stillFocused);
      const landing = findLeaf(layout, focused);
      if (landing && landing.active !== pathname) navigate(landing.active);
    },
    [navigate, pathname]
  );

  const selectTab = useCallback(
    (paneId: string, href: string) => {
      const pane = findLeaf(layout, paneId);
      if (!pane) return;
      const alreadyFocused = paneId === focused && pane.active === href;
      commit(mapNode(layout, paneId, (found) => ({ ...(found as Leaf), active: href })), paneId);
      // Same tab already on screen: do not push the router again — that used
      // to remount the focused document for a no-op click.
      if (alreadyFocused) return;
      // A tab in another pane: raising it focuses that pane. Navigate only
      // when the address bar would actually move.
      if (href !== pathname) navigate(href);
    },
    [navigate, pathname]
  );

  const setTitle = useCallback((href: string, title: string) => {
    if (!title) return;
    if (!tabs.some((tab) => tab.href === href && tab.title !== title)) return;
    describe(tabs.map((tab) => (tab.href === href ? { ...tab, title } : tab)));
  }, []);

  // Pane iframes name their tabs through postMessage — the shell's store is
  // not the one inside `?pane=1`.
  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      if (event.origin !== window.location.origin) return;
      const data = event.data;
      if (
        !data ||
        data.type !== TAB_TITLE_MESSAGE ||
        typeof data.href !== "string" ||
        typeof data.title !== "string"
      ) {
        return;
      }
      setTitle(data.href, data.title);
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [setTitle]);

  const dropTab = useCallback(
    (paneId: string, side: DropSide, href: string, from?: string) => {
      const source = (from && findLeaf(layout, from)) || paneHolding(href);
      if (source?.id === paneId && side === "center") {
        // Dropped back where it came from: just raise it.
        commit(mapNode(layout, paneId, (found) => ({ ...(found as Leaf), active: href })), paneId);
        navigate(href);
        return;
      }
      const { layout: next, focus } = splitPaneTree(layout, paneId, side, href, source?.id);
      commit(next, focus);
      if (href !== pathname) navigate(href);
    },
    [navigate, pathname]
  );

  const focusPane = useCallback(
    (paneId: string) => {
      const pane = findLeaf(layout, paneId);
      if (!pane || paneId === focused) return;
      commit(layout, paneId);
      // The address bar follows the focused pane: it is the one the sidebar,
      // the back button and a new tab all act on.
      if (pane.active !== pathname) navigate(pane.active);
    },
    [navigate, pathname]
  );

  const resizePane = useCallback((splitId: string, ratio: number) => {
    commit(
      mapNode(layout, splitId, (found) =>
        found.kind === "split" ? { ...found, ratio: Math.min(0.85, Math.max(0.15, ratio)) } : found
      ),
      focused
    );
  }, []);

  const describeWorkspaces = useCallback(
    (workspaces: { id: string; name: string; icon?: string; detail?: string }[]) => {
      const known = new Map(
        workspaces.map((workspace) => [`/${encodeURIComponent(workspace.id)}`, workspace])
      );
      describe(
        tabs.map((tab) => {
          const workspace = known.get(tab.href);
          if (!workspace) return tab;
          return {
            ...tab,
            title: workspace.name,
            icon: workspace.icon ?? "",
            detail: workspace.detail ?? "",
          };
        })
      );
    },
    []
  );

  // Whatever the address bar says must be open in the focused pane, however
  // it got there — a bookmark, the back button, a redirect, a link we do not
  // own. The router drives exactly one pane.
  useEffect(() => {
    if (isPane() || UNTABBED.includes(pathname)) return;
    if (pending.current && pending.current !== pathname) return;
    pending.current = null;
    const current = findLeaf(layout, focused);
    const holder = current?.tabs.includes(pathname) ? current : paneHolding(pathname);
    if (holder) {
      if (holder.active !== pathname || holder.id !== focused) {
        commit(
          mapNode(layout, holder.id, (found) => ({ ...(found as Leaf), active: pathname })),
          holder.id
        );
      }
      return;
    }
    const pane = findLeaf(layout, focused) ?? leaves(layout)[0];
    register(pathname);
    commit(mapNode(layout, pane.id, (found) => withTab(found as Leaf, pathname)), pane.id);
  }, [pathname, openTabs, panes, focusedPane]);

  const value = useMemo<TabsValue>(
    () => ({
      tabs: openTabs,
      activeHref: pathname,
      open,
      openHere,
      close,
      selectTab,
      setTitle,
      describeWorkspaces,
      panes,
      focusedPane,
      dropTab,
      focusPane,
      resizePane,
    }),
    [openTabs, pathname, open, openHere, close, selectTab, setTitle, describeWorkspaces,
     panes, focusedPane, dropTab, focusPane, resizePane]
  );

  return <TabsContext.Provider value={value}>{children}</TabsContext.Provider>;
}

/** Keeps one tab's name in step with the page inside it. */
export function useTabTitle(title: string | null | undefined) {
  const { activeHref, setTitle } = useTabs();
  const pane = usePaneMode();
  useEffect(() => {
    if (!title) return;
    if (pane) {
      // Outer shell owns the tab strip; this document is only the page body.
      window.parent.postMessage(
        { type: TAB_TITLE_MESSAGE, href: window.location.pathname, title },
        window.location.origin
      );
      return;
    }
    setTitle(activeHref, title);
  }, [activeHref, title, setTitle, pane]);
}
