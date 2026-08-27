#!/usr/bin/env python3

from __future__ import annotations

import argparse
import importlib
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


VAR_RE = re.compile(r"\$([A-Za-z0-9_-]+)")
OPTIONAL_SEGMENT_RE = re.compile(r"\[([^\[\]]*)\]")
ASPECT_RE = re.compile(r"^(\d+)\s*:\s*(\d+)$")
ANY_RE = re.compile(r"^any\((.*)\)$", re.IGNORECASE)

# Calibrated baseline: @font-size 1.0 ~= readable default across common images.
BASE_FONT_RATIO = 0.015

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
}


@dataclass
class LayoutSpec:
    mode: str  # projection-consistent | width-consistent
    primary_axis: str  # width | height (used for width-consistent)
    projection_ratio: tuple[int, int] | None  # used for projection-consistent


@dataclass
class BoxSpec:
    region: str  # interior | exterior
    edge: str  # top | bottom
    align: str  # left | center | right
    scale: float
    when_any: list[str]
    lines: list[str]


@dataclass
class ProfileV2:
    layout: LayoutSpec
    font_family: str
    font_size: float
    line_spacing: float
    frame_width: float
    padding_exterior: float
    padding_interior: float
    background_color: str
    text_color_exterior: str
    text_color_interior: str
    boxes: list[BoxSpec]


@dataclass
class BoxLayoutResult:
    spec: BoxSpec
    font: Any
    box_x: int
    box_y: int
    box_w: int
    box_h: int
    lines: list[str]
    line_widths: list[int]
    line_heights: list[int]
    line_spacing_px: int
    text_color: str


def normalize_key(value: str) -> str:
    return value.strip().lower()


def parse_aspect_ratio(text: str) -> tuple[int, int]:
    m = ASPECT_RE.fullmatch(text.strip())
    if not m:
        raise ValueError(f"Invalid aspect ratio: {text!r} (expected W:H)")
    w = int(m.group(1))
    h = int(m.group(2))
    if w <= 0 or h <= 0:
        raise ValueError("Aspect ratio values must be positive")
    return w, h


def parse_when_any(expr: str) -> list[str]:
    m = ANY_RE.fullmatch(expr.strip())
    if not m:
        raise ValueError(f"Invalid when expression: {expr!r}; expected any(a,b,c)")
    inner = m.group(1).strip()
    if not inner:
        return []
    return [normalize_key(x) for x in inner.split(",") if x.strip()]


def parse_box_header(text: str) -> BoxSpec:
    # @box <region> <edge> <align> [scale <float>] [when any(...)]
    parts = text.split()
    if len(parts) < 4 or parts[0] != "@box":
        raise ValueError(f"Invalid box header: {text!r}")

    region = normalize_key(parts[1])
    edge = normalize_key(parts[2])
    align = normalize_key(parts[3])

    if region not in {"interior", "exterior"}:
        raise ValueError(f"Invalid box region: {region}")
    if edge not in {"top", "bottom"}:
        raise ValueError(f"Invalid box edge: {edge}")
    if align not in {"left", "center", "right"}:
        raise ValueError(f"Invalid box align: {align}")

    scale = 1.0
    when_any: list[str] = []

    i = 4
    while i < len(parts):
        token = normalize_key(parts[i])
        if token == "scale":
            if i + 1 >= len(parts):
                raise ValueError(f"Missing scale value in: {text!r}")
            scale = float(parts[i + 1])
            i += 2
            continue
        if token == "when":
            expr = " ".join(parts[i + 1 :]).strip()
            when_any = parse_when_any(expr)
            break
        raise ValueError(f"Unknown box option {token!r} in: {text!r}")

    if scale <= 0:
        raise ValueError("Box scale must be > 0")

    return BoxSpec(region=region, edge=edge, align=align, scale=scale, when_any=when_any, lines=[])


