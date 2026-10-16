/** Turning a file somebody picked into a workspace icon the server will take.
 *
 *  The server stores one string per workspace and sends it with every
 *  listing, so the picture has to be small before it is stored, not after.
 *  A raster image is redrawn at icon size; an SVG is kept as it is, because
 *  redrawing it would throw away the one thing it is good at. */

/** Mirrors `MAX_ICON_LENGTH` in packages/orchestrator/workspaces.py. */
export const MAX_ICON_LENGTH = 256_000;

/** What the icon is drawn at, at twice the 32px the biggest preview uses. */
const ICON_PIXELS = 64;

function base64(text: string): string {
  const bytes = new TextEncoder().encode(text);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

export async function fileToIcon(file: File): Promise<string> {
  if (!file.type.startsWith("image/") && !file.name.toLowerCase().endsWith(".svg")) {
    throw new Error("이미지 파일만 올릴 수 있다 (png, svg, jpeg, webp, gif).");
  }
  // 8MB of source is already far more than an icon needs; reading more than
  // that into memory to shrink it is not worth it.
  if (file.size > 8_000_000) throw new Error("파일이 너무 크다. 8MB 이하로 올려라.");

  if (file.type === "image/svg+xml" || file.name.toLowerCase().endsWith(".svg")) {
    const url = `data:image/svg+xml;base64,${base64(await file.text())}`;
    if (url.length > MAX_ICON_LENGTH) {
      throw new Error("SVG가 너무 크다. 글꼴이나 내장 이미지를 빼고 다시 올려라.");
    }
    return url;
  }

  const source = await createImageBitmap(file);
  try {
    const scale = Math.min(ICON_PIXELS / source.width, ICON_PIXELS / source.height, 1);
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(source.width * scale));
    canvas.height = Math.max(1, Math.round(source.height * scale));
    const context = canvas.getContext("2d");
    if (!context) throw new Error("이 브라우저에서는 이미지를 줄일 수 없다.");
    context.drawImage(source, 0, 0, canvas.width, canvas.height);
    // PNG rather than the original type: transparency survives, and a photo
    // scaled to 64px is small either way.
    const url = canvas.toDataURL("image/png");
    if (url.length > MAX_ICON_LENGTH) throw new Error("이미지를 충분히 줄이지 못했다.");
    return url;
  } finally {
    source.close();
  }
}
