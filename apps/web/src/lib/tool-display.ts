export function toolDisplay(toolName: string, input: Record<string, unknown> | null | undefined): string {
  const command = input?.command;
  if (typeof command === "string") return command;
  if (toolName.toLowerCase().startsWith("collaboration")) {
    const name = input?.task_name;
    const target = input?.target;
    const detail = typeof name === "string" ? name : typeof target === "string" ? target : "";
    const encrypted = typeof input?.message === "string" && input.message.startsWith("gAAAA");
    return `${toolName}${detail ? ` · ${detail}` : ""}${encrypted ? " · 전달 내용은 CLI 암호문" : ""}`;
  }
  return JSON.stringify(input ?? {});
}
