"use client";

import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from "react";

/** What the focused page asks the window chrome to show.
 *
 *  The chrome (sidebar, pane host, usage bar) lives above the router so a tab
 *  switch does not remount it. Pages publish here instead of rendering the
 *  chrome themselves. */
export type ShellUsage = {
  refreshKey: number;
  totalCostUsd: number;
  totalTokens: number;
  failingAgents?: Record<string, string>;
};

export type ShellMeta = {
  barActions: ReactNode;
  usage: ShellUsage | null;
  sidebarReloadKey: number;
};

const EMPTY: ShellMeta = {
  barActions: null,
  usage: null,
  sidebarReloadKey: 0,
};

type ShellMetaApi = {
  setMeta: (patch: Partial<ShellMeta>) => void;
  resetMeta: () => void;
};

const ShellMetaStateContext = createContext<ShellMeta>(EMPTY);
const ShellMetaApiContext = createContext<ShellMetaApi | null>(null);

export function ShellMetaProvider({ children }: { children: ReactNode }) {
  const [meta, setMetaState] = useState<ShellMeta>(EMPTY);

  const setMeta = useCallback((patch: Partial<ShellMeta>) => {
    setMetaState((prev) => ({ ...prev, ...patch }));
  }, []);

  const resetMeta = useCallback(() => {
    setMetaState(EMPTY);
  }, []);

  const api = useMemo(() => ({ setMeta, resetMeta }), [setMeta, resetMeta]);

  return (
    <ShellMetaApiContext.Provider value={api}>
      <ShellMetaStateContext.Provider value={meta}>
        {children}
      </ShellMetaStateContext.Provider>
    </ShellMetaApiContext.Provider>
  );
}

export function useShellMeta(): ShellMeta {
  return useContext(ShellMetaStateContext);
}

export function useShellMetaPublisher(): ShellMetaApi {
  const api = useContext(ShellMetaApiContext);
  if (!api) {
    throw new Error("useShellMetaPublisher must be used inside <ShellMetaProvider>");
  }
  return api;
}
