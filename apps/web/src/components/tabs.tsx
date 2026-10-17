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
import { BROWSER_ROUTE, previewLabel, previewTarget } from "@/lib/preview";

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
 * hydrate against, which is the default list.
 * ---------------------------------------------------------------------- */

const SERVER_TABS: Tab[] = [START];
let tabs: Tab[] = SERVER_TABS;
let restored = false;
const listeners = new Set<() => void>();

function publish(next: Tab[]) {
  if (next.length === tabs.length && next.every((tab, index) =>
    tab.href === tabs[index].href && tab.title === tabs[index].title
    && (tab.icon ?? "") === (tabs[index].icon ?? "")
    && (tab.detail ?? "") === (tabs[index].detail ?? ""))) return;
  tabs = next;
  try {
    window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(tabs));
  } catch {
    // A browser with storage switched off keeps its tabs for this page only.
  }
  for (const listener of listeners) listener();
}

/** Reads the saved list once, on the client, after hydration has committed. */
function restore() {
  restored = true;
  try {
    const stored = window.sessionStorage.getItem(STORAGE_KEY);
    if (!stored) return;
    const parsed: unknown = JSON.parse(stored);
    if (!Array.isArray(parsed)) return;
    const saved = parsed.filter(
      (tab): tab is Tab =>
        typeof tab === "object" && tab !== null &&
        typeof (tab as Tab).href === "string" && typeof (tab as Tab).title === "string"
    );
    if (saved.length) publish(saved);
  } catch {
    // Unreadable storage is no reason to lose the page.
  }
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  if (!restored) restore();
  return () => { listeners.delete(listener); };
}

const readTabs = () => tabs;
const readServerTabs = () => SERVER_TABS;

type TabsValue = {
  tabs: Tab[];
  /** The route on screen. Derived from the address rather than stored: the
   *  active tab *is* whatever the router is showing, so there is no second
   *  copy of that fact to fall out of step. */
  activeHref: string;
  /** Sidebar navigation: a workspace joins the bar and keeps the others. */
  open: (href: string, info?: TabInfo) => void;
  /** Navigation from inside a page: the tab you are in becomes the new page,
   *  the way a start page hands its own tab over to what you picked on it. */
  openHere: (href: string, info?: TabInfo) => void;
  close: (href: string) => void;
  /** The page naming itself once it knows what it is. */
  setTitle: (href: string, title: string) => void;
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
  // Where we have asked the router to go. Until the address catches up, the
  // bar already shows the destination, and the effect below must not read the
  // page we are leaving as a tab that went missing and put it back.
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
      if (!tabs.some((tab) => tab.href === href)) {
        publish([...tabs, { href, ...info, title: info?.title ?? titleFor(href) }]);
      }
      navigate(href);
    },
    [navigate]
  );

  const openHere = useCallback(
    (href: string, info?: TabInfo) => {
      const here = tabs.findIndex((tab) => tab.href === pathname);
      const existing = tabs.findIndex((tab) => tab.href === href);
      if (existing >= 0) {
        // Already open somewhere: go there, and let the tab we are leaving go
        // rather than leaving a second way into the same page behind.
        if (here >= 0 && here !== existing) publish(tabs.filter((_, index) => index !== here));
      } else {
        const next = { href, ...info, title: info?.title ?? titleFor(href) };
        publish(here < 0 ? [...tabs, next] : tabs.map((tab, index) => (index === here ? next : tab)));
      }
      navigate(href);
    },
    [navigate, pathname]
  );

  const close = useCallback(
    (href: string) => {
      const index = tabs.findIndex((tab) => tab.href === href);
      if (index < 0) return;
      const rest = tabs.filter((tab) => tab.href !== href);
      if (href !== pathname) {
        publish(rest);
        return;
      }
      // Closing the tab you are reading moves you to its right, the way every
      // editor does it, and to its left when it was the last one on that side.
      const target = rest[index] ?? rest[index - 1];
      publish(target ? rest : [START]);
      navigate(target?.href ?? START_HREF);
    },
    [navigate, pathname]
  );

  const setTitle = useCallback((href: string, title: string) => {
    if (!title) return;
    if (!tabs.some((tab) => tab.href === href && tab.title !== title)) return;
    publish(tabs.map((tab) => (tab.href === href ? { ...tab, title } : tab)));
  }, []);

  const describeWorkspaces = useCallback(
    (workspaces: { id: string; name: string; icon?: string; detail?: string }[]) => {
      const known = new Map(
        workspaces.map((workspace) => [`/${encodeURIComponent(workspace.id)}`, workspace])
      );
      publish(
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

  // Whatever the address bar says must have a tab, however it got there — a
  // bookmark, the back button, a redirect, a link we do not own.
  useEffect(() => {
    if (UNTABBED.includes(pathname)) return;
    if (pending.current && pending.current !== pathname) return;
    pending.current = null;
    if (!tabs.some((tab) => tab.href === pathname)) {
      publish([...tabs, { href: pathname, title: titleFor(pathname) }]);
    }
  }, [pathname, openTabs]);

  const value = useMemo<TabsValue>(
    () => ({
      tabs: openTabs, activeHref: pathname, open, openHere, close, setTitle, describeWorkspaces,
    }),
    [openTabs, pathname, open, openHere, close, setTitle, describeWorkspaces]
  );

  return <TabsContext.Provider value={value}>{children}</TabsContext.Provider>;
}

/** Keeps one tab's name in step with the page inside it. */
export function useTabTitle(title: string | null | undefined) {
  const { activeHref, setTitle } = useTabs();
  useEffect(() => {
    if (title) setTitle(activeHref, title);
  }, [activeHref, title, setTitle]);
}
