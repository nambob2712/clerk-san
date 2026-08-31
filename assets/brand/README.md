# Clerk-san brand assets

This directory contains the original **Clerk Pivot** identity used by the Clerk-san application.

## Concept

The mark is a geometric K: its uninterrupted spine represents the preserved source, its two arms
represent the machine-produced candidate and verified record, and its single indigo square is the
human-review pivot between them. It is not a shield, certification mark, or automatic-approval symbol.

## Assets

| Asset | Use |
| --- | --- |
| `clerksan-mark.svg` | 24px master mark |
| `clerksan-mark-16.svg` | Pixel-adjusted favicon and compact control mark |
| `clerksan-mark-48.svg` | Larger product and documentation mark |
| `clerksan-mark-reversed.svg` | Mark on dark ink surfaces |
| `clerksan-lockup.svg` | Horizontal light-surface lockup |
| `clerksan-lockup-reversed.svg` | Horizontal dark-surface lockup |
| `icons/*.svg` | Nine product-specific, 24px outlined icons |

The UI caption remains localized live text and is not part of the lockup artwork.

## Provenance and license

The geometry in this directory was authored from first principles for Clerk-san on 2026-08-31.
An internal AI concept sheet was used to compare scale and composition, but no generated raster or
vector path was copied, traced, or shipped. The lockup uses an SVG `<text>` element with a system
font stack; it does not embed or redistribute a font.

These project assets are Copyright (c) 2026 PHAM BAO NAM and are distributed under the repository's
[MIT License](../../LICENSE). Third-party product logos, icon sets, model assets, and fonts are not
included here.

## Rules

- Use the mark at its intended 16px, 24px, or 48px optical size.
- Use the standard lockup on light neutral surfaces and the reversed lockup on dark ink surfaces.
- Product icons inherit `currentColor`; state or navigation code owns their rendered color.
- Keep the square pivot and K geometry intact. Do not rotate, stretch, enclose, or add branches.
- Do not add gradients, glows, shadows, rounded badge containers, checkmarks, or compliance language.
- Green remains reserved for verified runtime state and never appears in the master brand artwork.

See [the brand specification](../../docs/brand-spec.md) for tokens and implementation guidance.
