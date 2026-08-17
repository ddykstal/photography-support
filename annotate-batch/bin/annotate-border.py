#!/usr/bin/env python3

import argparse
import importlib
import json
import math
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any


VAR_RE = re.compile(r"\$([A-Za-z0-9_-]+)")
OPTIONAL_SEGMENT_RE = re.compile(r"\[([^\[\]]*)\]")

# Base text sizes (pixels) for known output resolutions.
SCREEN_FONT_SIZE_PRESETS: dict[tuple[int, int], int] = {
    (1600, 1200): 28,
    (1920, 1080): 30,
    (3000, 2000): 44,
    (3200, 2400): 56,
    (3840, 2160): 60,
    (6000, 4000): 88,
}

# Friendly profile keys mapped to EXIF/IPTC/XMP tags.
ATTRIBUTE_TAG_MAP: dict[str, list[str]] = {
    "identifier": ["XMP-dc:Identifier"],
    "title": ["XMP-dc:Title"],
    "caption": ["XMP-dc:Description"],
    "copyright": ["XMP-dc:Rights", "IPTC:CopyrightNotice"],
    "license": ["XMP-xmpRights:UsageTerms"],
    "capture-date": ["EXIF:DateTimeOriginal", "XMP-exif:DateTimeOriginal", "XMP-xmp:CreateDate"],
    "camera-make": ["EXIF:Make", "XMP-exif:Make", "XMP-tiff:Make"],
    "camera-model": ["EXIF:Model", "EXIF:CameraModelName", "XMP-exif:Model", "XMP-tiff:Model"],
    "camera": [
        "EXIF:Make",
        "XMP-exif:Make",
        "XMP-tiff:Make",
        "EXIF:Model",
        "EXIF:CameraModelName",
        "XMP-exif:Model",
        "XMP-tiff:Model",
    ],
    "iso": ["EXIF:ISO", "EXIF:PhotographicSensitivity", "XMP-exif:ISOSpeedRatings"],
    "aperture": ["EXIF:FNumber", "Composite:Aperture", "XMP-exif:FNumber"],
    "shutter-speed": ["EXIF:ExposureTime", "Composite:ShutterSpeed", "XMP-exif:ExposureTime"],
    "focal-length": ["EXIF:FocalLength", "Composite:FocalLength"],
    "lens": ["EXIF:LensModel", "EXIF:LensID", "Composite:LensID", "XMP-exifEX:LensModel"],
    "lens-model": ["EXIF:LensModel", "EXIF:LensID", "Composite:LensID", "XMP-exifEX:LensModel"],
    # Backward-compatible underscore aliases.
    "capture_date": ["EXIF:DateTimeOriginal", "XMP-exif:DateTimeOriginal", "XMP-xmp:CreateDate"],
    "camera_make": ["EXIF:Make", "XMP-exif:Make", "XMP-tiff:Make"],
    "camera_model": ["EXIF:Model", "EXIF:CameraModelName", "XMP-exif:Model", "XMP-tiff:Model"],
    "shutter_speed": ["EXIF:ExposureTime", "Composite:ShutterSpeed", "XMP-exif:ExposureTime"],
    "focal_length": ["EXIF:FocalLength", "Composite:FocalLength"],
    "lens_model": ["EXIF:LensModel", "EXIF:LensID", "Composite:LensID", "XMP-exifEX:LensModel"],
}


def normalize_key(value: str) -> str:
    return value.strip().lower().replace("_", "-")


def parse_inline_directives(text_line: str) -> tuple[str, dict[str, str]]:
    # Split on "  @" (two spaces then directive) to keep normal @ chars in body text harmless.
    parts = text_line.split("  @")
    base_text = parts[0].rstrip()
    directives: dict[str, str] = {}

    for chunk in parts[1:]:
        chunk = chunk.strip()
        if not chunk:
            continue
        items = chunk.split(maxsplit=1)
        key = normalize_key(items[0])
        value = items[1].strip() if len(items) > 1 else "true"
        directives[key] = value

    return base_text, directives


