"use client";

import { MoonIcon, SunIcon } from "lucide-react";
import { ThemeProvider as NextThemesProvider, useTheme } from "next-themes";
import { Button } from "@/components/ui/button";

/** `class` strategy because globals.css defines the dark palette under `.dark`
 *  (`@custom-variant dark (&:is(.dark *))`), and the provider writes that class
 *  before paint so a dark-mode reload does not flash white. */
export function ThemeProvider({ children }: { children: React.ReactNode }) {
  return (
    <NextThemesProvider attribute="class" defaultTheme="system" enableSystem>
      {children}
    </NextThemesProvider>
  );
}

export function ThemeToggle() {
  const { resolvedTheme, setTheme } = useTheme();
  const isDark = resolvedTheme === "dark";

  return (
    <Button
      size="icon-sm"
      variant="ghost"
      aria-label={isDark ? "라이트 모드로" : "다크 모드로"}
      title={isDark ? "라이트 모드로" : "다크 모드로"}
      onClick={() => setTheme(isDark ? "light" : "dark")}
    >
      {/* Both icons render; CSS picks one, so the button does not change
          between server and client render (no hydration mismatch). */}
      <SunIcon className="dark:hidden" />
      <MoonIcon className="hidden dark:block" />
    </Button>
  );
}
