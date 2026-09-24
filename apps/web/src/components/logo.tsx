/** The AnA mark, as a glyph rather than a picture.
 *
 *  `public/logo.svg` carries its own white disc, which is what the browser tab
 *  needs; inside the sidebar the mark sits on the shadcn header's coloured
 *  square instead, so it has to take the colour it is given. Hence
 *  `currentColor` and no background of its own. */
export function Logo({ className }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 300 300"
      fill="currentColor"
      xmlns="http://www.w3.org/2000/svg"
      className={className}
      role="img"
      aria-label="AnA"
    >
      <path d="M55 244H94.7895L123.301 160.897L151.158 244H204.211H244L170.579 55H128.421L55 244ZM145.947 94.8894H151.158L198.635 228.329H163.474L146.643 179.417L131.773 136.204L145.947 94.8894Z" />
    </svg>
  );
}