def parse_text_template(profile_path: Path) -> dict[str, Any]:
    border: dict[str, Any] = {}
    line_specs: list[dict[str, Any]] = []

    for raw in profile_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        if line.startswith("@"):
            items = line[1:].strip().split(maxsplit=1)
            if not items:
                continue
            key = normalize_key(items[0])
            value = items[1].strip() if len(items) > 1 else "true"
            border[key] = value
            continue

        text, directives = parse_inline_directives(raw.rstrip())
        line_specs.append({"text": text, **directives})

    if not line_specs:
        raise ValueError("Template contains no render lines")

    return {
        "missing_value": "",
        "border": border,
        "line_specs": line_specs,
    }


def load_profile(profile_path: Path) -> dict[str, Any]:
    return parse_text_template(profile_path)


def parse_float(value: Any, default: float) -> float:
    if value is None:
        return default
    try:
        return float(str(value).strip())
    except ValueError:
        return default


def parse_font_family_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value).strip()
    if not text:
        return []
    return [part.strip() for part in text.split(",") if part.strip()]


def collect_variables(line_specs: list[dict[str, Any]]) -> list[str]:
    vars_set: set[str] = set()
    for spec in line_specs:
        text = str(spec.get("text", ""))
        for match in VAR_RE.findall(text):
            vars_set.add(normalize_key(match))
        when_expr = str(spec.get("when", "")).strip()
        if when_expr.startswith("any(") and when_expr.endswith(")"):
            for key in when_expr[4:-1].split(","):
                vars_set.add(normalize_key(key))
    return sorted(vars_set)


def exiftool_tags_for_variables(variables: list[str]) -> list[str]:
    tags: set[str] = set()
    for var in variables:
        mapped = ATTRIBUTE_TAG_MAP.get(var)
        if mapped:
            tags.update(mapped)
    return sorted(tags)


def run_exiftool_json(image_path: Path, tags: list[str]) -> dict[str, str]:
    if shutil.which("exiftool") is None:
        raise RuntimeError("exiftool is required and was not found in PATH")

    cmd = ["exiftool", "-j", *[f"-{tag}" for tag in tags], str(image_path)]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"exiftool failed for {image_path}: {proc.stderr.strip()}")

    payload = json.loads(proc.stdout)
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        return {}

    raw = payload[0]
    out: dict[str, str] = {}
    for tag in tags:
        short = tag.split(":", 1)[1] if ":" in tag else tag
        value = raw.get(short)
        if value is None:
            value = raw.get(tag)
        if value is None:
            out[tag] = ""
        elif isinstance(value, list):
            out[tag] = ", ".join(str(v).strip() for v in value if str(v).strip())
        else:
            out[tag] = str(value).strip()
    return out


def first_nonempty(exif_values: dict[str, str], tags: list[str]) -> str:
    for tag in tags:
        value = exif_values.get(tag, "")
        if value:
            return value
    return ""


def normalize_capture_date(value: str) -> str:
    if not value:
        return ""
    if len(value) >= 10 and value[4] in {":", "-"} and value[7] in {":", "-"}:
        yyyy = value[0:4]
        mm = value[5:7]
        dd = value[8:10]
        if yyyy.isdigit() and mm.isdigit() and dd.isdigit():
            return f"{yyyy}-{mm}-{dd}"
    return value


def build_display_metadata(variables: list[str], exif_values: dict[str, str]) -> dict[str, str]:
    display: dict[str, str] = {}

    for var in variables:
        if var == "camera":
            make = first_nonempty(exif_values, ["EXIF:Make", "XMP-exif:Make", "XMP-tiff:Make"])
            model = first_nonempty(
                exif_values,
                ["EXIF:Model", "EXIF:CameraModelName", "XMP-exif:Model", "XMP-tiff:Model"],
            )
            display[var] = " ".join(part for part in [make, model] if part).strip()
            continue

        mapped = ATTRIBUTE_TAG_MAP.get(var)
        if mapped:
            value = first_nonempty(exif_values, mapped)
            if var in {"capture-date", "capture_date"}:
                value = normalize_capture_date(value)
            display[var] = value
            continue

        display[var] = ""

    return display


