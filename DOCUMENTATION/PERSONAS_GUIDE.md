# Tool-registered personas — end to end

This is the human-facing walkthrough for the `PERSONAS` contract. The
field-by-field reference (with every field commented inline) lives in
`jarvis-cli/jarvis/actions/_template.py` §7 — start there if you just want
to copy-paste something. This doc is for understanding how the pieces fit
together, and for the parts too big to comment inline (the full "vars" key
list, a worked example, the request/response shapes).

## The idea

The Skin modal (the "Skin" button next to the topbar title) has always
shipped five hardcoded personas — J.A.R.V.I.S, Verity, Friday, Edith,
Karen — each a one-click bundle of a name, an accent palette, a logo, and
an attitude. `PERSONAS` lets a **tool file** (built-in, under
`jarvis-cli/jarvis/actions/`, or your own under `~/.jarvis/tools/`) add
more of these, with the exact same one-click behavior, appearing as
ordinary pills right alongside the built-in five. A persona-registering
file doesn't need to define any actual tools — a file can register
personas only, tools only, or both.

## What a tool can control

| Skin modal field           | `PERSONAS` key                          |
|-----------------------------|------------------------------------------|
| Preset pill label            | `name` (required)                        |
| Assistant's display name     | `assistant_name` (defaults to `name`)    |
| "Addresses you as"           | `address_user_as`                        |
| Attitude                     | `attitude` — an existing id, or an inline `{label, full, compact}` to register a brand new one |
| Accent color                 | `hex` (derived palette) or `vars` (fixed palette) — one of the two is required |
| Logo (boot screen + every brand mark) | `logo` — SVG markup or a PNG (path or base64) |
| Interface (corner rounding / glow / text size) | `interface` |
| Saturation slider             | `saturation` |

None of these are required except `name` and a color. Register just a
name and a color and you get a working, if plain, persona; fill in as many
of the rest as you care about.

## A complete worked example

```python
# jarvis-cli/jarvis/actions/orion_persona.py
"""Registers an 'Orion' persona. Doesn't define any tools of its own —
a PERSONAS-only file is entirely valid; see actions/_template.py §7."""

PERSONAS = [
    {
        "id": "orion",
        "name": "Orion",
        "assistant_name": "O.R.I.O.N.",
        "address_user_as": "commander",
        "attitude": {
            "label": "Stoic",
            "full": "calm and unflinching under pressure, measures every "
                    "word before it's spoken, never raises its voice",
            "compact": "Calm, measured, unflinching.",
        },
        "hex": "#7dd3fc",
        "logo": {"png_path": "orion_logo.png"},  # next to this .py file
        "interface": {"corner_rounding": 40, "glow": 20, "text_size": 110},
        "saturation": 80,
    },
]
```

Drop `orion_logo.png` next to that file, restart Jarvis, open the Skin
modal — "Orion" is now a pill next to J.A.R.V.I.S and Verity. Click it,
click Save, and `ai_config.json`'s `persona.assistant_name`/
`address_user_as`/`attitude` are written exactly the way picking a
built-in persona already writes them (see `ai_config.py`) — nothing about
the save path changes.

## The `hex` vs `vars` choice

- **`hex` only**: the color you give runs through the same derivation math
  a plain accent-color pick already uses (soft/dim/glow/secondary/tertiary
  are all computed from it). The Saturation slider affects this persona,
  same as any accent color.
- **`vars`**: a fully fixed palette — every value literal, nothing
  derived. This is how all five built-in personas work. The Saturation
  slider has **no effect** on a persona defined this way (it's disabled
  while such a persona is active, same as it already is for the built-in
  five).

Only these exact keys are accepted in `vars` — anything else is dropped
with a logged warning, not silently ignored and not an error that kills
the whole file:

```
--accent  --accent-soft  --accent-dim  --accent-glow
--accent-secondary  --accent-tertiary
--accent-secondary-rgb  --accent-tertiary-rgb
--status-online  --border  --border-strong
--bg  --bg-1  --bg-panel  --bg-panel-2  --bg-raised
```

