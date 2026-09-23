"use client";

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

/** Agent replies are markdown — code fences, lists, tables — so they are
 *  rendered rather than shown as literal asterisks and backticks.
 *
 *  Styled per element instead of with a typography plugin: the project does not
 *  ship one, and the bubble already sets the surface colors, so these only need
 *  spacing and the few block treatments (code, tables, quotes). */
export function Markdown({ children }: { children: string }) {
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
            // and push the whole column sideways.
            <pre className="overflow-x-auto rounded-md bg-foreground/10 p-3">{children}</pre>
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
}
