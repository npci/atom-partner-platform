# Partner Platform — Design System

The visual + interaction system for the partner (bank-side) frontend.
Aesthetic: a **Linear/Stripe-dashboard hybrid** — neutral surfaces, one
disciplined accent, dense but breathable. It's an operational console a bank
reviewer lives in all day, not a marketing site. **Light mode only.**

The tokens below live in [`src/index.css`](src/index.css) `:root`. Components
are **hand-rolled against these tokens** (no external UI library) to keep the
codebase dependency-light, the way the rest of the platform is built.

> **Rule:** new code uses the canonical tokens. The legacy `--bg-*` /
> `--text-primary` / `--text-secondary` names still resolve (they're aliases),
> but don't reach for them in new work. Never re-introduce a per-component
> local palette (the old `T = { … }` objects in the panels) — that drift is
> exactly what this system replaces.

---

## 1. Color

### Surfaces (cool-neutral greys)
| Token | Value | Use |
|---|---|---|
| `--surface-app` | `#f7f8fa` | page background |
| `--surface-card` | `#ffffff` | cards, panels, drawer |
| `--surface-sunken` | `#f0f2f5` | inputs, code blocks, table header |
| `--surface-hover` | `#f3f4f7` | row / card hover |

### Text
| Token | Value | Use |
|---|---|---|
| `--text-strong` | `#0f1729` | headings, key numbers |
| `--text-default` | `#3a4256` | body |
| `--text-muted` | `#71798c` | meta, captions |
| `--text-onaccent` | `#ffffff` | text on accent fills |

### Borders
| Token | Value | Use |
|---|---|---|
| `--border` | `#e4e7ec` | default 1px |
| `--border-strong` | `#d3d8e0` | input outline, dividers under pressure |

### Accent + semantics
Each semantic ships with a `-soft` companion for tinted badge/banner
backgrounds — so nothing hand-rolls `rgba(…, 0.15)` strokes anymore.

| Role | Solid | Soft |
|---|---|---|
| Accent / Info | `--accent` `#2f5fe0` (hover `--accent-hover` `#2750c4`) | `--accent-soft` / `--info-soft` `#eaf0fe` |
| Success | `--success` `#16a34a` | `--success-soft` `#e7f6ec` |
| Warning | `--warning` `#d97706` | `--warning-soft` `#fdf2e3` |
| Danger | `--danger` `#dc2626` | `--danger-soft` `#fcebeb` |

Status badges pair **icon + color**, never color alone (accessibility).

---

## 2. Spacing — 4px base

`--sp-1: 4` · `--sp-2: 8` · `--sp-3: 12` · `--sp-4: 16` · `--sp-5: 20` ·
`--sp-6: 24` · `--sp-8: 32` · `--sp-10: 40`

Page gutter `--sp-8` desktop / `--sp-4` mobile. Card padding `--sp-5`.

---

## 3. Type scale

| Token | Size | Weight | Use |
|---|---|---|---|
| `--fs-display` | 28 | 700 | page hero (Dashboard) |
| `--fs-h1` | 20 | 700 | page title |
| `--fs-h2` | 16 | 600 | card / section title |
| `--fs-body` | 14 | 400 | body |
| `--fs-meta` | 12.5 | 500 | meta, table cells |
| `--fs-micro` | 11 | 600 (+0.3 tracking, uppercase) | labels, badges |

Weights: `--fw-regular 400` · `--fw-medium 500` · `--fw-semibold 600` ·
`--fw-bold 700`. Font stack stays the system-font stack already in `index.css`.

---

## 4. Elevation, radius, focus

| Token | Value | Use |
|---|---|---|
| `--radius-sm` | 6px | inputs, small controls |
| `--radius-md` | 10px | cards, panels |
| `--radius-lg` | 14px | drawer, modal |
| `--radius-pill` | 999px | badges, pills |
| `--shadow-sm` | `0 1px 2px rgba(15,23,41,.06)` | resting card |
| `--shadow-md` | `0 2px 8px rgba(15,23,41,.08)` | raised / hover |
| `--shadow-pop` | `0 8px 24px rgba(15,23,41,.12)` | drawer, modal |
| `--ring` | `0 0 0 3px rgba(47,95,224,.35)` | `:focus-visible` on every interactive |

`:focus-visible` is wired globally in `index.css` — interactive elements get
the ring for free.

---

## 5. Component kit (hand-rolled, Phase 1)

Every screen is assembled from these. Status: ⬜ to build.

| Component | Replaces today's… | Notes |
|---|---|---|
| `Button` ⬜ | inline `runBtn` / `ghostBtn`, the phantom `.btn-secondary` | variants `primary / secondary / ghost / danger`, sizes `sm / md`, `loading` state |
| `Badge` ⬜ | the 4 copies of status-pill logic | `tone` + optional `icon`; built from the existing `Pill` |
| `Card` / `PanelCard` ⬜ | the 4 panels' duplicated header / loading / error / empty boilerplate | `PanelCard` owns the loading/error/empty/header states each panel reimplements |
| `Field` ⬜ | bare `<input>` + detached `<label>` | label↔input via `htmlFor`, help text, error slot |
| `Toast` (provider) ⬜ | Settings' singleton toast + `alert()` + inline banners + `setTimeout` success screens | one queue, auto-dismiss, dismissible |
| `Tabs` ⬜ | — (needed for ChangeDetail restructure) | sticky, `aria` roles, keyboard arrows |
| `Drawer` ⬜ | inline DocPreviewDrawer styles | reusable slide-over; full-width < 700px |
| `Stepper` ✅ keep | LifecycleStepper | already `role="progressbar"`; restyled to tokens |
| `StatTile` ✅ keep | — | the gold-standard primitive (icon+color, keyboard); re-point to tokens |

**Baked into the primitives (so pages inherit it):**
- **Accessibility** — collapsibles/clickables are real `<button>`s with
  `aria-expanded`; `:focus-visible` ring everywhere; badges carry icons.
- **Responsiveness** — header nav collapses, `1fr 1fr` grids stack, drawer
  goes full-width below ~700px.

---

## 6. Roadmap

- **Phase 0 — Design system definition.** ✅ Tokens in `index.css` + this doc.
- **Phase 1 — Primitive kit.** Build the components above against the tokens;
  retire the panels' local `T` objects and inline buttons.
- **Phase 2 — Page reskin.** Login → Dashboard → Settings / Users /
  KnowledgeBase. Fold accessibility + responsiveness into each page's pass.
- **Phase 3 — ChangeDetail restructure.** Break the ~4,400-line page into
  sub-navigated sections (sticky tabs: *Overview · Documents · Feasibility ·
  Decision · Activity*), anchor the Decision panel + activity composer, add the
  missing empty states.
