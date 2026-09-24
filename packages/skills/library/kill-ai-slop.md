---
# Vendored from yetone/kill-ai-slop (Apache-2.0), skill/SKILL.md, body verbatim. Only the frontmatter is ANA's:
# `title` is what the settings page shows a person, `description` is the one
# line the skill catalog puts in every prompt (upstream's is a paragraph of
# trigger phrases for a different harness), and `roles` is who may use it here.
name: kill-ai-slop
title: AI 슬롭 제거
description: Find and strip the machine-default UI and copy tics (gradients, emoji, badge spam, AI voice)
roles: [worker]
---

# Kill AI Slop

AI slop is **ugly** in a specific way: it piles on every possible style and
detail without settling on a focus. A gradient, a glow, a mascot, emoji, a wall
of glowing cards, every default switched on at once, until every product looks
like the same garish template. It reads as "designed" in a thumbnail and falls
apart the moment anyone looks. Your job is to strip it back to something a
person would actually choose.

The principles, held on every fix you make:

1. **Decide before you decorate.** Every visual choice must be explainable.
2. **One accent, one voice.**
3. **Hierarchy from scale and space.** Coloring words or swapping fonts is a shortcut.
4. **Subtract first.** The first move toward not-ugly is removing things.
5. **Specific beats punchy** in copy.
6. **Decoration must mean something** — icons, badges, callouts are signals.

## Workflow

Follow these steps in order. Do not mass-edit before the user has seen the report.

### 1. Scope
Confirm what to scan. Default to the app/site source (skip `node_modules`,
`dist`, `build`, `.git`, `vendor`, lockfiles, minified files). Ask if the
project mixes several apps.

### 2. Scan
Run the bundled scanner, which greps the codebase for the code-level signals of
each tell and prints grouped `file:line` hits:

```
node scripts/scan.mjs <root>          # human-readable report
node scripts/scan.mjs <root> --json   # machine-readable, for triage
```

It is pure Node (no dependencies) and never edits files. Use its output as a
starting map, not gospel — confirm each hit by reading the code.

To narrow a scan: `--only=01,06` / `--skip=19` filter by tell id, and
`--exclude=legacy` drops paths (substring match on the project-relative path).
`--rules=extra.mjs` loads additional project- or language-specific tells
(`scripts/rules.ru.mjs` is a shipped Russian-copy example and the template for
your own). Hits the user has confirmed as intentional can be pinned in source
with `deslop-ignore`, `deslop-ignore-next-line 06`, or `deslop-ignore-file`
comments — prefer the id-scoped forms so new tells still surface.

### 3. Triage
For every hit, open the file and decide **slop vs. intentional**. This is the
step that separates this skill from a lint rule. A gradient, a serif, or an
emoji can be a real, defended choice. Keep anything the user clearly chose
(brand tokens, a logo, a deliberate illustration). Flag only defaults.

Read `references/taxonomy.md` for what each tell is and why it reads as
machine-made, and `references/detection.md` for the exact patterns and their
common false positives.

### 4. Report
Before changing anything, give the user a grouped summary: each tell, the
`file:line` hits you confirmed, one sentence on why, and the proposed fix.
Mirror the format:

```
slop  src/Hero.tsx:12   indigo→violet gradient        → one solid accent
slop  src/Hero.tsx:31   gradient-clip headline        → solid ink, scale up
slop  src/Note.tsx:8    border-l-4 callout ×3         → 1 aside, rest is body
slop  copy.md:1         "not just X — it's Y"         → say the specific thing
→ 4 groups, 11 hits.
```

Then ask which groups to apply, or whether to proceed on all.

### 5. Fix
Apply the minimal change that removes the tell while preserving intent and
function. Use `references/fixes.md` for the before→after pattern per tell.

- Prefer editing shared tokens/components over touching every call site.
- Never invent new brand colors; if a palette must change, propose neutrals +
  the project's existing accent and let the user confirm.
- Keep copy meaning; make it specific, don't just delete it.
- Re-run the scanner after fixing to confirm the count dropped, and note any
  hits you intentionally left (with the reason).

## Guardrails

- **Respect authorship.** Treat unfamiliar files and deliberate flourishes as
  someone's choice. When unsure whether something is slop, ask — don't strip it.
- **Small, reviewable diffs.** Never reformat unrelated code. Never run
  `git add -A`; stage explicit files only, and leave others' work-in-progress
  alone.
- **No new dependencies** to do this work.
- **Verify visually when possible.** If a dev server exists, look at the before
  and after; a passing scan is not the same as a better page.

## References

- `references/taxonomy.md` — the 35 tells: what each is, why it's slop, the fix.
- `references/detection.md` — concrete ripgrep/regex patterns + false positives.
- `references/fixes.md` — before→after remediation patterns.
- `scripts/scan.mjs` — the dependency-free scanner.

---

## [ANA addendum — not part of the upstream skill]

Added by ANA (Apache-2.0 requires marking changes). The upstream skill ships a
scanner and three reference files; ANA's skill library is one file per skill, so
neither is here. Read the rest of this section instead of `scripts/scan.mjs`,
`references/taxonomy.md`, `references/detection.md` and `references/fixes.md`.

Scan with grep or ripgrep — add no dependency — and use these as the code-level
signal of each tell:

- **Color**: `#6366f1`, `indigo-500.*purple`, `violet-`, `from-indigo.*to-purple`;
  `bg-clip-text` on a headline; a warm amber/stone wash; the default semantic
  palette (info blue / tip amber / warning orange); a status box whose border,
  text and background are one hue; `radial-gradient`, `blur-3xl`, `opacity-20`
  glow on a near-black page.
- **Type**: `font-serif italic` on one word; Playfair/Lora/Cormorant where sans
  belongs; strike-through or highlight as decoration; a small `uppercase
  tracking-wide text-xs` kicker over every heading; a whole marketing sentence
  as the display headline; every size crammed between 14 and 24px.
- **Copy**: scattered bolded or colored keywords; "not just X — it's Y", "say
  goodbye to", "game-changing"; an emoji on every heading, button and bullet.
- **Components**: `animate-ping` status dots; `border-l-4` callouts; a
  rounded-square icon tile per feature; max radius plus `backdrop-blur` +
  `bg-white/10`; `shadow-2xl` on small things; the same radius on every nested
  layer; a 1px border that dies at the corner; badge and pill spam ("✨ New");
  model-drawn SVG icons; an icon in a tint of itself.
- **Motion**: `hover:scale-105 transition-transform` on everything; a spinner
  that wobbles off its own center.
- **Layout**: ALL-CAPS label + number card grids; invented stat rows ("10k+
  developers"); giant 01 / 02 / 03 section ordinals; cards inside cards; one
  spacing value everywhere (`gap-4`, `p-4`); Inter + Space Grotesk defaults; the
  "tasteful terminal" (mono everywhere, one accent); an operational UI dressed
  as a magazine.

Fix per tell: one solid accent instead of a gradient; solid ink and a larger
size instead of gradient-clipped text; one real `aside` instead of three
callouts; scale and space instead of color for hierarchy; the specific sentence
instead of the punchy one. Edit shared tokens and components rather than call
sites, and re-scan afterwards to confirm the count dropped.
