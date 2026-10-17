import type { Metadata } from "next";
import { Geist_Mono } from "next/font/google";
import localFont from "next/font/local";
import "./globals.css";
import { AppShell } from "@/components/app-shell";
import { ShellMetaProvider } from "@/components/shell-meta";
import { TabsProvider } from "@/components/tabs";
import { ThemeProvider } from "@/components/theme";
import { TooltipProvider } from "@/components/ui/tooltip";
import { Toaster } from "@/components/ui/toast";

const pretendard = localFont({
  src: "../fonts/PretendardVariable.woff2",
  variable: "--font-pretendard",
  display: "swap",
  weight: "100 900",
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "AnA",
  description: "AnA is Not Agent — Claude Code and Codex, orchestrated",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      lang="ko"
      // next-themes writes the class before paint; without this React warns
      // about the attribute it did not render.
      suppressHydrationWarning
      className={`${pretendard.variable} ${geistMono.variable} h-full font-sans antialiased`}
    >
      <body className="flex min-h-full flex-col">
        <ThemeProvider>
          <TooltipProvider>
            {/* Above the router's own output: the open tabs and the window
                chrome outlive any one page, so a tab switch only swaps the
                focused pane's document rather than remounting the shell. */}
            <TabsProvider>
              <ShellMetaProvider>
                <AppShell>{children}</AppShell>
              </ShellMetaProvider>
            </TabsProvider>
            {/* One place for "it saved" / "it failed" across every page: the
                console used to answer a save with a line of text that
                appeared somewhere near the form, or with nothing at all. */}
            <Toaster />
          </TooltipProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
