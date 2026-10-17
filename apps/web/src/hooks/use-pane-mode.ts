"use client";

import { useSyncExternalStore } from "react";

/** Whether this document is one pane of a split view rather than the whole
 *  app. A pane is the same page served to an iframe with `?pane=1`, so it
 *  renders its content without the sidebar, the tabs or the status bar —
 *  those belong to the window around it.
 *
 *  Read through a store rather than from `useSearchParams`: a client
 *  component that reads the query makes every page that uses the shell
 *  dynamic, and the shell is on all of them. The server's answer is "not a
 *  pane", which is what a page opened normally is. */
const noSubscribe = () => () => {};
const readPaneMode = () => new URLSearchParams(window.location.search).get("pane") === "1";
const notAPane = () => false;

export function usePaneMode(): boolean {
  return useSyncExternalStore(noSubscribe, readPaneMode, notAPane);
}

/** The same page, addressed as a pane. */
export function paneSrc(href: string): string {
  return `${href}${href.includes("?") ? "&" : "?"}pane=1`;
}
