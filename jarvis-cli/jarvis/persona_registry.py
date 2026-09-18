"""Validates and normalizes PERSONAS entries contributed by actions/*.py and
~/.jarvis/tools/*.py files (see actions/_template.py section 7).

WHY THIS IS A SEPARATE MODULE
------------------------------
tool_loader.py already owns "scan a file, validate its module-level
contract, never let one bad file take discovery down" for TOOL_SCHEMAS/
TOOLS/TOOL_GROUP. PERSONAS is a second, independent contract a file can
opt into (with or without also being a tool file) — same philosophy, same
fault-tolerance, different shape. Keeping the validation here instead of
inline in tool_loader.py keeps tool_loader.py's job legible (still "load
tools") and gives this a home ai_client.py / cli.py / tools.py can import
directly without dragging in the whole loader.

WHAT A PERSONA LOOKS LIKE
--------------------------
A module sets:

    PERSONAS = [
        {
            "id": "orion",                       # required, slug
            "name": "Orion",                      # required, preset-picker label
            "assistant_name": "O.R.I.O.N.",       # optional, defaults to `name`
            "address_user_as": "commander",       # optional
            "attitude": "formal",                 # optional, id of an existing
                                                   # attitude (built-in or another
                                                   # persona's custom_attitude)
            # ...or register a brand new one inline instead of a string id:
            # "attitude": {"label": "Stoic", "full": "...", "compact": "..."},
            "hex": "#7dd3fc",                      # required unless "vars" given
            "vars": {"--accent": "#7dd3fc", ...},  # optional — see ALLOWED_VARS
            "logo": {...},                         # optional — see _resolve_logo
            "interface": {"corner_rounding": 60, "glow": 40, "text_size": 100},
            "saturation": 100,
        },
    ]

See actions/_template.py for the full, human-facing writeup of every field
and DOCUMENTATION/PERSONAS_GUIDE.md for the end-to-end picture (how this
reaches the Skin modal). This module only validates/normalizes; it never
raises out to a caller — a bad persona entry is dropped and reported as a
string in the errors list, exactly like a bad tool file is logged and
skipped by tool_loader.py.
"""

import base64
import re

MAX_LOGO_BYTES = 512 * 1024  # 512KB — a Skin-modal logo, not a photo album
MAX_TEXT_FIELD = 120
MAX_ATTITUDE_TEXT = 320

_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,48}$")
_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")

# The five names the built-in Skin modal already ships (see app.js's
# PERSONA_PRESETS). A tool claiming one of these is rejected exactly like a
# tool file claiming a built-in tool's name is rejected in tool_loader.py —
# the built-in always wins, so a shipped persona can never be shadowed or
# silently redefined by an action file.
RESERVED_PERSONA_IDS = {"jarvis", "verity", "friday", "edith", "karen"}

# Only these CSS custom properties may be set through "vars" — the exact
# set app.js's own ui-kit.js importTheme() already trusts (see that
# function's "only known variables are accepted" comment). A persona's
# "vars" is JSON that ends up as a raw root.style.setProperty() call in the
# browser; capping it to a known allow-list means a careless (not even
# malicious — this is trusted local code either way, see custom_tools_
# store.py's module docstring on that trust level) persona file can't set
# an arbitrary CSS property on :root.
ALLOWED_VARS = {
    "--accent", "--accent-soft", "--accent-dim", "--accent-glow",
    "--accent-secondary", "--accent-tertiary",
    "--accent-secondary-rgb", "--accent-tertiary-rgb",
    "--status-online", "--border", "--border-strong",
    "--bg", "--bg-1", "--bg-panel", "--bg-panel-2", "--bg-raised",
}

_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _slugify(text):
    return _SLUG_STRIP_RE.sub("-", (text or "").strip().lower()).strip("-")[:48]