def substitute_vars(template: str, metadata: dict[str, str], missing_value: str) -> str:
    def repl(match: re.Match[str]) -> str:
        key = normalize_key(match.group(1))
        value = metadata.get(key, "")
        return value if value else missing_value

    def repl_optional(match: re.Match[str]) -> str:
        inner = match.group(1)
        vars_in_inner = [normalize_key(name) for name in VAR_RE.findall(inner)]

        # Optional block renders only when all referenced variables are present.
        if vars_in_inner and any(not metadata.get(key, "").strip() for key in vars_in_inner):
            return ""

        return VAR_RE.sub(repl, inner)

    text = OPTIONAL_SEGMENT_RE.sub(repl_optional, template)
    text = VAR_RE.sub(repl, text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def line_has_render_content(text: str) -> bool:
    return any(ch.isalnum() for ch in text)


def should_render_line(spec: dict[str, Any], metadata: dict[str, str]) -> bool:
    expr = str(spec.get("when", "")).strip()
    if not expr:
        return True

    # Simple form: any(a,b,c)
    if expr.startswith("any(") and expr.endswith(")"):
        keys = [normalize_key(k.strip()) for k in expr[4:-1].split(",") if k.strip()]
        return any(bool(metadata.get(k, "").strip()) for k in keys)

    return True


def normalize_font_token(value: str) -> str:
    return "".join(ch for ch in value.lower() if ch.isalnum())


def expand_family_aliases(family: str) -> list[str]:
    norm = normalize_font_token(family)
    generic_map = {
        "sansserif": ["Arial", "Helvetica", "Avenir", "SF Pro", "Verdana"],
        "serif": ["Times New Roman", "Times", "Georgia", "Palatino"],
        "monospace": ["Menlo", "Courier New", "Courier", "Monaco"],
    }
    aliases = generic_map.get(norm)
    return aliases if aliases else [family]


def find_font_path_from_families(font_families: list[str], font_weight: str = "normal") -> Path | None:
    font_dirs = [
        Path("/System/Library/Fonts"),
        Path("/Library/Fonts"),
        Path("~/Library/Fonts").expanduser(),
    ]

    font_files: list[Path] = []
    for directory in font_dirs:
        if not directory.exists():
            continue
        for path in directory.rglob("*"):
            if path.is_file() and path.suffix.lower() in {".ttf", ".otf", ".ttc"}:
                font_files.append(path)

    indexed = [(path, normalize_font_token(path.stem)) for path in font_files]

    expanded_families: list[str] = []
    for family in font_families:
        expanded_families.extend(expand_family_aliases(family))

    bold_requested = normalize_key(font_weight) == "bold"

    for family in expanded_families:
        token = normalize_font_token(family)

        if bold_requested:
            for path, stem in indexed:
                if stem == token and ("bold" in stem or "demi" in stem or "black" in stem):
                    return path
            for path, stem in indexed:
                if token and token in stem and ("bold" in stem or "demi" in stem or "black" in stem):
                    return path

        for path, stem in indexed:
            if stem == token:
                return path
        for path, stem in indexed:
            if token and token in stem:
                return path
    return None


def load_font(
    font_size: int,
    font_path: str | None,
    font_families: list[str],
    font_weight: str = "normal",
) -> Any:
    image_font = importlib.import_module("PIL.ImageFont")

    if font_path:
        return image_font.truetype(font_path, font_size)

    resolved = find_font_path_from_families(font_families, font_weight)
    if resolved is None:
        raise RuntimeError(
            "Could not resolve a TrueType/OpenType font from font-family list: "
            + ", ".join(font_families)
        )
    return image_font.truetype(str(resolved), font_size)


def wrap_text_to_width(draw_obj: Any, text: str, font_obj: Any, max_width: int) -> list[str]:
    words = text.split()
    if not words:
        return []

    wrapped: list[str] = []
    current = words[0]

    for word in words[1:]:
        candidate = f"{current} {word}"
        bbox = draw_obj.textbbox((0, 0), candidate, font=font_obj)
        candidate_width = max(0, bbox[2] - bbox[0])
        if candidate_width <= max_width:
            current = candidate
        else:
            wrapped.append(current)
            current = word

    wrapped.append(current)
    return wrapped


def parse_aspect_ratio(value: str) -> tuple[int, int]:
    text = value.strip()
    match = re.fullmatch(r"(\d+)\s*:\s*(\d+)", text)
    if not match:
        raise ValueError("aspect-ratio must be in W:H form (for example: 16:9)")

    w = int(match.group(1))
    h = int(match.group(2))
    if w <= 0 or h <= 0:
        raise ValueError("aspect-ratio values must be positive")

    g = math.gcd(w, h)
    return w // g, h // g


def parse_positive_int(value: Any, label: str) -> int:
    try:
        out = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a positive integer") from None
    if out <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return out


def resolve_target_resolution(
    aspect_raw: Any,
    pixel_width_raw: Any,
    pixel_height_raw: Any,
    *,
    context_label: str,
) -> tuple[int, int]:
    provided = sum(
        value is not None and str(value).strip() != ""
        for value in [aspect_raw, pixel_width_raw, pixel_height_raw]
    )
    if provided != 2:
        raise ValueError(
            f"{context_label} requires exactly two of: aspect-ratio, pixel-width, pixel-height"
        )

    aspect: tuple[int, int] | None = None
    pixel_width: int | None = None
    pixel_height: int | None = None

    if aspect_raw is not None and str(aspect_raw).strip() != "":
        aspect = parse_aspect_ratio(str(aspect_raw))
    if pixel_width_raw is not None and str(pixel_width_raw).strip() != "":
        pixel_width = parse_positive_int(pixel_width_raw, "pixel-width")
    if pixel_height_raw is not None and str(pixel_height_raw).strip() != "":
        pixel_height = parse_positive_int(pixel_height_raw, "pixel-height")

    if aspect and pixel_width is not None and pixel_height is None:
        pixel_height = max(1, round(pixel_width * aspect[1] / aspect[0]))
    elif aspect and pixel_height is not None and pixel_width is None:
        pixel_width = max(1, round(pixel_height * aspect[0] / aspect[1]))

    if pixel_width is None or pixel_height is None:
        raise ValueError(
            f"could not resolve {context_label}; check aspect-ratio/pixel-width/pixel-height"
        )

    return pixel_width, pixel_height


def resolve_projection_reference_resolution(border: dict[str, Any]) -> tuple[int, int]:
    projection_aspect_raw = border.get("projection-aspect-ratio", border.get("projection_aspect_ratio"))
    projection_pixel_width_raw = border.get(
        "projection-pixel-width",
        border.get("projection_pixel_width", border.get("projection-width", border.get("projection_width"))),
    )
    projection_pixel_height_raw = border.get(
        "projection-pixel-height",
        border.get("projection_pixel_height", border.get("projection-height", border.get("projection_height"))),
    )

    prefixed_provided = sum(
        value is not None and str(value).strip() != ""
        for value in [projection_aspect_raw, projection_pixel_width_raw, projection_pixel_height_raw]
    )
    if prefixed_provided == 2:
        return resolve_target_resolution(
            projection_aspect_raw,
            projection_pixel_width_raw,
            projection_pixel_height_raw,
            context_label="projection reference",
        )
    if prefixed_provided != 0:
        raise ValueError(
            "projection reference requires exactly two of: "
            "projection-aspect-ratio, projection-pixel-width, projection-pixel-height"
        )

    # Backward-compatible fallback to non-prefixed keys.
    aspect_raw = border.get("aspect-ratio", border.get("aspect_ratio"))
    pixel_width_raw = border.get("pixel-width", border.get("pixel_width"))
    pixel_height_raw = border.get("pixel-height", border.get("pixel_height"))
    unprefixed_provided = sum(
        value is not None and str(value).strip() != ""
        for value in [aspect_raw, pixel_width_raw, pixel_height_raw]
    )
    if unprefixed_provided == 2:
        return resolve_target_resolution(
            aspect_raw,
            pixel_width_raw,
            pixel_height_raw,
            context_label="projection reference",
        )
    if unprefixed_provided != 0:
        raise ValueError(
            "projection reference requires exactly two of: "
            "aspect-ratio, pixel-width, pixel-height"
        )

    # Default projector/display reference for consistent apparent typography.
    return (1920, 1080)


def reference_font_size_for_resolution(width: int, height: int, adjust: float = 1.0) -> int:
    size = SCREEN_FONT_SIZE_PRESETS.get((width, height))
    if size is None:
        size = max(12, round(height * 0.028))
    adjusted = round(size * adjust)
    return max(8, adjusted)


def estimate_image_footer_canvas_size(image_width: int, image_height: int, render_plan: dict[str, Any]) -> tuple[int, int]:
    side_border = int(render_plan["border_thickness"])
    top_border = int(render_plan["border_thickness"])
    footer_height = int(render_plan["footer_height"])
    canvas_width = image_width + (2 * side_border)
    canvas_height = top_border + image_height + footer_height
    return canvas_width, canvas_height


def compute_projection_consistent_base_font_size(
    image_mod: Any,
    image_draw_mod: Any,
    border: dict[str, Any],
    line_specs: list[dict[str, Any]],
    metadata: dict[str, str],
    image_width: int,
    image_height: int,
    missing_value: str,
    projection_width: int,
    projection_height: int,
    reference_font_px: int,
) -> int:
    # Initial estimate from fitting the source image bounds alone.
    guess = max(8, round(reference_font_px * max(image_width / projection_width, image_height / projection_height)))

    for _ in range(8):
        plan = build_render_segments(
            image_mod,
            image_draw_mod,
            border,
            line_specs,
            metadata,
            image_width,
            missing_value,
            base_font_size_override=guess,
        )
        canvas_w, canvas_h = estimate_image_footer_canvas_size(image_width, image_height, plan)
        fit_scale = min(projection_width / canvas_w, projection_height / canvas_h)
        if fit_scale <= 0:
            break

        apparent_px = guess * fit_scale
        if abs(apparent_px - reference_font_px) < 0.25:
            break

        ideal = max(8, round(reference_font_px / fit_scale))
        if abs(ideal - guess) <= 1:
            guess = ideal
            break

        guess = max(8, round((guess + ideal) / 2))

    return guess


def build_render_segments(
    image_mod: Any,
    image_draw_mod: Any,
    border: dict[str, Any],
    line_specs: list[dict[str, Any]],
    metadata: dict[str, str],
    content_width: int,
    missing_value: str,
    base_font_size_override: int | None = None,
) -> dict[str, Any]:
    line_chars = parse_float(border.get("line-chars", border.get("line_chars")), 95.0)
    min_font_points = parse_float(border.get("min-font-points", border.get("min_font_points")), 8.0)
    render_dpi = parse_float(border.get("render-dpi", border.get("render_dpi")), 300.0)
    horizontal_padding_ratio = parse_float(
        border.get("horizontal-padding-ratio", border.get("horizontal_padding_ratio")),
        0.03,
    )
    line_spacing_ratio = parse_float(
        border.get("line-spacing-ratio", border.get("line_spacing_ratio")),
        0.30,
    )
    vertical_padding_lines = parse_float(
        border.get("vertical-padding-lines", border.get("vertical_padding_lines")),
        0.45,
    )
    border_lines = parse_float(border.get("border-lines", border.get("border_lines")), 1.0)

    font_path = border.get("font-path", border.get("font_path"))
    font_family_value = border.get("font-family", border.get("font_family", "Arial, Sans-Serif"))
    font_families = parse_font_family_list(font_family_value)
    font_weight = str(border.get("font-weight", border.get("font_weight", "normal"))).strip().lower()
    if font_weight not in {"normal", "bold"}:
        raise ValueError("font-weight must be one of: normal, bold")
    if not font_families and not font_path:
        font_families = ["Arial", "Sans-Serif"]

    horizontal_padding = max(8, round(content_width * horizontal_padding_ratio))
    usable_width = max(20, content_width - (2 * horizontal_padding))

    probe_size = 100
    probe_font = load_font(
        probe_size,
        str(font_path) if font_path else None,
        font_families,
        font_weight=font_weight,
    )
    dummy = image_mod.new("RGB", (max(40, content_width), 20), color="#FFFFFF")
    draw_dummy = image_draw_mod.Draw(dummy)
    n_bbox = draw_dummy.textbbox((0, 0), "n", font=probe_font)
    n_width_at_probe = max(1, n_bbox[2] - n_bbox[0])
    unit_per_px = n_width_at_probe / probe_size

    min_font_px = max(10, round((min_font_points * render_dpi) / 72.0))
    if line_chars <= 0:
        line_chars = 95.0
    computed_font_px = round(usable_width / (line_chars * unit_per_px))
    base_font_size = max(min_font_px, computed_font_px)

    if base_font_size_override is not None:
        base_font_size = max(8, int(base_font_size_override))

    rendered_segments: list[dict[str, Any]] = []
    line_height_for_border = 0

    for spec in line_specs:
        if not should_render_line(spec, metadata):
            continue

        text_template = str(spec.get("text", "")).strip()
        if not text_template:
            continue

        rendered = substitute_vars(text_template, metadata, missing_value)
        if not line_has_render_content(rendered):
            continue

        scale = parse_float(spec.get("scale"), 1.0)
        font_size = max(8, round(base_font_size * scale))
        font = load_font(
            font_size,
            str(font_path) if font_path else None,
            font_families,
            font_weight=font_weight,
        )

        bbox_n = draw_dummy.textbbox((0, 0), "Ag", font=font)
        this_line_height = max(1, bbox_n[3] - bbox_n[1])
        line_height_for_border = max(line_height_for_border, this_line_height)

        wrap_value = spec.get("wrap")
        if wrap_value is not None:
            wrap_float = parse_float(wrap_value, 1.0)
            if wrap_float <= 1.0:
                wrap_width = max(20, round(usable_width * wrap_float))
            else:
                wrap_width = max(20, round(wrap_float))
            wrapped_lines = wrap_text_to_width(draw_dummy, rendered, font, wrap_width)
        else:
            wrapped_lines = [rendered]

        for subline in wrapped_lines:
            if not line_has_render_content(subline):
                continue
            bbox = draw_dummy.textbbox((0, 0), subline, font=font)
            seg_w = max(0, bbox[2] - bbox[0])
            seg_h = max(1, bbox[3] - bbox[1])
            rendered_segments.append(
                {
                    "text": subline,
                    "font": font,
                    "width": seg_w,
                    "height": seg_h,
                }
            )
            line_height_for_border = max(line_height_for_border, seg_h)

    if line_height_for_border <= 0:
        line_height_for_border = max(1, base_font_size)

    line_spacing = max(2, round(line_height_for_border * line_spacing_ratio))
    text_block_height = sum(int(seg["height"]) for seg in rendered_segments)
    if rendered_segments:
        text_block_height += line_spacing * (len(rendered_segments) - 1)

    if rendered_segments:
        footer_vertical_padding = max(4, round(line_height_for_border * vertical_padding_lines))
        footer_height = text_block_height + (2 * footer_vertical_padding)
    else:
        footer_vertical_padding = 0
        footer_height = 0

    border_thickness = max(4, round(line_height_for_border * border_lines))

    return {
        "rendered_segments": rendered_segments,
        "horizontal_padding": horizontal_padding,
        "line_spacing": line_spacing,
        "footer_vertical_padding": footer_vertical_padding,
        "footer_height": footer_height,
        "border_thickness": border_thickness,
    }


def draw_segments(
    draw_obj: Any,
    segments: list[dict[str, Any]],
    align: str,
    left_x: int,
    content_width: int,
    horizontal_padding: int,
    y_start: int,
    line_spacing: int,
    text_color: str,
) -> None:
    y = y_start
    for seg in segments:
        text = str(seg["text"])
        text_width = int(seg["width"])
        text_height = int(seg["height"])
        font = seg["font"]

        if align == "left":
            x = left_x + horizontal_padding
        elif align == "right":
            x = left_x + content_width - horizontal_padding - text_width
        else:
            x = left_x + (content_width - text_width) // 2

        draw_obj.text((x, y), text, fill=text_color, font=font)
        y += text_height + line_spacing


def save_jpeg_with_metadata(canvas: Any, output_path: Path, source_info: dict[str, Any]) -> None:
    save_kwargs: dict[str, Any] = {
        "format": "JPEG",
        "quality": 100,
        "subsampling": 0,
    }

    exif_bytes = source_info.get("exif")
    if exif_bytes:
        save_kwargs["exif"] = exif_bytes

    icc_profile = source_info.get("icc_profile")
    if icc_profile:
        save_kwargs["icc_profile"] = icc_profile

    dpi = source_info.get("dpi")
    if dpi:
        save_kwargs["dpi"] = dpi

    canvas.save(output_path, **save_kwargs)


def annotate_image(input_path: Path, output_path: Path, profile: dict[str, Any]) -> list[Path]:
    border = profile["border"]
    line_specs = profile["line_specs"]

    background_color = str(border.get("background-color", border.get("background_color", "#FFFFFF")))
    text_color = str(border.get("text-color", border.get("text_color", "#000000")))
    align = str(border.get("justify", border.get("align", "left"))).lower()
    missing_value = str(profile.get("missing_value", ""))
    layout = str(border.get("layout", "image-footer")).strip().lower()

    if align not in {"left", "center", "right"}:
        raise ValueError("justify must be one of: left, center, right")
    if layout and layout != "image-footer":
        raise ValueError("layout must be image-footer (screen-footer mode was removed)")

    image_mod = importlib.import_module("PIL.Image")
    image_draw_mod = importlib.import_module("PIL.ImageDraw")

    source_image = image_mod.open(input_path)
    source_info = dict(source_image.info)
    image = source_image.convert("RGB")
    source_image.close()
    width, height = image.size

    variables = collect_variables(line_specs)
    tags = exiftool_tags_for_variables(variables)
    exif_values = run_exiftool_json(input_path, tags)
    metadata = build_display_metadata(variables, exif_values)

    written: list[Path] = []

    projection_w, projection_h = resolve_projection_reference_resolution(border)

    font_size_adjust = parse_float(border.get("font-size-adjust", border.get("font_size_adjust")), 1.0)
    if font_size_adjust <= 0:
        raise ValueError("font-size-adjust must be > 0")

    reference_font_size = reference_font_size_for_resolution(projection_w, projection_h, font_size_adjust)
    base_font_size_override = compute_projection_consistent_base_font_size(
        image_mod,
        image_draw_mod,
        border,
        line_specs,
        metadata,
        width,
        height,
        missing_value,
        projection_w,
        projection_h,
        reference_font_size,
    )

    render_plan = build_render_segments(
        image_mod,
        image_draw_mod,
        border,
        line_specs,
        metadata,
        width,
        missing_value,
        base_font_size_override=base_font_size_override,
    )

    side_border = int(render_plan["border_thickness"])
    top_border = int(render_plan["border_thickness"])
    footer_height = int(render_plan["footer_height"])
    footer_vertical_padding = int(render_plan["footer_vertical_padding"])

    canvas_width = width + (2 * side_border)
    canvas_height = top_border + height + footer_height
    canvas = image_mod.new("RGB", (canvas_width, canvas_height), color=background_color)
    canvas.paste(image, (side_border, top_border))

    draw = image_draw_mod.Draw(canvas)
    y_start = top_border + height + footer_vertical_padding
    draw_segments(
        draw,
        render_plan["rendered_segments"],
        align,
        side_border,
        width,
        int(render_plan["horizontal_padding"]),
        y_start,
        int(render_plan["line_spacing"]),
        text_color,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    save_jpeg_with_metadata(canvas, output_path, source_info)
    written.append(output_path)

    return written


def main() -> int:
    script_dir = Path(__file__).resolve().parent
    default_profile_path = script_dir / "profiles" / "annotation-projection-consistent.annotate"

    parser = argparse.ArgumentParser(
        description=(
            "Create a Polaroid-style annotation border on a JPEG using Pillow. "
            "Uses plain-text templates with $variables and @directives only."
        ),
        epilog=(
            "Example:\n"
            "  python3 annotate-batch/bin/annotate-border.py input.jpg output.jpg --profile annotate-batch/bin/profiles/annotation-projection-consistent.annotate\n"
            "\n"
            "Template notes:\n"
            "  @layout image-footer (optional; this is the only supported layout)\n"
            "  @projection-aspect-ratio 16:9\n"
            "  @projection-pixel-width 1920\n"
            "  @projection-pixel-height 1080\n"
            "  @justify left|center|right\n"
            "  @font-weight normal|bold\n"
            "  optional inline segments: [-- $license]\n"
            "\n"
            "Projection reference defines consistent on-screen text size and uses either:\n"
            "  projection-aspect-ratio/projection-pixel-width/projection-pixel-height\n"
            "or fallback keys:\n"
            "  aspect-ratio/pixel-width/pixel-height\n"
            "Provide exactly two keys in one set; the third is derived.\n"
            "If none are provided, 1920x1080 (16:9) is assumed.\n"
            "\n"
            "Base font size is calibrated against projection fit scaling.\n"
            "Optional: @font-size-adjust 0.95 (or 1.05) to nudge all screen sizes.\n"
            "line-chars/min-font-points tune baseline footer layout behavior.\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("input_image", help="Path to input JPEG image")
    parser.add_argument("output_image", help="Path to output JPEG image")
    parser.add_argument(
        "--profile",
        default=str(default_profile_path),
        help=(
            "Path to plain-text annotate template "
            "(default: script-dir/profiles/annotation-projection-consistent.annotate)"
        ),
    )

    args = parser.parse_args()

    input_path = Path(args.input_image).expanduser().resolve()
    output_path = Path(args.output_image).expanduser().resolve()
    profile_path = Path(args.profile).expanduser().resolve()

    if not input_path.exists() or not input_path.is_file():
        parser.error(f"Input image not found: {input_path}")
    if input_path.suffix.lower() not in {".jpg", ".jpeg"}:
        parser.error("Input image must be a JPEG (.jpg/.jpeg)")
    if not profile_path.exists() or not profile_path.is_file():
        parser.error(f"Profile file not found: {profile_path}")

    try:
        profile = load_profile(profile_path)
        outputs = annotate_image(input_path, output_path, profile)
    except (ValueError, RuntimeError, OSError, json.JSONDecodeError, TypeError) as exc:
        parser.error(str(exc))

    for out in outputs:
        print(f"Wrote annotated image: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