## Logos

Two shapes, pick one:

- **SVG** (`svg_brand`, optionally `svg_boot`) — raw markup dropped
  straight into the existing `.brand-mark` (40×40 viewBox) and
  `.boot__svg` (200×200 viewBox) elements, the same way Verity's
  hand-drawn smiley-face mark already is.
- **PNG** (`png_path` or `png_base64`) — read once at discovery time
  (capped at 512KB) and embedded as a base64 data URI, wrapped in a plain
  `<image>` element inside the existing SVG viewBox
  (`preserveAspectRatio="xMidYMid meet"`).

**On transparency**: a PNG's background is used exactly as the file
already has it. Jarvis never inspects, strips, or chroma-keys a PNG's
background — a transparent PNG shows the app's background through it
(matching how the SVG rings behave, since they have no fill of their
own); a PNG with a solid/opaque background just shows that background as
part of the mark. There's no "ignore transparency" processing step to
configure — whichever the PNG already is, is what's shown.

## Custom attitudes

`attitude` can be a plain string — the id of an attitude that already
exists, built-in (`dry`, `cheerful`, `snarky`, `formal`, `warm`, `blunt` —
see `ai_client.ATTITUDE_PRESETS`) or registered by any persona, in any
file, including this one. Or it can be an inline object:

```python
"attitude": {
    "label": "Stoic",       # shown in the Skin modal's dropdown
    "full": "...",          # folded into the system prompt (see
                             # ai_client._system_prompt())
    "compact": "...",       # a shorter variant used where prompt space
                             # is tighter
},
```

An inline attitude is registered globally the moment its file is
discovered, under a slug derived from `label` (or an explicit `id`). It
becomes selectable for **any** persona from then on — including from the
Skin modal's dropdown directly, with no persona pill involved at all — not
just the one that defined it, and it genuinely changes what the model is
told about its own personality (`ai_client.ATTITUDE_PRESETS` is updated
with it at import time), not just what the dropdown shows.

## Validation and failure modes

Every persona is validated independently, at discovery time, in
`jarvis-cli/jarvis/persona_registry.py`:

- Missing `name`, a malformed `hex`, an unreadable `png_path`, a `vars`
  key outside the allowed list, an oversized logo, an incomplete inline
  attitude — each of these drops just that one persona (logged to
  stderr/the log file) and never touches the rest of that file's
  personas, that file's tools, or the rest of discovery.
- An `id` that collides with a built-in persona (`jarvis`, `verity`,
  `friday`, `edith`, `karen`) is rejected — the built-in always wins, a
  shipped persona can never be shadowed.
- An `id` that collides across two different files: first registration
  wins (built-in `actions/` is always scanned before your own
  `~/.jarvis/tools/`), the later one is logged and dropped.

Check your terminal or log output after adding a persona — the same habit
`actions/_template.py`'s very first section already recommends for tools.

## Where this actually surfaces (for anyone touching the plumbing)

```
your action file's PERSONAS
        │
        ▼
tool_loader.discover_actions()   (scans actions/, then ~/.jarvis/tools/)
        │  calls persona_registry.validate_personas() per file
        ▼
tools.AUTO_PERSONAS / tools.AUTO_ATTITUDES   (deduped, aggregated)
        │
        ├──▶ ai_client.ATTITUDE_PRESETS.update(AUTO_ATTITUDES)
        │       (custom attitudes actually affect the system prompt)
        │
        └──▶ tools.personas_list_payload()
                    │
                    ▼
             `jarvis personas-list`   (JSON to stdout, same contract
                                        as `jarvis tools-list`)
                    │
                    ▼
             GET /api/personas   (web/server.js)
                    │
                    ▼
             web/public/app.js: loadRegisteredPersonas()
                    │  merges into REGISTERED_PERSONAS / REGISTERED_ATTITUDES
                    ▼
             Skin modal: persona pills, Attitude dropdown
```

Nothing in this chain needs to be edited to add a new persona — the whole
point is that a tool file is the only file you write.
