"use client";

import { cn } from "@/lib/utils";

/** A workspace's own mark, wherever the workspace is listed.
 *
 *  Two kinds, told apart by their shape, because that is how the server
 *  stores them: a `data:` URL for a picture the user uploaded, and otherwise
 *  a short piece of text, which in practice is an emoji. An image is drawn
 *  through `<img>` and never inlined — an SVG inlined into the document can
 *  carry script, and the same file in an `<img>` cannot run one. */
export function WorkspaceIcon({
  icon,
  className,
}: {
  icon: string | null | undefined;
  className?: string;
}) {
  if (!icon) return null;
  if (icon.startsWith("data:")) {
    return (
      // Decorative: the name it sits next to is the label.
      // eslint-disable-next-line @next/next/no-img-element
      <img
        src={icon}
        alt=""
        aria-hidden
        className={cn("size-4 shrink-0 rounded-[3px] object-contain", className)}
      />
    );
  }
  return (
    <span
      aria-hidden
      className={cn(
        "flex size-4 shrink-0 items-center justify-center text-[13px] leading-none",
        className
      )}
    >
      {icon}
    </span>
  );
}
