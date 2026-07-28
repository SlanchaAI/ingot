# Ingot console — visual system

Documents what `ui/static/index.html` already commits to. Upstream source of truth for the
palette is `~/Source/slancha-tailwind/src/design-system/tokens.css` (the Slancha Operations
product console). The console inlines its tokens because it ships as one self-contained
file served by FastAPI with no build step.

## Theme

Light and dark are both first-class. `color-scheme: light dark` plus a
`prefers-color-scheme` block, with `:root[data-theme="light"]` / `[data-theme="dark"]`
overrides so an explicit choice wins. Neither theme is the canonical one.

## Color

Variable names are historical (`--rust` predates the move to the blue console); they are
kept because every state class re-skins in place through them.

| Token | Light | Dark | Role |
|---|---|---|---|
| `--paper` | `#F7F8FA` | `#0E1420` | Canvas |
| `--paper-2` | `#FFFFFF` | `#161D2B` | Raised surface |
| `--paper-3` | `#EEF2F7` | `#1D2534` | Inset / code |
| `--ink` | `#15171C` | `#E8EDF4` | Body |
| `--ink-2` | `#515B6E` | `#9AA6B8` | Secondary |
| `--ink-3` | `#667085` | `#8A94A6` | Labels, metadata |
| `--line` / `--line-2` | `#D9DEE7` / `#C4CDDA` | `#253044` / `#313D52` | Borders |
| `--rust` | `#0F63C9` | `#4D9BFF` | Primary, links, quarantined state |
| `--pass` | `#0C6E57` | `#46C39F` | Challenger wins, gate passed |
| `--fail` | `#BE3B34` | `#E0796D` | Refused, regression |
| `--warn` | `#8A5D00` | `#D8B45C` | Running, thin margin, caution |

Departures from upstream, all to clear AA as text: `--rust` is the text-safe `#0F63C9`, not
the fill `#1F78E8`; `--pass` is `#0C6E57`, not the aqua `#34BCA4`. The `*-bg` washes are
carried over unchanged.

The ink ramp has three tiers where the Slancha system has two, and the invented third tier
was the one that failed — `#98A2B3` measured 2.42:1 on the canvas, below even the 3:1
large-text bar, and it was the color the held-out task count was drawn in. `--ink-3` now
takes the canonical `--slancha-muted` `#667085` (4.68:1) and `--ink-2` drops one step to
`#515B6E` (6.43:1) so three tiers stay distinguishable. Measured worst case, light and
dark, every token pair now clears 4.5:1.

**Status is never carried by color alone.** Every state pairs its color with a glyph
(`✓ ✕ ⛔ ⚠`) or an explicit word.

## Typography

One family in multiple weights plus a mono, paired on a contrast axis — no two similar
sans-serifs. `--serif` is an alias for the UI family and does not load a serif; the name
is historical.

- **UI**: Figtree Variable, system fallback.
- **Data, labels, all numerics**: JetBrains Mono Variable, with `font-variant-numeric:
  tabular-nums` everywhere a number can change under a 3s poll.
- **Scale is fixed, not fluid.** Board title `2.4rem`, section heads `1.9rem`, with one
  structural step down under `640px` (`1.9` / `1.55`). An operator reads this at one desk
  on one monitor; type that scales with every pixel of window width only wobbles.
- **Board title**: `letter-spacing: -.02em`, `text-wrap: balance`.
- **Prose**: `text-wrap: pretty`, capped at `48rem`.

Mono uppercase with wide tracking marks metadata and section identity. It is the console's
voice; it is not an eyebrow-per-section reflex — the four bands are the four objects the
product has.

## Layout

- Page `max-width: 74rem`, centered, `padding: 0 1.4rem 4rem`.
- Bands separated by `padding-top: 2.6rem` and a rule, not by cards. Cards appear only
  where something is genuinely a discrete object: the review card, the KPI cells, a skill
  row. Never nested.
- Responsive grids use `repeat(auto-fit, minmax(Npx, 1fr))` rather than breakpoints.
- Dialogs: `.cmp-overlay` fixed, `z-index: 50`. The promotion dialog is a flex column with
  a scrolling body and a pinned decision row, because its evidence grows with the held-out
  set.

## Motion

**No page-load choreography.** The console opens into a task. There is no section reveal:
the four bands are the four objects the product has, not a narrative. The scroll-reveal
this replaces also gated visibility on a CSS transition, and transitions do not run in a
background tab or a headless renderer — the Skills section shipped at `opacity: 0` with
8,600px of content still in the layout. Content is visible by default; motion only ever
enhances it.

- Staggered list entrance on first paint only; the 3s refresh never re-animates.
- The review card carries a slow beam while a challenger waits — the one decorative motion,
  and it marks the one state that needs a human.
- `prefers-reduced-motion: reduce` drops the beam and settles every reveal instantly.

## Components

`.kpi` · `.alert` · `.review-card` (duel → margin → cautions → risk → diff → evidence) ·
`.srow` grouped into `.skill-folder` by provenance · `.hrow` + `.audit` ·
`.cmp-modal` in three variants (promote, skill browser, reject) · `.chip` · `.pill` ·
`.btn` (`.primary`, `.danger`, disabled).

## Rules specific to this surface

1. A number that can change under the poll is mono and tabular.
2. A destructive or irreversible control is never the element that receives focus when a
   dialog opens.
3. Any caution rendered on the review card renders in the promotion dialog too — they
   share `cautions()` for exactly this reason.
4. Empty states name the command that changes the state.
