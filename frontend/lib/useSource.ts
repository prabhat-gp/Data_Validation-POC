"use client";

import { useEffect, useState } from "react";

export const SOURCE_KEY = "source";
export const SOURCE_EVENT = "dq-source";
export const ALL_SOURCES = "All sources";

/**
 * The source-system filter, shared by every page.
 *
 * It lives in localStorage with a window event rather than in React context so
 * the Sidebar can own the control while pages in a different subtree react to
 * it, without a provider wrapping the whole app or a full reload.
 *
 * Returns ALL_SOURCES when nothing is chosen; callers treat that as "no filter"
 * and pass undefined to the API.
 */
export function useSource(): [string, (next: string) => void] {
  const [source, setSource] = useState(ALL_SOURCES);

  useEffect(() => {
    setSource(localStorage.getItem(SOURCE_KEY) || ALL_SOURCES);
    const onChange = (e: Event) => setSource((e as CustomEvent).detail);
    window.addEventListener(SOURCE_EVENT, onChange);
    return () => window.removeEventListener(SOURCE_EVENT, onChange);
  }, []);

  return [source, setGlobalSource];
}

export function setGlobalSource(next: string) {
  localStorage.setItem(SOURCE_KEY, next);
  window.dispatchEvent(new CustomEvent(SOURCE_EVENT, { detail: next }));
}

/** undefined = every source, for endpoints where the param is optional. */
export function sourceParam(source: string): string | undefined {
  return source === ALL_SOURCES ? undefined : source;
}
