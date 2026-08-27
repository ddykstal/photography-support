# Annotation Profile V2 Specification (Draft)

This document defines a new profile format for the box-based annotation model.

---

## 1. Goals

V2 is designed to be:

- conceptually simple
- strongly composable
- explicit about geometry
- profile-driven (fewer hardcoded layout assumptions)

Core concepts are:

1. layout mode
2. primary dimension
3. global scale
4. frame width
5. interior/exterior padding
6. positioned text boxes

---

## 2. File structure

A V2 profile is a plain-text file with:

- global directives (`@...`) at top level
- one or more `@box` blocks
- text lines inside each box block
- comments beginning with `#`

Blank lines are ignored.

---

## 3. Version marker

A V2 profile **must** declare:

```text
@profile-version 2
```

If missing, the parser may reject the file or fall back to V1 parser (implementation choice).

---

## 4. Global directives

## 4.1 Layout

### Image-consistent

```text
@layout image width
```

or

```text
@layout image height
```

- `width` (default behavior for image mode): primary dimension = image width
- `height`: primary dimension = image height

### Projection-consistent

```text
@layout projection 16:9
```

- projection aspect ratio is required in this form
- primary dimension is chosen by fit axis relative to projection ratio:
  - if image is width-constrained by projection fit, primary = image width
  - if image is height-constrained by projection fit, primary = image height

---

## 4.2 Typography

```text
@font-family American Typewriter, Courier New, Monospace
@font-size 1.0
@line-spacing 0.30
```

- `@font-size` is a calibrated scalar where `1.0` is the baseline readable size.
- Implementation computes `base_font_px` from primary dimension using an internal calibration, then applies `font-size` as a multiplier.
- `@line-spacing` is multiplier relative to line height

---

## 4.3 Frame and padding

```text
@frame-width 1.0
@padding exterior 1.0, interior 1.0
```

- `@frame-width` is a secondary scale factor based on base font size
  - recommended formula: `frame_px = base_font_px * frame-width`
  - same width for top/bottom/left/right frame
- padding values are multipliers of base font size
  - `exterior_padding_px = base_font_px * exterior-padding`
  - `interior_padding_px = base_font_px * interior-padding`

---

## 4.4 Colors

```text
@background-color #FFFFFF
@text-color exterior #000000
@text-color interior #000000
```

Defaults:

- background: `#FFFFFF`
- exterior text: `#000000`
- interior text: `#000000`

---

## 5. Text boxes

A text box starts with:

```text
@box <exterior|interior> <top|bottom> <left|center|right> [scale <float>] [when any(a,b,c)]
```

Examples:

```text
@box exterior top center
@box interior bottom left scale 0.9 when any(copyright,license)
@box exterior bottom right scale 0.8
```

Then one or more text lines follow until next `@box` or EOF.

Each box has:

- region: `interior` or `exterior`
- edge: `top` or `bottom`
- horizontal anchor: `left`, `center`, `right`
- optional box scale (default `1.0`)
- optional box-level condition `when any(...)`

Effective font size for a box:

- `box_font_px = base_font_px * box.scale`

Box-level conditional rendering:

- If `when any(...)` is present, the box is rendered only if any listed variables are non-empty.
- `@when` is not used at line level in V2.

---

## 6. Box positioning model

There are 12 logical positions:

- vertical: `top` / `bottom`
- region: `interior` / `exterior`
- horizontal: `left` / `center` / `right`

Rules:

1. Box coordinates are resolved relative to image rect and frame rect.
2. `left`/`right` boxes align with image left/right edges.
3. Horizontal token also defines text justification inside the box.
4. Interior boxes draw inside image area with interior padding.
5. Exterior boxes draw in frame gutters with exterior padding.

---

## 7. Text content in boxes

Inside a box, render lines use the same variable substitution style:

- `$title`, `$caption`, etc.

Optional bracket segments remain supported:

```text
$copyright [-- $license]
```

Inline line directives are not part of V2.

- No per-line `@scale`
- No per-line `@wrap`
- No per-line `@when`

All conditional behavior is box-level (`when any(...)`).

---

## 8. Overflow and stacking policy

When multiple boxes target the same zone:

- Top zones stack downward in declaration order.
- Bottom zones stack upward in declaration order.

Overflow policy in V2:

- **Strict**: fail rendering when a box does not fit its zone.

Overlap policy in V2:

- If independently positioned boxes overlap, render all text as positioned (overlap is allowed).
- No automatic deconfliction is performed.
- Z-order is declaration order (later boxes render on top of earlier boxes).

---

## 9. Defaults summary

If omitted:

- `@layout`: **required** in V2
- `@font-family`: implementation default (e.g. `Arial, Sans-Serif`)
- `@font-size`: `1.0` (calibrated baseline; implementation maps this to a reasonable readable size)
- `@line-spacing`: `0.30`
- `@frame-width`: `1.0` (always relative to `base_font_px`)
- `@padding exterior 1.0, interior 1.0`
- colors:
  - background `#FFFFFF`
  - exterior text `#000000`
  - interior text `#000000`
- box scale: `1.0`

---

## 10. Example A (image-consistent width)

```text
@profile-version 2
@layout image width

@font-family American Typewriter, Courier New, Monospace
@font-size 1.0
@line-spacing 0.30

@frame-width 1.0
@padding exterior 1.0, interior 1.0

@background-color #000000
@text-color exterior #FFFFFF
@text-color interior #FFFFFF

@box exterior top center
$title

@box interior bottom left
$copyright

@box exterior bottom center
$shutter-speed sec, f/$aperture, ISO $iso
```

---

## 11. Example B (projection-consistent 16:9)

```text
@profile-version 2
@layout projection 16:9

@font-family American Typewriter, Courier New, Monospace
@font-size 1.0
@line-spacing 0.30

@frame-width 1.0
@padding exterior 1.0, interior 1.0

@background-color #000000
@text-color exterior #FFFFFF
@text-color interior #FFFFFF

@box exterior top center
$title

@box interior bottom left
$copyright

@box exterior bottom center
$shutter-speed sec, f/$aperture, ISO $iso
```

---

## 12. Migration notes from V1

- V1 freeform global keys remain valid only in V1 parser.
- V2 introduces strict grammar + `@profile-version 2`.
- Keep V1 parser intact during migration.
- Add explicit CLI/profile selector if needed.

---

## 13. Diagnostics (recommended)

Optional diagnostics mode should emit structured layout details to **STDERR**:

- resolved layout mode and primary dimension choice
- calibrated base font size
- frame and padding in pixels
- per-box rectangle and measured text block rectangle
- overflow failure reason (strict mode)

This keeps image output/STDOUT clean while making tuning easier.
