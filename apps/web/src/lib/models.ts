/**
 * Model names, grouped the way a picker needs them.
 *
 * OpenCode is one CLI in front of several providers and its names are
 * `provider/model`, dozens at a time. The provider is the segment before the
 * *first* slash and the rest is kept whole: a model id may carry slashes of
 * its own (`omniroute/tapie/claude-opus-5`), and splitting on every slash
 * would invent a provider that does not exist.
 *
 * Kept out of the component so the rule is testable without a browser.
 */

/** Above this many names a dropdown stops being readable and a search box
 *  earns its place. Four Claude aliases stay a dropdown; OpenCode's seventy
 *  do not. */
export const MODEL_PICKER_THRESHOLD = 12;

export type ModelGroup = {
  /** "" for a name with no provider prefix, which is every other CLI. */
  provider: string;
  models: string[];
};

/** Groups by provider, in the order the names arrive — the server hands them
 *  over as the CLI listed them, which already groups them. */
export function groupByProvider(models: string[]): ModelGroup[] {
  const byProvider = new Map<string, string[]>();
  for (const name of models) {
    const slash = name.indexOf("/");
    const provider = slash === -1 ? "" : name.slice(0, slash);
    const existing = byProvider.get(provider);
    if (existing) existing.push(name);
    else byProvider.set(provider, [name]);
  }
  return [...byProvider.entries()].map(([provider, names]) => ({
    provider,
    models: names,
  }));
}

/** What the picker shows for one name inside its group: the provider is the
 *  group heading already, so repeating it in every row is noise. */
export function modelLabel(name: string, provider: string): string {
  return provider && name.startsWith(`${provider}/`)
    ? name.slice(provider.length + 1)
    : name;
}
