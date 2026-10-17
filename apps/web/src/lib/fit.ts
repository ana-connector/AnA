/**
 * Fitting a fixed viewport into the room there actually is.
 *
 * A sized preview is a real viewport: the framed page has to believe it is
 * 1280 wide, or checking a layout at 1280 proves nothing. But a 1280-wide
 * frame does not fit in a split pane, and the honest-looking answer — let the
 * pane scroll — means the thing you opened the preview to look at is half
 * off-screen, and reading it means dragging a scrollbar back and forth.
 *
 * So the frame keeps its size and is drawn smaller: scaled by whichever axis
 * runs out of room first, centred, with the pane's background showing on the
 * other axis. The page inside still reports the viewport it was given.
 */

/** The scale that fits `content` inside `box`, never above 1.
 *
 * Capped at 1 because a phone-width frame in a wide pane should be phone
 * sized, not blown up to fill the pane — the size is the question being
 * asked. A box that has not been measured yet (0, or a NaN from a detached
 * node) scales to 1 rather than to 0, so the first paint shows the frame at
 * its own size instead of collapsing it to nothing.
 */
export function fitScale(
  content: readonly [number, number],
  box: { width: number; height: number }
): number {
  const [width, height] = content;
  if (!(width > 0) || !(height > 0)) return 1;
  const across = box.width > 0 ? box.width / width : 1;
  const down = box.height > 0 ? box.height / height : 1;
  return Math.min(1, across, down);
}

/** How the scale is written next to the size inputs: "72%", or nothing at
 *  all when the frame is drawn at its own size. */
export function scaleLabel(scale: number): string | null {
  if (!(scale > 0) || scale >= 0.995) return null;
  return `${Math.round(scale * 100)}%`;
}
