# Clerk-san brand specification

> Identity: Clerk Pivot
>
> Approved: 2026-08-31
>
> Asset completeness: mark, lockup, responsive variants, and nine product icons

## Product idea

Clerk-san preserves an immutable source, proposes a local machine-derived candidate, and requires a
human review decision before a verified record exists. The identity visualizes that contract instead
of using generic AI, cloud, shield, or checkmark imagery.

## Mark construction

The geometric K has four invariant parts:

1. one uninterrupted vertical source spine;
2. one upper candidate arm;
3. one lower verified-record arm; and
4. one square human-review pivot at their junction.

The pivot is a bookkeeping registration point, not a circuit node. The mark has no enclosing tile,
page outline, certification badge, or decorative layer.

## Palette

| Token | Value | Use |
| --- | --- | --- |
| `ink` | `#20242C` | Standard mark and lockup |
| `accent` | `#4E5BD5` | The single review pivot |
| `reversed-ink` | `#FFFFFF` | Mark and wordmark on dark surfaces |
| `reversed-accent` | `#8993FF` | Pivot on dark ink surfaces |

These values align with the product's semantic ink and accent tokens. Success green is a runtime
state color and is never part of the identity.

## Typography

The wordmark is exactly `Clerk-san`. The SVG lockup uses live text with this system stack:

```text
ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif
```

No commercial font file or outlined commercial glyph is distributed. Application captions remain
live, localized HTML text below the lockup.

## Responsive assets

- 16px: `clerksan-mark-16.svg`, with pixel-adjusted 2px strokes.
- 24px: `clerksan-mark.svg`, the default master.
- 48px and larger: `clerksan-mark-48.svg`, with display-scale optical spacing.
- Dark surfaces: reversed mark or lockup only.

Do not mechanically scale the display mark down to favicon size when the 16px asset is available.

## Product icon system

All product icons use a `0 0 24 24` viewBox, a 20px optical footprint, 1.75px square-ended strokes,
`currentColor`, no background tile, and at least one clear square pivot. Audit timeline may repeat the
pivot to represent ordered events. The nine meanings are:

- document intake;
- human review;
- verified record;
- audit timeline;
- duplicate evidence;
- local-first processing;
- evidence search;
- accounting export; and
- recurring bill.

Selection, status, and feedback colors are applied by the UI. The source SVGs remain monochrome.
React navigation loads the icon URLs through a CSS mask so `currentColor` follows hover, selected,
and disabled states without duplicating SVG paths. Streamlit inlines only allowlisted assets and
removes title metadata when an instance is decorative, preventing repeated accessibility IDs.

The browser favicon uses `clerksan-mark-16.svg`; the full lockup is reserved for surfaces with enough
horizontal space.

## Provenance

The SVG geometry was authored from first principles for Clerk-san on 2026-08-31. An internal AI
concept sheet helped compare composition and small-size behavior; it is not included in the source
snapshot, and no generated path or raster was copied or traced. The assets are project source under
the root MIT License, Copyright (c) 2026 PHAM BAO NAM.

## No-go zone

- No shield, automatic checkmark, robot, cloud, circuit, or neural-network motif.
- No gradients, glow, drop shadow, glass, 3D treatment, or ornamental background shape.
- No rounded badge or app-tile container built into the artwork.
- No additional pivot in the master mark and no green master-logo variant.
- No description that implies legal certification, automatic approval, or production assurance.