def parse_profile_v2(profile_path: Path) -> ProfileV2:
    globals_raw: dict[str, Any] = {}
    boxes: list[BoxSpec] = []
    current_box: BoxSpec | None = None

    for line_no, raw in enumerate(profile_path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue

        if stripped.startswith("@box "):
            box = parse_box_header(stripped)
            boxes.append(box)
            current_box = box
            continue

        if stripped.startswith("@"):
            if current_box is not None:
                raise ValueError(f"Global directive after @box is not allowed in V2 (line {line_no})")

            if stripped.startswith("@profile-version "):
                globals_raw["profile-version"] = stripped.split(maxsplit=1)[1].strip()
            elif stripped.startswith("@layout "):
                globals_raw["layout"] = stripped.split(maxsplit=1)[1].strip()
            elif stripped.startswith("@font-family "):
                globals_raw["font-family"] = stripped.split(maxsplit=1)[1].strip()
            elif stripped.startswith("@font-size "):
                globals_raw["font-size"] = float(stripped.split(maxsplit=1)[1].strip())
            elif stripped.startswith("@line-spacing "):
                globals_raw["line-spacing"] = float(stripped.split(maxsplit=1)[1].strip())
            elif stripped.startswith("@frame-width "):
                globals_raw["frame-width"] = float(stripped.split(maxsplit=1)[1].strip())
            elif stripped.startswith("@padding "):
                # @padding exterior 1.0, interior 1.0
                value = stripped.split(maxsplit=1)[1].strip()
                m = re.fullmatch(r"exterior\s+([0-9]*\.?[0-9]+)\s*,\s*interior\s+([0-9]*\.?[0-9]+)", value)
                if not m:
                    raise ValueError(f"Invalid @padding format on line {line_no}")
                globals_raw["padding-exterior"] = float(m.group(1))
                globals_raw["padding-interior"] = float(m.group(2))
            elif stripped.startswith("@background-color "):
                globals_raw["background-color"] = stripped.split(maxsplit=1)[1].strip()
            elif stripped.startswith("@text-color "):
                value = stripped.split(maxsplit=1)[1].strip()
                parts = value.split(maxsplit=1)
                if len(parts) != 2 or normalize_key(parts[0]) not in {"interior", "exterior"}:
                    raise ValueError(f"Invalid @text-color format on line {line_no}")
                globals_raw[f"text-color-{normalize_key(parts[0])}"] = parts[1].strip()
            else:
                raise ValueError(f"Unknown directive on line {line_no}: {stripped}")
            continue

        if current_box is None:
            raise ValueError(f"Render line outside @box block on line {line_no}")
        current_box.lines.append(raw.rstrip())

    if str(globals_raw.get("profile-version", "")) != "2":
        raise ValueError("V2 profile requires @profile-version 2")

    layout_raw = str(globals_raw.get("layout", "")).strip()
    if not layout_raw:
        raise ValueError("V2 profile requires @layout")

    layout_parts = layout_raw.split()
    if len(layout_parts) == 2 and normalize_key(layout_parts[0]) == "image":
        axis = normalize_key(layout_parts[1])
        if axis not in {"width", "height"}:
            raise ValueError("@layout image must be: @layout image width|height")
        layout = LayoutSpec(mode="width-consistent", primary_axis=axis, projection_ratio=None)
    elif len(layout_parts) == 2 and normalize_key(layout_parts[0]) == "projection":
        ratio = parse_aspect_ratio(layout_parts[1])
        layout = LayoutSpec(mode="projection-consistent", primary_axis="width", projection_ratio=ratio)
    else:
        raise ValueError("@layout must be @layout image width|height OR @layout projection W:H")

    if not boxes:
        raise ValueError("V2 profile requires at least one @box")

    for i, b in enumerate(boxes, start=1):
        if not b.lines:
            raise ValueError(f"Box {i} has no render lines")

    font_size = float(globals_raw.get("font-size", 1.0))
    line_spacing = float(globals_raw.get("line-spacing", 0.30))
    frame_width = float(globals_raw.get("frame-width", 1.0))
    if font_size <= 0:
        raise ValueError("@font-size must be > 0")
    if frame_width < 0:
        raise ValueError("@frame-width must be >= 0")

    return ProfileV2(
        layout=layout,
        font_family=str(globals_raw.get("font-family", "Arial, Sans-Serif")),
        font_size=font_size,
        line_spacing=line_spacing,
        frame_width=frame_width,
        padding_exterior=float(globals_raw.get("padding-exterior", 1.0)),
        padding_interior=float(globals_raw.get("padding-interior", 1.0)),
        background_color=str(globals_raw.get("background-color", "#FFFFFF")),
        text_color_exterior=str(globals_raw.get("text-color-exterior", "#000000")),
        text_color_interior=str(globals_raw.get("text-color-interior", "#000000")),
        boxes=boxes,
    )


def parse_font_family_list(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


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


def find_font_path_from_families(font_families: list[str]) -> Path | None:
    font_dirs = [Path("/System/Library/Fonts"), Path("/Library/Fonts"), Path("~/Library/Fonts").expanduser()]
    font_files: list[Path] = []
    for directory in font_dirs:
        if not directory.exists():
            continue
        for path in directory.rglob("*"):
            if path.is_file() and path.suffix.lower() in {".ttf", ".otf", ".ttc"}:
                font_files.append(path)

    indexed = [(path, normalize_font_token(path.stem)) for path in font_files]
    expanded: list[str] = []
    for fam in font_families:
        expanded.extend(expand_family_aliases(fam))

    for family in expanded:
        token = normalize_font_token(family)
        for path, stem in indexed:
            if stem == token:
                return path
        for path, stem in indexed:
            if token and token in stem:
                return path
    return None


def load_font(font_size: int, font_families: list[str]) -> Any:
    image_font = importlib.import_module("PIL.ImageFont")
    resolved = find_font_path_from_families(font_families)
    if resolved is None:
        raise RuntimeError("Could not resolve font from family list: " + ", ".join(font_families))
    return image_font.truetype(str(resolved), font_size)


def exiftool_tags_for_variables(variables: list[str]) -> list[str]:
    tags: set[str] = set()
    for var in variables:
        mapped = ATTRIBUTE_TAG_MAP.get(var)
        if mapped:
            tags.update(mapped)
    return sorted(tags)


def run_exiftool_json(image_path: Path, tags: list[str]) -> dict[str, str]:
    if not tags:
        return {}
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
            model = first_nonempty(exif_values, ["EXIF:Model", "EXIF:CameraModelName", "XMP-exif:Model", "XMP-tiff:Model"])
            display[var] = " ".join(x for x in [make, model] if x).strip()
            continue

        tags = ATTRIBUTE_TAG_MAP.get(var)
        if tags:
            value = first_nonempty(exif_values, tags)
            if var == "capture-date":
                value = normalize_capture_date(value)
            display[var] = value
        else:
            display[var] = ""
    return display


def substitute_vars(template: str, metadata: dict[str, str]) -> str:
    def repl(match: re.Match[str]) -> str:
        key = normalize_key(match.group(1))
        return metadata.get(key, "")

    def repl_optional(match: re.Match[str]) -> str:
        inner = match.group(1)
        vars_inner = [normalize_key(v) for v in VAR_RE.findall(inner)]
        if vars_inner and any(not metadata.get(k, "").strip() for k in vars_inner):
            return ""
        return VAR_RE.sub(repl, inner)

    text = OPTIONAL_SEGMENT_RE.sub(repl_optional, template)
    text = VAR_RE.sub(repl, text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def line_has_render_content(text: str) -> bool:
    return any(ch.isalnum() for ch in text)


def wrap_text_to_width(draw_obj: Any, text: str, font_obj: Any, max_width: int) -> list[str]:
    words = text.split()
    if not words:
        return []
    wrapped: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        bbox = draw_obj.textbbox((0, 0), candidate, font=font_obj)
        width = max(0, bbox[2] - bbox[0])
        if width <= max_width:
            current = candidate
        else:
            wrapped.append(current)
            current = word
    wrapped.append(current)
    return wrapped


def collect_variables_from_profile(profile: ProfileV2) -> list[str]:
    out: set[str] = set()
    for box in profile.boxes:
        for name in box.when_any:
            out.add(normalize_key(name))
        for line in box.lines:
            for match in VAR_RE.findall(line):
                out.add(normalize_key(match))
    return sorted(out)


def should_render_box(box: BoxSpec, metadata: dict[str, str]) -> bool:
    if not box.when_any:
        return True
    return any(bool(metadata.get(k, "").strip()) for k in box.when_any)


def save_jpeg_with_metadata(canvas: Any, output_path: Path, source_info: dict[str, Any]) -> None:
    kwargs: dict[str, Any] = {"format": "JPEG", "quality": 100, "subsampling": 0}
    exif_bytes = source_info.get("exif")
    if exif_bytes:
        kwargs["exif"] = exif_bytes
    icc = source_info.get("icc_profile")
    if icc:
        kwargs["icc_profile"] = icc
    dpi = source_info.get("dpi")
    if dpi:
        kwargs["dpi"] = dpi
    canvas.save(output_path, **kwargs)


def emit_diag(enabled: bool, msg: str) -> None:
    if enabled:
        print(msg, file=sys.stderr)


def annotate_v2(input_path: Path, output_path: Path, profile: ProfileV2, diagnostics: bool) -> None:
    image_mod = importlib.import_module("PIL.Image")
    draw_mod = importlib.import_module("PIL.ImageDraw")

    src = image_mod.open(input_path)
    source_info = dict(src.info)
    image = src.convert("RGB")
    src.close()
    image_w, image_h = image.size

    # Resolve primary dimension.
    primary_reason = ""
    if profile.layout.mode == "width-consistent":
        primary = image_w if profile.layout.primary_axis == "width" else image_h
        primary_reason = f"image {profile.layout.primary_axis}"
    else:
        assert profile.layout.projection_ratio is not None
        proj_w, proj_h = profile.layout.projection_ratio
        img_ratio = image_w / image_h
        proj_ratio = proj_w / proj_h
        if img_ratio >= proj_ratio:
            primary = image_w
            primary_reason = "projection fit: width-constrained"
        else:
            primary = image_h
            primary_reason = "projection fit: height-constrained"

    base_font_px = max(8, round(primary * BASE_FONT_RATIO * profile.font_size))
    frame_px = max(0, round(base_font_px * profile.frame_width))
    pad_exterior = max(0, round(base_font_px * profile.padding_exterior))
    pad_interior = max(0, round(base_font_px * profile.padding_interior))

    emit_diag(diagnostics, f"layout={profile.layout.mode} primary={primary} reason={primary_reason}")
    emit_diag(diagnostics, f"base_font_px={base_font_px} frame_px={frame_px} pad_ext={pad_exterior} pad_int={pad_interior}")

    canvas_w = image_w + 2 * frame_px
    canvas_h = image_h + 2 * frame_px
    image_x = frame_px
    image_y = frame_px

    zones = {
        ("exterior", "top"): (image_x, 0, image_w, frame_px),
        ("exterior", "bottom"): (image_x, image_y + image_h, image_w, frame_px),
        ("interior", "top"): (image_x, image_y, image_w, image_h),
        ("interior", "bottom"): (image_x, image_y, image_w, image_h),
    }

    variables = collect_variables_from_profile(profile)
    tags = exiftool_tags_for_variables(variables)
    exif_values = run_exiftool_json(input_path, tags)
    metadata = build_display_metadata(variables, exif_values)

    font_families = parse_font_family_list(profile.font_family)

    dummy = image_mod.new("RGB", (max(64, image_w), max(64, image_h)), color="#FFFFFF")
    draw_dummy = draw_mod.Draw(dummy)

    # stack cursors per vertical zone
    top_cursors: dict[tuple[str, str], int] = {
        ("exterior", "top"): zones[("exterior", "top")][1],
        ("interior", "top"): zones[("interior", "top")][1],
    }
    bottom_cursors: dict[tuple[str, str], int] = {
        ("exterior", "bottom"): zones[("exterior", "bottom")][1] + zones[("exterior", "bottom")][3],
        ("interior", "bottom"): zones[("interior", "bottom")][1] + zones[("interior", "bottom")][3],
    }

    placements: list[BoxLayoutResult] = []

    for idx, box in enumerate(profile.boxes, start=1):
        if not should_render_box(box, metadata):
            emit_diag(diagnostics, f"box#{idx} skipped by when")
            continue

        box_font_px = max(8, round(base_font_px * box.scale))
        font = load_font(box_font_px, font_families)
        line_spacing_px = max(1, round(box_font_px * profile.line_spacing))

        text_lines_raw: list[str] = []
        for template_line in box.lines:
            rendered = substitute_vars(template_line, metadata)
            if line_has_render_content(rendered):
                text_lines_raw.append(rendered)

        if not text_lines_raw:
            emit_diag(diagnostics, f"box#{idx} has no renderable lines")
            continue

        zone_x, zone_y, zone_w, zone_h = zones[(box.region, box.edge)]
        box_pad = pad_exterior if box.region == "exterior" else pad_interior
        max_content_w = zone_w - (2 * box_pad)
        if max_content_w <= 0:
            raise ValueError(f"Strict overflow: box#{idx} has non-positive content width in zone")

        wrapped_lines: list[str] = []
        for line in text_lines_raw:
            wrapped = wrap_text_to_width(draw_dummy, line, font, max_content_w)
            wrapped_lines.extend(wrapped)

        widths: list[int] = []
        heights: list[int] = []
        for line in wrapped_lines:
            bbox = draw_dummy.textbbox((0, 0), line, font=font)
            w = max(0, bbox[2] - bbox[0])
            h = max(1, bbox[3] - bbox[1])
            widths.append(w)
            heights.append(h)

        content_w = max(widths) if widths else 0
        content_h = sum(heights) + (line_spacing_px * max(0, len(heights) - 1))
        box_w = content_w + (2 * box_pad)
        box_h = content_h + (2 * box_pad)

        if box_w > zone_w:
            raise ValueError(f"Strict overflow: box#{idx} width {box_w}px exceeds zone width {zone_w}px")

        if box.align == "left":
            box_x = image_x
        elif box.align == "right":
            box_x = image_x + image_w - box_w
        else:
            box_x = image_x + (image_w - box_w) // 2

        key = (box.region, box.edge)
        if box.edge == "top":
            box_y = top_cursors[key]
            if box_y + box_h > zone_y + zone_h:
                raise ValueError(f"Strict overflow: box#{idx} exceeds top zone height")
            top_cursors[key] = box_y + box_h
        else:
            box_y = bottom_cursors[key] - box_h
            if box_y < zone_y:
                raise ValueError(f"Strict overflow: box#{idx} exceeds bottom zone height")
            bottom_cursors[key] = box_y

        color = profile.text_color_exterior if box.region == "exterior" else profile.text_color_interior

        emit_diag(
            diagnostics,
            f"box#{idx} zone={box.region}/{box.edge}/{box.align} box=({box_x},{box_y},{box_w},{box_h}) lines={len(wrapped_lines)}",
        )

        placements.append(
            BoxLayoutResult(
                spec=box,
                font=font,
                box_x=box_x,
                box_y=box_y,
                box_w=box_w,
                box_h=box_h,
                lines=wrapped_lines,
                line_widths=widths,
                line_heights=heights,
                line_spacing_px=line_spacing_px,
                text_color=color,
            )
        )

    # Render in declaration order (later boxes on top).
    canvas = image_mod.new("RGB", (canvas_w, canvas_h), color=profile.background_color)
    canvas.paste(image, (image_x, image_y))
    draw = draw_mod.Draw(canvas)

    for placed in placements:
        box = placed.spec
        box_pad = pad_exterior if box.region == "exterior" else pad_interior
        y = placed.box_y + box_pad
        content_w = max(placed.line_widths) if placed.line_widths else 0
        for line, lw, lh in zip(placed.lines, placed.line_widths, placed.line_heights):
            if box.align == "left":
                x = placed.box_x + box_pad
            elif box.align == "right":
                x = placed.box_x + placed.box_w - box_pad - lw
            else:
                # center inside text box content area
                x = placed.box_x + box_pad + (content_w - lw) // 2
            draw.text((x, y), line, fill=placed.text_color, font=placed.font)
            y += lh + placed.line_spacing_px

    output_path.parent.mkdir(parents=True, exist_ok=True)
    save_jpeg_with_metadata(canvas, output_path, source_info)


def main() -> int:
    script_dir = Path(__file__).resolve().parent
    default_profile = script_dir / "profiles" / "annotation-v2-demo.annotate"

    parser = argparse.ArgumentParser(
        description="Annotate JPEG with V2 box-model profiles (@profile-version 2).",
        epilog=(
            "Example:\n"
            "  python3 annotate-batch/bin/annotate-border-v2.py input.jpg output.jpg "
            "--profile annotate-batch/bin/profiles/annotation-v2-demo.annotate\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("input_image", help="Path to input JPEG image")
    parser.add_argument("output_image", help="Path to output JPEG image")
    parser.add_argument(
        "--profile",
        default=str(default_profile),
        help="Path to V2 profile (default: script-dir/profiles/annotation-v2-demo.annotate)",
    )
    parser.add_argument(
        "--diagnostics",
        action="store_true",
        help="Emit layout diagnostics to STDERR",
    )

    args = parser.parse_args()

    input_path = Path(args.input_image).expanduser().resolve()
    output_path = Path(args.output_image).expanduser().resolve()
    profile_path = Path(args.profile).expanduser().resolve()

    if not input_path.exists() or not input_path.is_file():
        parser.error(f"Input image not found: {input_path}")
    if input_path.suffix.lower() not in {".jpg", ".jpeg"}:
        parser.error("Input image must be JPEG (.jpg/.jpeg)")
    if not profile_path.exists() or not profile_path.is_file():
        parser.error(f"Profile file not found: {profile_path}")

    try:
        profile = parse_profile_v2(profile_path)
        annotate_v2(input_path, output_path, profile, diagnostics=args.diagnostics)
    except (ValueError, RuntimeError, OSError, json.JSONDecodeError, TypeError) as exc:
        parser.error(str(exc))

    print(f"Wrote annotated image: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
