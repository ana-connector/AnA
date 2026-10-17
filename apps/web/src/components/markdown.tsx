"use client";

import { memo } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useTabs } from "@/components/tabs";
import { browserHref, isLoopback, previewLabel } from "@/lib/preview";

/** Agent replies are markdown — code fences, lists, tables — so they are
 *  rendered rather than shown as literal asterisks and backticks.
 *
 *  Styled per element instead of with a typography plugin: the project does not
 *  ship one, and the bubble already sets the surface colors, so these only need
 *  spacing and the few block treatments (code, tables, quotes).
 *
 *  Memoized on the text: parsing is the expensive part of a transcript render,
 *  and without this every streamed token re-parsed every earlier reply too. */
export const Markdown = memo(function Markdown({ children }: { children: string }) {
  const { open } = useTabs();
  return (
    <div className="flex flex-col gap-2 text-sm leading-relaxed break-words">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          p: ({ children }) => <p className="whitespace-pre-wrap">{children}</p>,
          a: ({ children, href }) => (
            <a
              href={href}
              target="_blank"
              rel="noreferrer"
              className="underline underline-offset-4"
              onClick={(event) => {
                // A link to this machine is something the agent just started,
                // so it opens in a browser tab beside the conversation rather
                // than in a window that buries it. Anything else is somebody
                // else's site and still opens where the user expects.
                if (!href || !isLoopback(href)) return;
                if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return;
                event.preventDefault();
                open(browserHref(href), { title: previewLabel(href) });
              }}
            >
              {children}
            </a>
          ),
          ul: ({ children }) => (
            <ul className="flex list-disc flex-col gap-1 pl-5">{children}</ul>
          ),
          ol: ({ children }) => (
            <ol className="flex list-decimal flex-col gap-1 pl-5">{children}</ol>
          ),
          h1: ({ children }) => <h1 className="font-heading text-base font-semibold">{children}</h1>,
          h2: ({ children }) => <h2 className="font-heading text-sm font-semibold">{children}</h2>,
          h3: ({ children }) => <h3 className="font-heading text-sm font-semibold">{children}</h3>,
          blockquote: ({ children }) => (
            <blockquote className="border-l-2 border-border pl-3 text-muted-foreground">
              {children}
            </blockquote>
          ),
          code: ({ className, children, ...props }) => {
            // Fenced blocks arrive with a language class; inline code does not.
            const isBlock = Boolean(className?.startsWith("language-"));
            if (isBlock) {
              return (
                <code className="font-mono text-xs" {...props}>
                  {children}
                </code>
              );
            }
            return (
              <code
                className="rounded bg-foreground/10 px-1 py-0.5 font-mono text-[0.85em]"
                {...props}
              >
                {children}
              </code>
            );
          },
          pre: ({ children }) => (
            // Its own scroll container: a long line must not widen the bubble
            // and push the whole column sideways. Focusable with it, because a
            // region you can only scroll with a pointer is a region a keyboard
            // cannot read.
            <pre
              tabIndex={0}
              className="overflow-x-auto rounded-md bg-foreground/10 p-3 focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none"
            >
              {children}
            </pre>
          ),
          table: ({ children }) => (
            <div className="overflow-x-auto">
              <table className="w-full border-collapse text-xs">{children}</table>
            </div>
          ),
          th: ({ children }) => (
            <th className="border border-border px-2 py-1 text-left font-medium">{children}</th>
          ),
          td: ({ children }) => <td className="border border-border px-2 py-1">{children}</td>,
          hr: () => <hr className="border-border" />,
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
});