def _clamp(value, lo, hi, default):
    try:
        n = float(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _resolve_logo(logo, filename, base_dir, errors):
    """Returns a normalized logo dict or None. Never raises.

    Three shapes are accepted, matching how the built-in Jarvis/Verity
    logos already work in app.js (boot ring + brand mark, swapped via
    innerHTML) plus a plain-PNG option for personas that aren't drawing an
    SVG by hand:

        {"svg_boot": "<circle .../>", "svg_brand": "<circle .../>"}
            Raw SVG markup, inserted into the existing boot__svg (viewBox
            "0 0 200 200") and .brand-mark (viewBox "0 0 40 40") elements
            exactly like LOGO_MARKUP.default/.verity already are — see
            app.js's applyPersonaLogo(). Only "svg_brand" is required;
            "svg_boot" falls back to the default arc-reactor rings if
            omitted, since the brand mark (topbar, buttons) is seen far
            more often than the once-per-session boot screen.

        {"png_path": "orion.png"}
            A PNG file next to the action file that registered this
            persona (resolved relative to that file's own directory —
            NOT the working directory — or given as an absolute path).
            Read once at discovery time and embedded as a base64 data URI,
            so nothing needs to be served or copied anywhere.

        {"png_base64": "iVBORw0KG..."}
            The same thing pre-encoded, for a persona built from a PNG
            that's already in memory (e.g. downloaded by the tool itself)
            rather than sitting on disk.

    A PNG logo (either form) is rendered as a plain <image> inside the
    existing SVG viewBox, at whatever aspect ratio it already has
    (preserveAspectRatio="xMidYMid meet") — Jarvis does not inspect,
    strip, key out, or otherwise touch the PNG's background. A
    transparent PNG shows the app's background through it, same as the
    built-in SVG rings already do; a PNG with a solid background just
    shows that background as part of the mark. Neither is treated as
    wrong — "ignore the background" here means exactly that: it is
    whatever the PNG already says it is.
    """
    if logo is None:
        return None
    if not isinstance(logo, dict):
        errors.append(f"{filename}: 'logo' must be an object.")
        return None

    svg_boot = logo.get("svg_boot")
    svg_brand = logo.get("svg_brand")
    png_path = logo.get("png_path")
    png_b64 = logo.get("png_base64")

    if svg_brand:
        if not isinstance(svg_brand, str) or len(svg_brand) > 20_000:
            errors.append(f"{filename}: 'logo.svg_brand' must be a string under 20000 chars.")
            return None
        if svg_boot is not None and (not isinstance(svg_boot, str) or len(svg_boot) > 20_000):
            errors.append(f"{filename}: 'logo.svg_boot' must be a string under 20000 chars.")
            return None
        return {"type": "svg", "brand": svg_brand, "boot": svg_boot or None}

    data = None
    if png_path:
        if not isinstance(png_path, str):
            errors.append(f"{filename}: 'logo.png_path' must be a string.")
            return None
        try:
            p = (base_dir / png_path) if base_dir and not _is_abs(png_path) else _as_path(png_path)
            raw = p.read_bytes()
        except Exception as e:
            errors.append(f"{filename}: couldn't read logo.png_path {png_path!r}: {e}")
            return None
        if len(raw) > MAX_LOGO_BYTES:
            errors.append(f"{filename}: logo.png_path {png_path!r} is over {MAX_LOGO_BYTES} bytes.")
            return None
        data = base64.b64encode(raw).decode("ascii")
    elif png_b64:
        if not isinstance(png_b64, str):
            errors.append(f"{filename}: 'logo.png_base64' must be a string.")
            return None
        cleaned = png_b64.split(",")[-1]  # tolerate a caller pasting a full data: URI
        try:
            raw = base64.b64decode(cleaned, validate=True)
        except Exception as e:
            errors.append(f"{filename}: 'logo.png_base64' isn't valid base64: {e}")
            return None
        if len(raw) > MAX_LOGO_BYTES:
            errors.append(f"{filename}: logo.png_base64 is over {MAX_LOGO_BYTES} bytes.")
            return None
        data = cleaned

    if data is None:
        errors.append(f"{filename}: 'logo' needs one of svg_brand, png_path, or png_base64.")
        return None

    return {"type": "png", "data_uri": f"data:image/png;base64,{data}"}


def _is_abs(path_str):
    return path_str.startswith("/") or (len(path_str) > 1 and path_str[1] == ":")


def _as_path(path_str):
    from pathlib import Path
    return Path(path_str)


def _resolve_attitude(raw_attitude, filename, errors):
    """Returns (attitude_id, custom_attitude_or_None). Never raises.

    `raw_attitude` is either a plain string (an existing attitude id — not
    validated against the live registry here, since a persona in FILE A is
    allowed to reference a custom attitude registered by FILE B and
    discovery order isn't guaranteed; an unresolvable id just falls back
    to the default attitude at prompt-build time, the same graceful
    fallback ATTITUDE_PRESETS.get(..., DEFAULT) already gives a typo'd
    built-in id today) or a dict defining a brand new one inline.
    """
    if raw_attitude is None:
        return None, None
    if isinstance(raw_attitude, str):
        cleaned = raw_attitude.strip()
        return (cleaned or None), None
    if isinstance(raw_attitude, dict):
        label = (raw_attitude.get("label") or "").strip()
        full = (raw_attitude.get("full") or "").strip()
        compact = (raw_attitude.get("compact") or "").strip()
        if not label or not full or not compact:
            errors.append(
                f"{filename}: a custom 'attitude' object needs non-empty "
                "'label', 'full', and 'compact'."
            )
            return None, None
        if len(full) > MAX_ATTITUDE_TEXT or len(compact) > MAX_ATTITUDE_TEXT:
            errors.append(f"{filename}: custom attitude 'full'/'compact' must be under {MAX_ATTITUDE_TEXT} chars.")
            return None, None
        att_id = _slugify(raw_attitude.get("id") or label)
        if not att_id:
            errors.append(f"{filename}: couldn't derive an id for custom attitude {label!r}.")
            return None, None
        return att_id, {"id": att_id, "label": label[:MAX_TEXT_FIELD], "full": full, "compact": compact}
    errors.append(f"{filename}: 'attitude' must be a string id or an object with label/full/compact.")
    return None, None


def _resolve_interface(raw, filename, errors):
    if raw is None:
        return None
    if not isinstance(raw, dict):
        errors.append(f"{filename}: 'interface' must be an object.")
        return None
    out = {}
    if "corner_rounding" in raw:
        out["radius"] = _clamp(raw.get("corner_rounding"), 0, 200, 100)
    if "glow" in raw:
        out["glow"] = _clamp(raw.get("glow"), 0, 200, 100)
    if "text_size" in raw:
        out["fontScale"] = _clamp(raw.get("text_size"), 80, 130, 100)
    return out or None


def validate_personas(raw_personas, filename, base_dir=None):
    """raw_personas: whatever a module's PERSONAS attribute holds.

    Returns (normalized_list, error_strings). Every entry in
    normalized_list is guaranteed to have at minimum: id, name,
    assistant_name, source_file. Entries that fail validation are dropped
    (with a reason appended to error_strings) rather than aborting the
    rest of the file's personas — one bad entry in a five-persona list
    shouldn't cost the other four, mirroring discover_actions()'s own
    per-file (not per-catalog) fault isolation.
    """
    if not isinstance(raw_personas, list):
        return [], [f"{filename}: PERSONAS must be a list."]

    out = []
    errors = []
    seen_in_file = set()

    for i, entry in enumerate(raw_personas):
        tag = f"{filename} PERSONAS[{i}]"
        if not isinstance(entry, dict):
            errors.append(f"{tag}: must be an object.")
            continue

        raw_id = entry.get("id")
        name = (entry.get("name") or "").strip()
        if not name:
            errors.append(f"{tag}: 'name' is required.")
            continue
        pid = _slugify(raw_id) if raw_id else _slugify(name)
        if not pid or not _ID_RE.match(pid):
            errors.append(f"{tag}: couldn't derive a valid id (got {pid!r}).")
            continue
        if pid in RESERVED_PERSONA_IDS:
            errors.append(f"{tag}: id {pid!r} is a built-in persona name and can't be overridden.")
            continue
        if pid in seen_in_file:
            errors.append(f"{tag}: duplicate id {pid!r} within {filename}.")
            continue

        hex_value = entry.get("hex")
        vars_raw = entry.get("vars")
        vars_clean = None
        if vars_raw is not None:
            if not isinstance(vars_raw, dict):
                errors.append(f"{tag}: 'vars' must be an object.")
                continue
            vars_clean = {}
            for k, v in vars_raw.items():
                if k not in ALLOWED_VARS:
                    errors.append(f"{tag}: 'vars' key {k!r} isn't a recognized theme variable — dropped.")
                    continue
                if not isinstance(v, str) or len(v) > 80:
                    errors.append(f"{tag}: 'vars[{k!r}]' must be a short string — dropped.")
                    continue
                vars_clean[k] = v
        if hex_value is not None and not (isinstance(hex_value, str) and _HEX_RE.match(hex_value)):
            errors.append(f"{tag}: 'hex' must look like '#rrggbb'.")
            continue
        if not hex_value and not vars_clean:
            errors.append(f"{tag}: needs either 'hex' or 'vars' (or both).")
            continue
        # A persona with 'vars' behaves like the built-in ones: a fixed,
        # non-derived palette, so the saturation slider has no effect on
        # it (see app.js updateSaturationControlState()). One with only
        # 'hex' instead runs through the normal accent-derivation math
        # (applyAccent()) and IS affected by saturation, same as picking
        # a plain accent color.
        hardcoded = bool(vars_clean)

        attitude_id, custom_attitude = _resolve_attitude(entry.get("attitude"), filename, errors)

        logo = _resolve_logo(entry.get("logo"), filename, base_dir, errors)

        interface = _resolve_interface(entry.get("interface"), filename, errors)

        saturation = None
        if entry.get("saturation") is not None:
            saturation = _clamp(entry.get("saturation"), 0, 150, 100)

        address = entry.get("address_user_as")
        if address is not None and (not isinstance(address, str) or len(address) > MAX_TEXT_FIELD):
            errors.append(f"{tag}: 'address_user_as' must be a short string — ignored.")
            address = None

        assistant_name = (entry.get("assistant_name") or name).strip()[:60] or name

        seen_in_file.add(pid)
        out.append({
            "id": pid,
            "name": name[:MAX_TEXT_FIELD],
            "assistant_name": assistant_name,
            "address_user_as": address,
            "attitude": attitude_id,
            "custom_attitude": custom_attitude,
            "hex": hex_value or (vars_clean or {}).get("--accent"),
            "vars": vars_clean,
            "hardcoded": hardcoded,
            "logo": logo,
            "interface": interface,
            "saturation": saturation,
            "source_file": filename,
        })

    return out, errors
