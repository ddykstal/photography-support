#!/usr/bin/env python3

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any


VAR_RE = re.compile(r"\$([A-Za-z0-9_-]+)")
OPTIONAL_SEGMENT_RE = re.compile(r"\[([^\[\]]*)\]")
ASPECT_RE = re.compile(r"^(\d+)\s*:\s*(\d+)$")
ANY_RE = re.compile(r"^any\((.*)\)$", re.IGNORECASE)

# Calibrated baseline: @font-size 1.0 ~= readable default across common images.
BASE_FONT_RATIO = 0.015

CAPTURE_DATETIME_TAGS = ["EXIF:DateTimeOriginal", "XMP-exif:DateTimeOriginal", "XMP-xmp:CreateDate"]
CAPTURE_TZ_TAGS = ["EXIF:OffsetTimeOriginal", "XMP-exif:OffsetTimeOriginal", "EXIF:TimeZoneOffset"]
GPS_LAT_TAGS = ["EXIF:GPSLatitude", "XMP-exif:GPSLatitude", "Composite:GPSLatitude"]
GPS_LON_TAGS = ["EXIF:GPSLongitude", "XMP-exif:GPSLongitude", "Composite:GPSLongitude"]


ATTRIBUTE_TAG_MAP: dict[str, list[str]] = {
    "identifier": ["XMP-dc:Identifier"],
    "title": ["XMP-dc:Title"],
    "caption": ["XMP-dc:Description"],
    "copyright": ["XMP-dc:Rights", "IPTC:CopyrightNotice"],
    "license": ["XMP-xmpRights:UsageTerms"],
    "capture-date": [*CAPTURE_DATETIME_TAGS, *CAPTURE_TZ_TAGS],
    "capture-time": [*CAPTURE_DATETIME_TAGS, *CAPTURE_TZ_TAGS],
    "capture-datetime": [*CAPTURE_DATETIME_TAGS, *CAPTURE_TZ_TAGS],
    "capture-tz": [*CAPTURE_DATETIME_TAGS, *CAPTURE_TZ_TAGS],
    "location": [*GPS_LAT_TAGS, *GPS_LON_TAGS],
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
    # Options may appear in either order.
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
            if i + 1 >= len(parts):
                raise ValueError(f"Missing when expression in: {text!r}")

            expr_tokens: list[str] = []
            j = i + 1
            while j < len(parts) and normalize_key(parts[j]) != "scale":
                expr_tokens.append(parts[j])
                j += 1

            expr = " ".join(expr_tokens).strip()
            when_any = parse_when_any(expr)
            i = j
            continue

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


@lru_cache(maxsize=1)
def indexed_system_fonts() -> tuple[tuple[str, str], ...]:
    font_dirs = [Path("/System/Library/Fonts"), Path("/Library/Fonts"), Path("~/Library/Fonts").expanduser()]
    indexed: list[tuple[str, str]] = []

    for directory in font_dirs:
        if not directory.exists():
            continue
        for path in directory.rglob("*"):
            if path.is_file() and path.suffix.lower() in {".ttf", ".otf", ".ttc"}:
                indexed.append((str(path), normalize_font_token(path.stem)))

    return tuple(indexed)


@lru_cache(maxsize=128)
def resolve_font_path_for_families(families_key: tuple[str, ...]) -> str | None:
    indexed = indexed_system_fonts()

    expanded: list[str] = []
    for fam in families_key:
        expanded.extend(expand_family_aliases(fam))

    for family in expanded:
        token = normalize_font_token(family)
        for path_str, stem in indexed:
            if stem == token:
                return path_str
        for path_str, stem in indexed:
            if token and token in stem:
                return path_str

    return None


def find_font_path_from_families(font_families: list[str]) -> Path | None:
    families_key = tuple(font_families)
    resolved = resolve_font_path_for_families(families_key)
    return Path(resolved) if resolved else None


@lru_cache(maxsize=256)
def load_font_from_path(font_path: str, font_size: int) -> Any:
    image_font = importlib.import_module("PIL.ImageFont")
    return image_font.truetype(font_path, font_size)


def load_font(font_size: int, font_families: list[str]) -> Any:
    resolved = find_font_path_from_families(font_families)
    if resolved is None:
        raise RuntimeError("Could not resolve font from family list: " + ", ".join(font_families))
    return load_font_from_path(str(resolved), int(font_size))


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


def normalize_capture_parts(value: str) -> tuple[str, str, str]:
    if not value:
        return "", "", ""

    text = value.strip()

    # Common EXIF/ISO forms, with optional timezone and optional AM/PM.
    # Examples:
    # - 2024:03:31 18:07:05
    # - 2024:03:31 18:07:05-07:00
    # - 2024-03-31T18:07:05Z
    # - 2024:03:31 6:07:05 PM
    pattern = re.compile(
        r"(?P<y>\d{4})[:\-](?P<m>\d{2})[:\-](?P<d>\d{2})"
        r"(?:[ T])"
        r"(?P<h>\d{1,2}):(?P<mi>\d{2}):(?P<s>\d{2})"
        r"(?:\.\d+)?"
        r"(?:\s*(?P<ampm>AM|PM|am|pm))?"
        r"(?:\s*(?P<tz>Z|[+\-]\d{2}:?\d{2}))?"
    )

    m = pattern.search(text)
    if m:
        yyyy = m.group("y")
        mm = m.group("m")
        dd = m.group("d")

        hour = int(m.group("h"))
        minute = m.group("mi")
        second = m.group("s")

        ampm = m.group("ampm")
        if ampm:
            ampm_upper = ampm.upper()
            if ampm_upper == "AM":
                hour = 0 if hour == 12 else hour
            elif ampm_upper == "PM":
                hour = 12 if hour == 12 else hour + 12

        if hour < 0 or hour > 23:
            return "", "", ""

        date_out = f"{yyyy}-{mm}-{dd}"
        time_out = f"{hour:02d}:{minute}:{second}"

        tz_raw = (m.group("tz") or "").strip()
        tz_out = ""
        if tz_raw:
            if tz_raw.upper() == "Z":
                tz_out = "Z"
            elif len(tz_raw) == 5 and (tz_raw[0] in {"+", "-"}) and tz_raw[1:].isdigit():
                # Convert -0700 -> -07:00
                tz_out = f"{tz_raw[0]}{tz_raw[1:3]}:{tz_raw[3:5]}"
            else:
                tz_out = tz_raw

        return date_out, time_out, tz_out

    # Fallback: date-only extraction.
    date_match = re.search(r"(\d{4})[:\-](\d{2})[:\-](\d{2})", text)
    if date_match:
        yyyy, mm, dd = date_match.groups()
        return f"{yyyy}-{mm}-{dd}", "", ""

    return "", "", ""


def normalize_timezone_only(value: str) -> str:
    text = value.strip()
    if not text:
        return ""
    if text.upper() == "Z":
        return "Z"

    m = re.fullmatch(r"([+\-])(\d{2}):?(\d{2})", text)
    if m:
        sign, hh, mm = m.groups()
        return f"{sign}{hh}:{mm}"

    # EXIF TimeZoneOffset can be "-7" or "-7 -7".
    m2 = re.fullmatch(r"([+\-]?\d{1,2})(?:\s+[+\-]?\d{1,2})?", text)
    if m2:
        hour = int(m2.group(1))
        if -23 <= hour <= 23:
            return f"{hour:+03d}:00"

    return ""


def resolve_capture_components(exif_values: dict[str, str]) -> tuple[str, str, str]:
    best_date = ""
    best_time = ""
    best_tz = ""

    # Prefer the first candidate with both date and time.
    for tag in CAPTURE_DATETIME_TAGS:
        raw = exif_values.get(tag, "").strip()
        if not raw:
            continue
        date_out, time_out, tz_out = normalize_capture_parts(raw)
        if date_out and time_out:
            best_date, best_time = date_out, time_out
            best_tz = tz_out
            break
        if date_out and not best_date:
            best_date = date_out

    # If timezone wasn't embedded in datetime value, try dedicated TZ tags.
    if not best_tz:
        for tag in CAPTURE_TZ_TAGS:
            raw_tz = exif_values.get(tag, "").strip()
            if not raw_tz:
                continue
            tz_out = normalize_timezone_only(raw_tz)
            if tz_out:
                best_tz = tz_out
                break

    return best_date, best_time, best_tz


def normalize_capture_date(value: str) -> str:
    date_out, _, _ = normalize_capture_parts(value)
    return date_out


def parse_gps_coordinate(raw_value: str, *, is_latitude: bool) -> float | None:
    text = raw_value.strip()
    if not text:
        return None

    # Decimal degree fast-path.
    try:
        value = float(text)
        if is_latitude and -90.0 <= value <= 90.0:
            return value
        if (not is_latitude) and -180.0 <= value <= 180.0:
            return value
    except ValueError:
        pass

    nums = [float(n) for n in re.findall(r"[-+]?\d+(?:\.\d+)?", text)]
    if not nums:
        return None

    deg = abs(nums[0])
    minute = nums[1] if len(nums) >= 2 else 0.0
    second = nums[2] if len(nums) >= 3 else 0.0
    value = deg + (minute / 60.0) + (second / 3600.0)

    # Sign from explicit degree sign or hemisphere suffix.
    if nums[0] < 0:
        value = -value

    hemi_match = re.search(r"\b([NSEW])\b", text, flags=re.IGNORECASE)
    if hemi_match:
        hemi = hemi_match.group(1).upper()
        if hemi in {"S", "W"}:
            value = -abs(value)
        else:
            value = abs(value)

    if is_latitude and not (-90.0 <= value <= 90.0):
        return None
    if (not is_latitude) and not (-180.0 <= value <= 180.0):
        return None

    return value


def resolve_gps_coordinates(exif_values: dict[str, str]) -> tuple[float, float] | None:
    lat: float | None = None
    lon: float | None = None

    for tag in GPS_LAT_TAGS:
        raw = exif_values.get(tag, "").strip()
        if not raw:
            continue
        parsed = parse_gps_coordinate(raw, is_latitude=True)
        if parsed is not None:
            lat = parsed
            break

    for tag in GPS_LON_TAGS:
        raw = exif_values.get(tag, "").strip()
        if not raw:
            continue
        parsed = parse_gps_coordinate(raw, is_latitude=False)
        if parsed is not None:
            lon = parsed
            break

    if lat is None or lon is None:
        return None
    return lat, lon


def reverse_geocode_enabled() -> bool:
    raw = os.environ.get("ANNOTATE_REVERSE_GEOCODE", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def geocode_timeout_seconds() -> float:
    timeout = 2.0
    timeout_raw = os.environ.get("ANNOTATE_GEOCODE_TIMEOUT", "").strip()
    if timeout_raw:
        try:
            timeout = max(0.2, float(timeout_raw))
        except ValueError:
            timeout = 2.0
    return timeout


def geocoder_provider() -> str:
    provider = os.environ.get("ANNOTATE_GEOCODER", "nominatim").strip().lower()
    if provider in {"", "default"}:
        return "nominatim"
    if provider in {"none", "off", "disabled"}:
        return "none"
    if provider in {"google", "nominatim"}:
        return provider
    return "nominatim"


def fetch_json(url: str, *, timeout: float) -> dict[str, Any] | None:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "annotate-border-v2/1.0",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None

    if isinstance(payload, dict):
        return payload
    return None


@lru_cache(maxsize=1024)
def reverse_geocode_nominatim(lat_rounded: float, lon_rounded: float) -> str:
    params = {
        "lat": f"{lat_rounded:.6f}",
        "lon": f"{lon_rounded:.6f}",
        "format": "jsonv2",
        "addressdetails": "1",
        "zoom": "12",
    }
    url = "https://nominatim.openstreetmap.org/reverse?" + urllib.parse.urlencode(params)
    payload = fetch_json(url, timeout=geocode_timeout_seconds())
    if payload is None:
        return ""

    address = payload.get("address", {})
    if not isinstance(address, dict):
        address = {}

    city = (
        address.get("city")
        or address.get("town")
        or address.get("village")
        or address.get("hamlet")
        or address.get("municipality")
        or address.get("county")
    )
    state = address.get("state") or address.get("state_district") or address.get("region")
    country = address.get("country")

    parts: list[str] = []
    for part in [city, state, country]:
        text = str(part).strip() if part else ""
        if text and text not in parts:
            parts.append(text)

    if parts:
        return ", ".join(parts)

    display_name = payload.get("display_name")
    if isinstance(display_name, str):
        return display_name.strip()

    return ""


def format_google_result(result: dict[str, Any]) -> str:
    formatted = result.get("formatted_address")
    if isinstance(formatted, str) and formatted.strip():
        return formatted.strip()

    components = result.get("address_components", [])
    if not isinstance(components, list):
        return ""

    by_type: dict[str, str] = {}
    for comp in components:
        if not isinstance(comp, dict):
            continue
        name = comp.get("long_name")
        types = comp.get("types", [])
        if not isinstance(name, str) or not isinstance(types, list):
            continue
        for t in types:
            if isinstance(t, str) and t not in by_type:
                by_type[t] = name.strip()

    city = by_type.get("locality") or by_type.get("postal_town") or by_type.get("administrative_area_level_2")
    state = by_type.get("administrative_area_level_1")
    country = by_type.get("country")

    parts: list[str] = []
    for part in [city, state, country]:
        if part and part not in parts:
            parts.append(part)
    return ", ".join(parts)


@lru_cache(maxsize=1024)
def reverse_geocode_google(lat_rounded: float, lon_rounded: float, api_key: str) -> str:
    params = {
        "latlng": f"{lat_rounded:.6f},{lon_rounded:.6f}",
        "key": api_key,
    }
    url = "https://maps.googleapis.com/maps/api/geocode/json?" + urllib.parse.urlencode(params)
    payload = fetch_json(url, timeout=geocode_timeout_seconds())
    if payload is None:
        return ""

    status = str(payload.get("status", ""))
    if status != "OK":
        return ""

    results = payload.get("results", [])
    if not isinstance(results, list):
        return ""

    # Prefer landmark-ish result types when available.
    preferred_types = {
        "point_of_interest",
        "establishment",
        "premise",
        "tourist_attraction",
        "park",
        "church",
        "place_of_worship",
        "natural_feature",
    }

    for result in results:
        if not isinstance(result, dict):
            continue
        types = result.get("types", [])
        if isinstance(types, list) and any((isinstance(t, str) and t in preferred_types) for t in types):
            text = format_google_result(result)
            if text:
                return text

    for result in results:
        if isinstance(result, dict):
            text = format_google_result(result)
            if text:
                return text

    return ""


def reverse_geocode_location(lat_rounded: float, lon_rounded: float) -> str:
    provider = geocoder_provider()
    if provider == "none":
        return ""

    if provider == "google":
        api_key = os.environ.get("ANNOTATE_GOOGLE_API_KEY", "").strip()
        if not api_key:
            return ""
        return reverse_geocode_google(lat_rounded, lon_rounded, api_key)

    return reverse_geocode_nominatim(lat_rounded, lon_rounded)


def build_display_metadata(variables: list[str], exif_values: dict[str, str]) -> dict[str, str]:
    display: dict[str, str] = {}
    capture_date, capture_time, capture_tz = resolve_capture_components(exif_values)

    location_value = ""
    if "location" in variables and reverse_geocode_enabled():
        coords = resolve_gps_coordinates(exif_values)
        if coords is not None:
            lat, lon = coords
            location_value = reverse_geocode_location(round(lat, 6), round(lon, 6))

    for var in variables:
        if var == "camera":
            make = first_nonempty(exif_values, ["EXIF:Make", "XMP-exif:Make", "XMP-tiff:Make"])
            model = first_nonempty(exif_values, ["EXIF:Model", "EXIF:CameraModelName", "XMP-exif:Model", "XMP-tiff:Model"])
            display[var] = " ".join(x for x in [make, model] if x).strip()
            continue

        if var == "location":
            display[var] = location_value
            continue

        tags = ATTRIBUTE_TAG_MAP.get(var)
        if tags:
            value = first_nonempty(exif_values, tags)

            if var in {"capture-date", "capture-time", "capture-datetime", "capture-tz"}:
                if var == "capture-date":
                    display[var] = capture_date
                elif var == "capture-time":
                    display[var] = capture_time
                elif var == "capture-tz":
                    display[var] = capture_tz
                else:
                    if capture_date and capture_time:
                        display[var] = f"{capture_date} {capture_time}{(' ' + capture_tz) if capture_tz else ''}"
                    else:
                        display[var] = ""
                continue

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

    variables = collect_variables_from_profile(profile)
    tags = exiftool_tags_for_variables(variables)
    exif_values = run_exiftool_json(input_path, tags)
    metadata = build_display_metadata(variables, exif_values)

    font_families = parse_font_family_list(profile.font_family)

    dummy = image_mod.new("RGB", (max(64, image_w), max(64, image_h)), color="#FFFFFF")
    draw_dummy = draw_mod.Draw(dummy)

    measured_boxes: list[dict[str, Any]] = []
    exterior_top_needed = 0
    exterior_bottom_needed = 0

    # First pass: measure renderable boxes and compute exterior gutter requirements.
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

        box_pad = pad_exterior if box.region == "exterior" else pad_interior
        max_content_w = image_w - (2 * box_pad)
        if max_content_w <= 0:
            raise ValueError(f"Strict overflow: box#{idx} has non-positive content width")

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

        if box_w > image_w:
            raise ValueError(f"Strict overflow: box#{idx} width {box_w}px exceeds image width {image_w}px")

        color = profile.text_color_exterior if box.region == "exterior" else profile.text_color_interior

        measured_boxes.append(
            {
                "idx": idx,
                "spec": box,
                "font": font,
                "lines": wrapped_lines,
                "line_widths": widths,
                "line_heights": heights,
                "line_spacing_px": line_spacing_px,
                "box_w": box_w,
                "box_h": box_h,
                "text_color": color,
            }
        )

        if box.region == "exterior":
            if box.edge == "top":
                exterior_top_needed = max(exterior_top_needed, box_h)
            else:
                exterior_bottom_needed = max(exterior_bottom_needed, box_h)

    top_gutter = max(frame_px, exterior_top_needed)
    bottom_gutter = max(frame_px, exterior_bottom_needed)

    canvas_w = image_w + 2 * frame_px
    canvas_h = top_gutter + image_h + bottom_gutter
    image_x = frame_px
    image_y = top_gutter

    emit_diag(
        diagnostics,
        f"gutters frame_px={frame_px} top_gutter={top_gutter} bottom_gutter={bottom_gutter}",
    )

    zones = {
        ("exterior", "top"): (image_x, 0, image_w, top_gutter),
        ("exterior", "bottom"): (image_x, image_y + image_h, image_w, bottom_gutter),
        ("interior", "top"): (image_x, image_y, image_w, image_h),
        ("interior", "bottom"): (image_x, image_y, image_w, image_h),
    }

    placements: list[BoxLayoutResult] = []

    # Second pass: place measured boxes into resolved zones.
    # Boxes sharing the same zone/edge always anchor to the same row (no vertical stacking).
    for measured in measured_boxes:
        idx = int(measured["idx"])
        box = measured["spec"]
        box_w = int(measured["box_w"])
        box_h = int(measured["box_h"])

        if box.align == "left":
            box_x = image_x
        elif box.align == "right":
            box_x = image_x + image_w - box_w
        else:
            box_x = image_x + (image_w - box_w) // 2

        key = (box.region, box.edge)
        zone_x, zone_y, zone_w, zone_h = zones[key]
        _ = zone_x, zone_w

        if box_h > zone_h:
            raise ValueError(f"Strict overflow: box#{idx} exceeds {box.edge} zone height")

        # Zone-specific shared-row anchors:
        # - exterior/top:    align bottoms (near image edge)
        # - interior/top:    align tops
        # - interior/bottom: align bottoms
        # - exterior/bottom: align tops (near image edge)
        if key == ("exterior", "top"):
            box_y = zone_y + zone_h - box_h
        elif key == ("interior", "top"):
            box_y = zone_y
        elif key == ("interior", "bottom"):
            box_y = zone_y + zone_h - box_h
        else:  # ("exterior", "bottom")
            box_y = zone_y

        emit_diag(
            diagnostics,
            f"box#{idx} zone={box.region}/{box.edge}/{box.align} box=({box_x},{box_y},{box_w},{box_h}) lines={len(measured['lines'])}",
        )

        placements.append(
            BoxLayoutResult(
                spec=box,
                font=measured["font"],
                box_x=box_x,
                box_y=box_y,
                box_w=box_w,
                box_h=box_h,
                lines=measured["lines"],
                line_widths=measured["line_widths"],
                line_heights=measured["line_heights"],
                line_spacing_px=measured["line_spacing_px"],
                text_color=measured["text_color"],
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
            "  python3 annotate-border/annotate-border-v2.py input.jpg output.jpg "
            "--profile annotate-border/profiles/annotation-v2-demo.annotate\n"
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
