(() => {
  "use strict";

  // ===========================================================================
  // Utilities
  // ===========================================================================

  const qs = (sel, root = document) => root.querySelector(sel);
  const qsa = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  function el(tag, attrs = {}, children = []) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") node.className = v;
      else if (k === "html") node.innerHTML = v;
      else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v);
    }
    for (const c of [].concat(children)) {
      if (c == null) continue;
      node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    }
    return node;
  }

  // ===========================================================================
  // Skin — persona rename (name Jarvis answers to / what he calls you) plus a
  // purely cosmetic accent color, edited from the "Skin" button next to the
  // topbar title. Persona fields are the same ai_config.json persona.
  // assistant_name/address_user_as the raw Settings > Config > AI tab edits
  // (see ai_config.py) — this is just a friendlier form for those two fields.
  // The accent color has no server-side equivalent at all: it's stored in
  // this browser's localStorage only and applied by overriding the CSS
  // custom properties every color in style.css is already built from
  // (--accent/--accent-soft/--accent-dim/--accent-glow), so nothing but the
  // :root override needs to change for a new color to skin the whole app.
  // ===========================================================================

  const SKIN_STORAGE_KEY = "jarvis.skin.v1";
  const SKIN_DEFAULT_ACCENT = "#4fd8ff";
  const SKIN_DEFAULT_NAME = "J.A.R.V.I.S";
  const SKIN_DEFAULT_ADDRESS = "sir";
  // Mirrors ai_client.ATTITUDE_PRESETS on the backend (id -> label) — kept
  // in sync by hand since the web UI has no direct import path into the
  // Python package. If a new attitude is added there, add its id/label
  // here too so it shows up as an option. "dry" first/default matches the
  // backend's DEFAULT_ATTITUDE and the app's original, unchanged tone.
  const SKIN_ATTITUDES = [
    { id: "dry", label: "Dry Wit (default)" },
    { id: "cheerful", label: "Cheerful" },
    { id: "snarky", label: "Snarky" },
    { id: "formal", label: "Formal Butler" },
    { id: "warm", label: "Warm & Encouraging" },
    { id: "blunt", label: "No-Nonsense" },
  ];
  const SKIN_DEFAULT_ATTITUDE = "dry";

  // Mirrors jarvis-cli/jarvis/persona_name.py's sanitize_cli_name() EXACTLY
  // (same illegal-char stripping, same digit-lead/reserved-name fallback)
  // so the Skin modal's live preview always matches what the next
  // `script.bat` rebuild will actually name the exe/command as. If the
  // rules ever change on the Python side, mirror the change here too.
  const CLI_NAME_RESERVED = new Set([
    "cli", "cmd", "com", "con", "exe", "nul", "prn", "aux",
    "python", "python3", "py", "pip", "git", "npm", "node",
  ]);
  const CLI_NAME_DEFAULT = "jarvis";
  function sanitizeCliName(raw) {
    const cleaned = String(raw || "").replace(/[^A-Za-z0-9]+/g, "").toLowerCase();
    if (!cleaned) return CLI_NAME_DEFAULT;
    const withPrefix = /^[0-9]/.test(cleaned) ? "cli" + cleaned : cleaned;
    if (CLI_NAME_RESERVED.has(withPrefix)) return CLI_NAME_DEFAULT;
    return withPrefix;
  }
  // Updates the "Terminal command after rebuild: <x>" hint under the name
  // field. Purely cosmetic preview — the real rename only takes effect
  // once the person reruns script.bat, which is spelled out in the hint
  // text itself so nobody expects it to apply live.
  function updateCliNamePreview() {
    const hint = qs("#skin-cli-name-preview");
    if (!hint) return;
    const raw = qs("#skin-assistant-name") ? qs("#skin-assistant-name").value : "";
    hint.innerHTML = `Terminal command after rebuild: <code>${sanitizeCliName(raw)}</code>`;
  }
  // Saturation slider range/default, as a percentage (100 = unchanged).
  // Scales the saturation of every skin-derived color (accent, soft, dim,
  // border, secondary/tertiary, background tint) — see applyAccent() below.
  // Never touches the "Classic (hardcoded)" preset, which bypasses
  // applyAccent() entirely.
  const SKIN_SATURATION_MIN = 0;
  const SKIN_SATURATION_MAX = 150;
  const SKIN_DEFAULT_SATURATION = 100;

  // A handful of curated presets shown as swatches; the color input next to
  // them covers everything else.
  //
  // NOTE: an earlier round of this feature (see the handoff doc) hand-baked
  // a "muted" version of each of these five hexes directly into this array,
  // scaling saturation down to ~55-65% of the original while keeping
  // lightness unchanged, to avoid a "candy-bright/neon" look. That turned
  // out to be the wrong call for a *different* reason than the one it was
  // trying to fix: baking the dilution into the stored hex meant it could
  // never be undone — every preset except Cyan ended up permanently
  // washed-out/pastel ("a shade of white in it"), with no way back to the
  // true color even at the saturation slider's max. Reverted back to each
  // preset's true, fully-saturated original hex here; the saturation
  // slider (see SKIN_SATURATION_MIN/MAX, applyAccent) is the actual,
  // reversible control for dialing vividness up or down from there, and
  // its default (100%) is a no-op — full original saturation, not muted.
  const SKIN_PRESETS = [
    { id: "cyan", name: "Cyan (default)", hex: "#4fd8ff" },
    { id: "amber", name: "Amber", hex: "#ffb347" },
    { id: "crimson", name: "Crimson", hex: "#ff5c72" },
    { id: "violet", name: "Violet", hex: "#b98bff" },
    { id: "emerald", name: "Emerald", hex: "#4fe6a4" },
    { id: "rose-gold", name: "Rose Gold", hex: "#f2b7c2" },
    // The exact original --blue (#2b5cff) from the pre-skins build, back
    // when it was a fixed, non-skinnable color (still is, for the
    // "precise" mode chip; see the round-3 note in applyAccent's callers /
    // the handoff doc). It also happens to land in a hue gap (~226°) the
    // other six presets don't cover between Cyan (~193°) and Violet
    // (~264°), so it reads cleanly as its own option.
    { id: "sapphire", name: "Sapphire", hex: "#2b5cff" },
    // The literal, pre-skins J.A.R.V.I.S palette, byte-for-byte — every
    // value below is copied straight from the original :root block (and

    // cross-checked against a pre-skins-feature build), not run through
    // scaleHsl/rotateHue/mixTowardAccent like every preset above. That
    // math reproduces the original palette *almost* exactly at the cyan
    // default, but "almost" was the problem: this preset exists so there's
    // always one option with zero derivation and zero drift risk, ever.
    // `hardcoded: true` + `vars` is what routes this through
    // applyHardcodedVars() instead of applyAccent() — see both below.
    {
      id: "classic-hardcoded",
      name: "Classic (hardcoded)",
      hex: "#4fd8ff",
      hardcoded: true,
      vars: {
        "--accent": "#4fd8ff",
        "--accent-soft": "#2ea9d6",
        "--accent-dim": "#164a63",
        "--accent-glow": "rgba(79, 216, 255, 0.35)",
        "--accent-secondary": "#f2b544",
        "--accent-tertiary": "#2b5cff",
        "--accent-secondary-rgb": "242, 181, 68",
        "--accent-tertiary-rgb": "43, 92, 255",
        // The original build never had a --status-online var at all — the
        // online dot was just unconditionally var(--green). Setting it
        // explicitly here reproduces that exactly without needing the
        // status-pill CSS itself to special-case this preset.
        "--status-online": "var(--green)",
        "--border": "rgba(102, 214, 255, 0.16)",
        "--border-strong": "rgba(102, 214, 255, 0.34)",
        "--bg": "#04070d",
        "--bg-1": "#070d16",
        "--bg-panel": "rgba(9, 18, 30, 0.68)",
        "--bg-panel-2": "rgba(13, 24, 38, 0.55)",
        "--bg-raised": "#0d1826",
      },
    },
  ];

  // Named "persona" presets — separate from SKIN_PRESETS above, and from
  // the freeform accent color picker entirely. Each one bundles an
  // assistant *name* with a fully hardcoded color palette in a single
  // click: no HSL derivation, no accent math, every value below is a
  // literal, hand-picked hex/rgba, applied the exact same way (and via the
  // same applyHardcodedVars() function) as "Classic (hardcoded)" in
  // SKIN_PRESETS. That's deliberate — a named persona is meant to look
  // exactly one specific way forever, not shift with a saturation slider
  // or drift if the accent-derivation formulas above get retuned later.
  //
  // "Jarvis" here is the same values as SKIN_PRESETS' classic-hardcoded
  // entry, written out again rather than shared by reference, so editing
  // one can never accidentally change the other. Friday/Edith/Karen were
  // built by running the same scaleHsl/rotateHue/mixTowardAccent math
  // applyAccent() uses at runtime (the corrected, dim-based background
  // mix — see applyAccent()'s "IMPORTANT" comment; the values below were
  // re-baked after that fix, since they were briefly too light/washed-out
  // before it), but *offline*, once, against a chosen accent per persona —
  // then baking the results in here as plain values,
  // per the "use the hardcoded model, not the generic picker" ask. Picking
  // one of these sets qs("#skin-assistant-name")'s value (persisted to
  // ai_config.json on Save exactly like typing a name by hand) AND the
  // full palette below (persisted client-side like every other color
  // choice) — see applyPersonaPreset().
  // Logo markup swapped in per persona — see applyPersonaLogo(). Every
  // other persona keeps the default three-ring arc-reactor mark; Verity
  // gets a smiley face (two eye dots + a static smile) instead, so it
  // reads as a ball with a face rather than a reactor. Eyes reuse the
  // pulsing ring--core/brand-mark__core dot classes; the mouth is stroked
  // directly in --accent rather than reusing ring--inner/brand-mark__ring--in,
  // since those classes spin and dash, which looks broken on a mouth
  // curve instead of a ring.
  const LOGO_MARKUP = {
    default: {
      boot: `<circle cx="100" cy="100" r="90" class="ring ring--outer"/>
        <circle cx="100" cy="100" r="72" class="ring ring--mid"/>
        <circle cx="100" cy="100" r="54" class="ring ring--inner"/>
        <circle cx="100" cy="100" r="6" class="ring ring--core"/>`,
      brand: `<circle cx="20" cy="20" r="18" class="brand-mark__ring"/>
        <circle cx="20" cy="20" r="11" class="brand-mark__ring brand-mark__ring--in"/>
        <circle cx="20" cy="20" r="3" class="brand-mark__core"/>`,
    },
    verity: {
      boot: `<circle cx="100" cy="100" r="90" class="ring ring--outer"/>
        <circle cx="72" cy="80" r="10" class="ring ring--core"/>
        <circle cx="128" cy="80" r="10" class="ring ring--core"/>
        <path d="M 62 120 Q 100 155 138 120" fill="none" stroke="var(--accent)" stroke-width="6" stroke-linecap="round"/>`,
      brand: `<circle cx="20" cy="20" r="18" class="brand-mark__ring"/>
        <circle cx="14" cy="17" r="2.2" class="brand-mark__core"/>
        <circle cx="26" cy="17" r="2.2" class="brand-mark__core"/>
        <path d="M 12 24 Q 20 30 28 24" fill="none" stroke="var(--accent)" stroke-width="1.8" stroke-linecap="round"/>`,
    },
  };

  // Swaps every boot-ring and brand-mark <svg> on the page between the
  // default arc-reactor rings and Verity's smiley face, via innerHTML
  // rather than duplicating four near-identical logo variants across
  // index.html. Called anywhere a persona is applied, previewed, saved,
  // reverted, or reset — see each call site's comment.
  function applyPersonaLogo(personaId) {
    const boot = qs(".boot__svg");
    const brands = qsa(".brand-mark");
    if (personaId === "verity") {
      if (boot) boot.innerHTML = LOGO_MARKUP.verity.boot;
      for (const brand of brands) brand.innerHTML = LOGO_MARKUP.verity.brand;
      return;
    }
    // A registered persona's own logo, if it supplied one — see
    // persona_registry.py's _resolve_logo for the two accepted shapes.
    // SVG markup drops straight into the existing boot/brand elements
    // exactly like Verity's does above; a PNG is wrapped in a plain
    // in-viewBox <image>, no background handling of any kind — the PNG's
    // own alpha (or lack of it) is shown exactly as it already is.
    const registered = REGISTERED_PERSONAS.find((p) => p.id === personaId);
    const logo = registered && registered.logo;
    if (logo && logo.type === "svg" && logo.brand) {
      if (boot) boot.innerHTML = logo.boot || LOGO_MARKUP.default.boot;
      for (const brand of brands) brand.innerHTML = logo.brand;
      return;
    }
    if (logo && logo.type === "png" && logo.data_uri) {
      if (boot) {
        boot.innerHTML = `<image href="${logo.data_uri}" x="10" y="10" width="180" height="180" preserveAspectRatio="xMidYMid meet"/>`;
      }
      for (const brand of brands) {
        brand.innerHTML = `<image href="${logo.data_uri}" x="2" y="2" width="36" height="36" preserveAspectRatio="xMidYMid meet"/>`;
      }
      return;
    }
    if (boot) boot.innerHTML = LOGO_MARKUP.default.boot;
    for (const brand of brands) brand.innerHTML = LOGO_MARKUP.default.brand;
  }

  const PERSONA_PRESETS = [
    {
      // Verity — a yellow smiley-face ball. Palette derived the same way
      // Friday/Edith/Karen were (offline scaleHsl/rotateHue/mixTowardAccent
      // pass against a chosen accent, baked in as plain hex values).
      // What's actually different visually beyond color lives outside
      // `vars` entirely: applyPersonaLogo() (above) swaps the arc-reactor
      // rings for Verity's smiley face whenever this preset is active.
      id: "verity",
      name: "Verity",
      assistantName: "Verity",
      hex: "#ffd21f",
      vars: {
        "--accent": "#ffd21f",
        "--accent-soft": "#c9a316",
        "--accent-dim": "#5c4a0c",
        "--accent-glow": "rgba(255, 210, 31, 0.35)",
        "--accent-secondary": "#2ea9d6",
        "--accent-tertiary": "#ff6b3d",
        "--accent-secondary-rgb": "46, 169, 214",
        "--accent-tertiary-rgb": "255, 107, 61",
        "--status-online": "var(--accent)",
        "--border": "rgba(255, 214, 51, 0.16)",
        "--border-strong": "rgba(255, 214, 51, 0.34)",
        "--bg": "#181405",
        "--bg-1": "#1d1808",
        "--bg-panel": "rgba(53, 45, 15, 0.68)",
        "--bg-panel-2": "rgba(52, 44, 19, 0.55)",
        "--bg-raised": "#2f2712",
      },
    },
    {
      id: "jarvis",
      name: "J.A.R.V.I.S",
      assistantName: "J.A.R.V.I.S",
      // Dry, unflappable, quietly sarcastic butler-bot — matches
      // SKIN_ATTITUDES' own "dry" default, which was modeled on Jarvis in
      // the first place, so this is really just making that implicit
      // match explicit for the preset picker.
      attitude: "dry",
      hex: "#4fd8ff",
      vars: {
        "--accent": "#4fd8ff",
        "--accent-soft": "#2ea9d6",
        "--accent-dim": "#164a63",
        "--accent-glow": "rgba(79, 216, 255, 0.35)",
        "--accent-secondary": "#f2b544",
        "--accent-tertiary": "#2b5cff",
        "--accent-secondary-rgb": "242, 181, 68",
        "--accent-tertiary-rgb": "43, 92, 255",
        "--status-online": "var(--green)",
        "--border": "rgba(102, 214, 255, 0.16)",
        "--border-strong": "rgba(102, 214, 255, 0.34)",
        "--bg": "#04070d",
        "--bg-1": "#070d16",
        "--bg-panel": "rgba(9, 18, 30, 0.68)",
        "--bg-panel-2": "rgba(13, 24, 38, 0.55)",
        "--bg-raised": "#0d1826",
      },
    },
    {
      // Accent (#ff4fd6) is the exact magenta sampled from the person's own
      // screenshot of a hand-tweaked pink skin earlier in this project —
      // the style.css they uploaded to source this from turned out to be
      // an untouched, pre-vivid-upgrade snapshot with no Friday colors in
      // it at all (the pink was only ever a runtime localStorage override,
      // never committed to a file), so this palette was reconstructed from
      // that screenshot instead of copied from the upload. Flagging this
      // in case the exact shade matters — happy to adjust hexes on request.
      id: "friday",
      name: "F.R.I.D.A.Y.",
      assistantName: "F.R.I.D.A.Y.",
      // MCU's Friday swapped in for Jarvis with a noticeably more
      // casual, cheeky, quick-with-a-quip energy (still competent and
      // loyal, just far less formal about it) — "snarky" is the closest
      // preset to that.
      attitude: "snarky",
      hex: "#ff4fd6",
      vars: {
        "--accent": "#ff4fd6",
        "--accent-soft": "#d62faf",
        "--accent-dim": "#631651",
        "--accent-glow": "rgba(255, 79, 214, 0.35)",
        "--accent-secondary": "#45f2b8",
        "--accent-tertiary": "#ff2a58",
        "--accent-secondary-rgb": "69, 242, 184",
        "--accent-tertiary-rgb": "255, 42, 88",
        "--status-online": "var(--accent)",
        "--border": "rgba(255, 102, 219, 0.16)",
        "--border-strong": "rgba(255, 102, 219, 0.34)",
        // Recomputed (see applyAccent()'s "IMPORTANT" comment) mixing
        // toward Friday's *dim* accent variant instead of her raw, bright
        // magenta — the raw-accent version baked in here previously came
        // out lighter/pinker than intended, the same background-washing
        // bug that applyAccent() had at runtime.
        "--bg": "#0c0812",
        "--bg-1": "#0f0e1b",
        "--bg-panel": "rgba(25, 19, 39, 0.68)",
        "--bg-panel-2": "rgba(27, 24, 45, 0.55)",
        "--bg-raised": "#19182c",
      },
    },
    {
      id: "edith",
      name: "E.D.I.T.H.",
      assistantName: "E.D.I.T.H.",
      // Tony's last AI, handed to Peter in Far From Home — clipped,
      // matter-of-fact, briefs you on drone strikes and threat data with
      // zero small talk. "formal" is the closest fit to that tone.
      attitude: "formal",
      hex: "#ff7a29",
      vars: {
        "--accent": "#ff7a29",
        "--accent-soft": "#c16126",
        "--accent-dim": "#572d13",
        "--accent-glow": "rgba(255, 122, 41, 0.35)",
        "--accent-secondary": "#244af0",
        "--accent-tertiary": "#ffed08",
        "--accent-secondary-rgb": "36, 74, 240",
        "--accent-tertiary-rgb": "255, 237, 8",
        "--status-online": "var(--accent)",
        "--border": "rgba(255, 135, 62, 0.16)",
        "--border-strong": "rgba(255, 135, 62, 0.34)",
        // Recomputed — see Friday's comment above; same fix, same reason.
        "--bg": "#0b0a0d",
        "--bg-1": "#0e1016",
        "--bg-panel": "rgba(23, 23, 28, 0.68)",
        "--bg-panel-2": "rgba(25, 27, 35, 0.55)",
        "--bg-raised": "#171b23",
      },
    },
    {
      id: "karen",
      name: "K.A.R.E.N.",
      assistantName: "K.A.R.E.N.",
      // Peter's suit AI in Homecoming — chatty, upbeat, genuinely
      // enthusiastic about helping (including unsolicited dating advice).
      // "warm" (Warm & Encouraging) is the closest match.
      attitude: "warm",
      hex: "#33e075",
      vars: {
        "--accent": "#33e075",
        "--accent-soft": "#36a05f",
        "--accent-dim": "#1a492c",
        "--accent-glow": "rgba(51, 224, 117, 0.35)",
        "--accent-secondary": "#d12e4d",
        "--accent-tertiary": "#20d4c8",
        "--accent-secondary-rgb": "209, 46, 77",
        "--accent-tertiary-rgb": "32, 212, 200",
        "--status-online": "var(--accent)",
        "--border": "rgba(68, 227, 128, 0.16)",
        "--border-strong": "rgba(68, 227, 128, 0.34)",
        // Recomputed — see Friday's comment above; same fix, same reason.
        "--bg": "#060c0f",
        "--bg-1": "#091218",
        "--bg-panel": "rgba(12, 28, 33, 0.68)",
        "--bg-panel-2": "rgba(15, 32, 39, 0.55)",
        "--bg-raised": "#0f1f27",
      },
    },
  ];

  // Tracks which preset (by id) is currently selected in the Skin modal, so
  // Save/Cancel/swatch-highlighting can tell a preset choice apart from a
  // raw custom color that just happens to match a preset's hex — see
  // renderSkinSwatches() and saveSkin(). null means "custom color, no
  // preset selected". Reset whenever the modal opens.
  let currentPresetId = null;

  // Same idea as currentPresetId, for PERSONA_PRESETS instead of
  // SKIN_PRESETS. The two are mutually exclusive in the UI (picking a
  // persona clears the accent-swatch selection and vice versa — see
  // applyPersonaPreset(), renderSkinSwatches()'s onclick, and the custom
  // color input handler in wireSkinModal()) since a persona's whole point
  // is a specific, non-derived palette; picking a plain accent afterward
  // means "no, override with a computed one instead."
  let currentPersonaId = null;

  // ===========================================================================
  // Registered personas — the dynamic counterpart to the hand-written
  // PERSONA_PRESETS above. A tool file (built-in actions/ or a user's own
  // ~/.jarvis/tools/) can expose a module-level PERSONAS list (see
  // jarvis/persona_registry.py and actions/_template.py §7); the backend
  // validates/normalizes those and GET /api/personas hands back the result
  // already logo-resolved (PNGs as base64 data URIs, SVG as raw markup) —
  // no further processing needed here beyond merging into the same UI
  // PERSONA_PRESETS already drives.
  //
  // Each entry, once loaded, has the SAME shape saveSkin()/applyPersonaPreset()
  // already expect from a PERSONA_PRESETS entry (id/name/assistantName/hex/
  // vars/attitude), plus fields no built-in preset has ever needed: addressAs
  // (a persona can pin "addresses you as" the same one click sets a palette),
  // logo (svg or png, see applyPersonaLogo()), interface ({radius, glow,
  // fontScale} — the Skin modal's "Interface" sliders), saturation (the
  // "Saturation" slider), and hardcoded (false for a persona that only gave a
  // "hex", meaning it goes through the normal accent-derivation math and the
  // saturation slider actually affects it — see isHardcodedPersona()).
  // ===========================================================================
  let REGISTERED_PERSONAS = [];
  // id -> label, merged into the Attitude <select> alongside SKIN_ATTITUDES.
  let REGISTERED_ATTITUDES = [];

  function allPersonas() {
    return PERSONA_PRESETS.concat(REGISTERED_PERSONAS);
  }

  function allAttitudes() {
    return SKIN_ATTITUDES.concat(REGISTERED_ATTITUDES);
  }

  // Every built-in PERSONA_PRESETS entry is a fixed, non-derived palette
  // (see that array's own comment) — always hardcoded. A registered persona
  // says so explicitly via its `hardcoded` flag (true when it gave `vars`,
  // false when it only gave `hex` — see persona_registry.py). Used by
  // updateSaturationControlState() to decide whether the slider actually
  // does anything for the currently active persona.
  function isHardcodedPersona(id) {
    if (!id) return false;
    const registered = REGISTERED_PERSONAS.find((p) => p.id === id);
    if (registered) return registered.hardcoded !== false;
    return true;
  }

  // Normalizes one raw /api/personas entry into the same shape
  // PERSONA_PRESETS entries already have, defensively — a malformed or
  // partially-invalid entry (should already be rare, since the backend
  // validated it, but this is still data crossing a process boundary) is
  // dropped rather than crashing the Skin modal.
  function normalizeRegisteredPersona(raw) {
    if (!raw || typeof raw !== "object") return null;
    const id = typeof raw.id === "string" ? raw.id.trim() : "";
    const name = typeof raw.name === "string" ? raw.name.trim() : "";
    if (!id || !name) return null;
    const hex = typeof raw.hex === "string" ? raw.hex : SKIN_DEFAULT_ACCENT;
    return {
      id,
      name,
      assistantName: (typeof raw.assistant_name === "string" && raw.assistant_name.trim()) || name,
      addressAs: typeof raw.address_user_as === "string" ? raw.address_user_as : null,
      attitude: typeof raw.attitude === "string" ? raw.attitude : null,
      hex,
      vars: raw.vars && typeof raw.vars === "object" ? raw.vars : null,
      hardcoded: raw.hardcoded !== false,
      logo: raw.logo && typeof raw.logo === "object" ? raw.logo : null,
      interface: raw.interface && typeof raw.interface === "object" ? raw.interface : null,
      saturation: typeof raw.saturation === "number" ? raw.saturation : null,
    };
  }

  // Fetches /api/personas once and merges the result into REGISTERED_
  // PERSONAS / REGISTERED_ATTITUDES, then re-renders anything already on
  // screen that depends on them. Called once at startup (see the bottom of
  // this file) — deliberately NOT awaited by anything else, since the app
  // (and the Skin modal) must stay fully usable with only the hardcoded
  // presets if the backend is slow, offline, or this request fails.
  async function loadRegisteredPersonas() {
    try {
      const data = await Api.getPersonas();
      const list = Array.isArray(data && data.personas) ? data.personas : [];
      REGISTERED_PERSONAS = list.map(normalizeRegisteredPersona).filter(Boolean);
      const attitudes = (data && data.attitudes && typeof data.attitudes === "object") ? data.attitudes : {};
      REGISTERED_ATTITUDES = Object.entries(attitudes).map(([id, a]) => ({
        id,
        label: (a && typeof a.label === "string" && a.label) || id,
      }));
    } catch (e) {
      // Offline/errored — leave REGISTERED_PERSONAS/ATTITUDES as whatever
      // they already were (empty on first load) and move on silently; this
      // is a nice-to-have layered on top of a fully working built-in set.
      return;
    }
    renderAttitudeOptions();
    // If the Skin modal happens to already be open, refresh what it's
    // showing so a newly-discovered persona/attitude appears without the
    // person having to close and reopen it.
    if (qs("#skin-backdrop") && !qs("#skin-backdrop").hidden) {
      renderPersonaPresets(currentPersonaId);
    }
    // Early boot (applySavedSkinEarly(), below) only ever had the
    // hardcoded PERSONA_PRESETS to search — if the browser's last-saved
    // personaId turns out to belong to a REGISTERED persona that just
    // arrived, apply its palette/logo/interface/saturation now instead of
    // leaving the page on whatever applySavedSkinEarly() fell back to.
    const prefs = loadSkinPrefs();
    if (prefs.personaId && !PERSONA_PRESETS.find((p) => p.id === prefs.personaId)) {
      const persona = REGISTERED_PERSONAS.find((p) => p.id === prefs.personaId);
      if (persona) {
        const themeActive = window.JarvisUI && window.JarvisUI.themes.isActive();
        if (!themeActive) {
          if (persona.vars) applyHardcodedVars(persona.vars);
          else applyAccent(persona.hex);
          applyPersonaLogo(persona.id);
          if (persona.interface && window.JarvisUI) window.JarvisUI.themes.setTuning(persona.interface);
        }
        applyAssistantNameToChrome(prefs.assistantName || persona.assistantName);
      }
    }
  }

  // The saturation slider's live value (percentage, 100 = unchanged),
  // mirrored here so applyAccent() can factor it in any time the accent
  // changes (swatch click, custom color drag, load, revert) without every
  // call site having to thread it through by hand. Never read by
  // applyHardcodedVars() — the "Classic (hardcoded)" preset bypasses
  // applyAccent() entirely and is unaffected by this slider no matter what
  // it's set to.
  let currentSaturationPercent = SKIN_DEFAULT_SATURATION;

  // Clamps a saturation percentage to the slider's own min/max, so a
  // corrupted localStorage value can't push it somewhere absurd.
  function clampSaturationPercent(percent) {
    return Math.max(
      SKIN_SATURATION_MIN,
      Math.min(SKIN_SATURATION_MAX, Number(percent) || SKIN_DEFAULT_SATURATION)
    );
  }

  // Enables/disables the saturation slider in the Skin modal — disabled
  // while "Classic (hardcoded)" is the active preset, since saturation
  // never applies to it (see applyAccent()/applyHardcodedVars()). The
  // slider's underlying value is left untouched so it's restored the
  // moment the person picks a different preset/color.
  function updateSaturationControlState() {
    const slider = qs("#skin-saturation");
    if (!slider) return;
    slider.disabled = currentPresetId === "classic-hardcoded" || isHardcodedPersona(currentPersonaId);
  }

  function loadSkinPrefs() {
    try {
      const raw = localStorage.getItem(SKIN_STORAGE_KEY);
      const parsed = raw ? JSON.parse(raw) : null;
      return parsed && typeof parsed === "object" ? parsed : {};
    } catch (e) {
      return {};
    }
  }

  function saveSkinPrefs(prefs) {
    try {
      localStorage.setItem(SKIN_STORAGE_KEY, JSON.stringify(prefs));
    } catch (e) {
      // Best-effort only — a full/blocked localStorage just means the
      // accent choice doesn't survive a reload, nothing else breaks.
    }
  }

  function hexToRgb(hex) {
    const m = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex || "");
    if (!m) return null;
    return { r: parseInt(m[1], 16), g: parseInt(m[2], 16), b: parseInt(m[3], 16) };
  }

  function rgbToHex({ r, g, b }) {
    const h = (n) => n.toString(16).padStart(2, "0");
    return `#${h(r)}${h(g)}${h(b)}`;
  }

  // rgb (0-255 channels) <-> hsl (h/s/l all 0-1) so soft/dim/border variants
  // can be derived by scaling *lightness and saturation* instead of naively
  // darkening each RGB channel by the same amount. Uniform RGB darkening
  // (the old approach) keeps a color's saturation maxed out no matter how
  // dark it gets, which reads as neon/plasticky against this app's muted
  // navy backdrop — the hand-picked defaults these variants were modeled on
  // pull *both* lightness and saturation down together (see the ratios in
  // applyAccent), which is what actually makes them look like shaded/dimmed
  // versions of the accent rather than a different, harsher color.
  function rgbToHsl({ r, g, b }) {
    r /= 255; g /= 255; b /= 255;
    const max = Math.max(r, g, b), min = Math.min(r, g, b);
    let h = 0, s = 0;
    const l = (max + min) / 2;
    const d = max - min;
    if (d !== 0) {
      s = d / (1 - Math.abs(2 * l - 1));
      switch (max) {
        case r: h = ((g - b) / d) % 6; break;
        case g: h = (b - r) / d + 2; break;
        default: h = (r - g) / d + 4; break;
      }
      h *= 60;
      if (h < 0) h += 360;
    }
    return { h, s, l };
  }

  function hslToRgb({ h, s, l }) {
    const c = (1 - Math.abs(2 * l - 1)) * s;
    const x = c * (1 - Math.abs(((h / 60) % 2) - 1));
    const m = l - c / 2;
    let r1 = 0, g1 = 0, b1 = 0;
    if (h < 60) [r1, g1, b1] = [c, x, 0];
    else if (h < 120) [r1, g1, b1] = [x, c, 0];
    else if (h < 180) [r1, g1, b1] = [0, c, x];
    else if (h < 240) [r1, g1, b1] = [0, x, c];
    else if (h < 300) [r1, g1, b1] = [x, 0, c];
    else [r1, g1, b1] = [c, 0, x];
    return {
      r: Math.round((r1 + m) * 255),
      g: Math.round((g1 + m) * 255),
      b: Math.round((b1 + m) * 255),
    };
  }

  // Scales a hex color's saturation/lightness by the given ratios (same hue),
  // clamped to valid HSL range. This is how accent-soft/-dim/-border are
  // derived below.
  function scaleHsl(rgb, sRatio, lRatio) {
    const hsl = rgbToHsl(rgb);
    hsl.s = Math.max(0, Math.min(1, hsl.s * sRatio));
    hsl.l = Math.max(0, Math.min(1, hsl.l * lRatio));
    return hslToRgb(hsl);
  }

  // Same as scaleHsl but also rotates hue by a fixed offset. Used to derive
  // --accent-secondary/--accent-tertiary (the "gold"/"blue" family used for
  // mode chips, badges, and JSON/log syntax coloring) from whatever accent
  // is picked, instead of leaving those two stuck at their old fixed hex
  // values — which is what made a non-cyan skin look unfinished (buttons
  // and borders reskin, but every badge/mode-chip/status-dot next to them
  // stays exactly the old cyan-era gold/blue/green and clashes). The offsets
  // (206°, +33°) are measured from the original design's own accent->gold
  // and accent->blue hue gaps, so at the default cyan accent this reduces
  // to (almost exactly) the original #f2b544/#2b5cff — same reasoning as
  // scaleHsl reproducing the original soft/dim.
  function rotateHue(rgb, hueDeltaDeg, sRatio, lRatio) {
    const hsl = rgbToHsl(rgb);
    hsl.h = (hsl.h + hueDeltaDeg + 360) % 360;
    hsl.s = Math.max(0, Math.min(1, hsl.s * sRatio));
    hsl.l = Math.max(0, Math.min(1, hsl.l * lRatio));
    return hslToRgb(hsl);
  }

  // Blends a base color toward the chosen accent by `amount` (0-1). Used to
  // tint the near-black background layers and structural borders (see
  // applyAccent) just enough that the app's atmosphere shifts hue with the
  // skin, without washing everything out in a fully saturated color.
  function mixTowardAccent(base, accent, amount) {
    return {
      r: Math.round(base.r * (1 - amount) + accent.r * amount),
      g: Math.round(base.g * (1 - amount) + accent.g * amount),
      b: Math.round(base.b * (1 - amount) + accent.b * amount),
    };
  }

  // The hand-picked near-black bases each --bg* token in style.css starts
  // from, before any accent tint is mixed in (see mixTowardAccent above).
  const BG_BASE = { r: 0x04, g: 0x07, b: 0x0d };       // --bg
  const BG1_BASE = { r: 0x07, g: 0x0d, b: 0x16 };       // --bg-1
  const BG_PANEL_BASE = { r: 9, g: 18, b: 30 };         // --bg-panel (alpha 0.68)
  const BG_PANEL2_BASE = { r: 13, g: 24, b: 38 };       // --bg-panel-2 (alpha 0.55)
  const BG_RAISED_BASE = { r: 13, g: 24, b: 38 };       // --bg-raised

  // Applies one accent hex to every --accent* CSS var the whole stylesheet
  // is built from, deriving the soft/dim/border/glow variants the same way
  // the hand-picked defaults in style.css actually relate to each other.
  // Measured against those defaults (accent #4fd8ff -> soft #2ea9d6, dim
  // #164a63, border rgba(102,214,255,.16/.34)): soft is accent's saturation
  // x0.67 and lightness x0.78; dim is saturation x0.64 and lightness x0.36;
  // border is a touch *lighter* than accent (lightness x1.07, full
  // saturation) shown at 0.16/0.34 alpha rather than as a solid fill; glow
  // is the accent itself at 35% alpha. Scaling saturation down alongside
  // lightness (instead of just darkening RGB channels toward black) is what
  // keeps soft/dim reading as shaded versions of the accent rather than a
  // separate neon color — see scaleHsl above.
  //
  // Also lightly re-tints the --bg* background layers with the same accent
  // (kept subtle — a light wash, not a full recolor) so picking a skin
  // shifts the app's overall mood a bit, not just its buttons. At the
  // default cyan accent this reduces to (almost exactly) the original fixed
  // values, so nothing changes unless you actually pick a different color.
  // Applies an accent color. hue/lightness are taken from `hex` as-is;
  // saturation is scaled by the saturation slider (currentSaturationPercent,
  // 100% = the color's own true saturation, unchanged) before anything is
  // derived from it. There used to be a hardcoded cap here forcing any
  // custom-picked color's saturation down to 65% "to avoid neon" — that was
  // the actual bug making every non-hardcoded color look diluted/whitish
  // (a fully-saturated pick could never render as fully saturated). Removed:
  // the saturation slider is the real, reversible control for that now, so
  // a fixed cap baked into the pipeline no longer serves any purpose except
  // adding unwanted white.
  function applyAccent(hex) {
    const rawRgb = hexToRgb(hex);
    if (!rawRgb) return;
    const rgb = scaleHsl(rawRgb, currentSaturationPercent / 100, 1);
    const root = document.documentElement.style;
    const appliedHex = rgbToHex(rgb);
    const soft = scaleHsl(rgb, 0.67, 0.78);
    const dim = scaleHsl(rgb, 0.64, 0.36);
    const borderRgb = scaleHsl(rgb, 1, 1.07);
    root.setProperty("--accent", appliedHex);
    root.setProperty("--accent-soft", rgbToHex(soft));
    root.setProperty("--accent-dim", rgbToHex(dim));
    root.setProperty("--accent-glow", `rgba(${rgb.r}, ${rgb.g}, ${rgb.b}, 0.35)`);
    root.setProperty("--border", `rgba(${borderRgb.r}, ${borderRgb.g}, ${borderRgb.b}, 0.16)`);
    root.setProperty("--border-strong", `rgba(${borderRgb.r}, ${borderRgb.g}, ${borderRgb.b}, 0.34)`);

    // The "gold"/"blue" family used for mode chips, badges, and JSON/log
    // syntax coloring — see rotateHue's comment above for why these move
    // with the skin now instead of staying fixed.
    const secondary = rotateHue(rgb, 206, 0.87, 0.93);
    const tertiary = rotateHue(rgb, 33, 1.0, 0.89);
    root.setProperty("--accent-secondary", rgbToHex(secondary));
    root.setProperty("--accent-tertiary", rgbToHex(tertiary));
    // Triplet form for the rgba(var(--accent-secondary-rgb), alpha) calls in
    // style.css — see that var's comment for why this exists alongside the
    // hex one above.
    root.setProperty("--accent-secondary-rgb", `${secondary.r}, ${secondary.g}, ${secondary.b}`);
    root.setProperty("--accent-tertiary-rgb", `${tertiary.r}, ${tertiary.g}, ${tertiary.b}`);

    // ONLINE status color: tracks the accent (like the boot ring) for every
    // color chosen through this function. The one true "always green, never
    // tracks the accent" look lives in the dedicated hardcoded preset below
    // (applyHardcodedVars) instead of being special-cased here — see that
    // preset's comment for why a hex comparison isn't a reliable way to
    // detect "the user wants the classic look".
    root.setProperty("--status-online", "var(--accent)");

    // Tint amount was previously so small (0.018-0.045) that the background
    // barely shifted hue no matter which accent was picked, leaving the
    // "deep space" backdrop looking like a leftover from the cyan default
    // under any other skin. Bumped ~4-5x so the app's overall mood actually
    // reads as tinted — still nowhere near a full recolor (these are still
    // near-black).
    //
    // IMPORTANT: mixed toward `dim` (the same darkened/desaturated accent
    // variant --accent-dim is built from — scaleHsl(rgb, 0.64, 0.36), see
    // above), NOT toward the raw, full-lightness `rgb`. Mixing toward the
    // raw accent was the actual bug behind backgrounds reading as washed-
    // out/whiter than "Classic (hardcoded)" even at the identical cyan
    // hex: the default accent is ~65% lightness, so blending even a small
    // amount of it straight in pulls every --bg* token substantially
    // *lighter* — toward that near-white lightness — instead of toward a
    // moody, dark shade of it. `dim` is already darkened the same way the
    // rest of this function treats "a shaded version of the accent," so
    // mixing toward it keeps backgrounds near-black (matching Classic's
    // hand-picked values almost exactly at the default cyan accent) while
    // still hue-shifting with whatever color is picked.
    const bg = mixTowardAccent(BG_BASE, dim, 0.08);
    const bg1 = mixTowardAccent(BG1_BASE, dim, 0.09);
    const bgPanel = mixTowardAccent(BG_PANEL_BASE, dim, 0.18);
    const bgPanel2 = mixTowardAccent(BG_PANEL2_BASE, dim, 0.16);
    const bgRaised = mixTowardAccent(BG_RAISED_BASE, dim, 0.14);
    root.setProperty("--bg", rgbToHex(bg));
    root.setProperty("--bg-1", rgbToHex(bg1));
    root.setProperty("--bg-panel", `rgba(${bgPanel.r}, ${bgPanel.g}, ${bgPanel.b}, 0.68)`);
    root.setProperty("--bg-panel-2", `rgba(${bgPanel2.r}, ${bgPanel2.g}, ${bgPanel2.b}, 0.55)`);
    root.setProperty("--bg-raised", rgbToHex(bgRaised));
  }

  // Applies a preset's literal, pre-computed CSS var values directly — no
  // HSL math, no accent-derivation, nothing that could drift from the
  // original numbers as the formulas above get tuned over time. Used only
  // by the "Classic (hardcoded)" preset (see SKIN_PRESETS); every other
  // preset/custom color goes through applyAccent() instead.
  function applyHardcodedVars(vars) {
    const root = document.documentElement.style;
    for (const [prop, value] of Object.entries(vars)) {
      root.setProperty(prop, value);
    }
  }

  // Single entry point for "apply whichever preset this is" — dispatches to
  // applyHardcodedVars() for the literal classic preset, applyAccent() for
  // every hand-picked/curated color. Callers that just have a hex (custom
  // color input, or no matching preset id) should call applyAccent()
  // directly instead.
  function applyPreset(preset) {
    if (preset.hardcoded) applyHardcodedVars(preset.vars);
    else applyAccent(preset.hex);
  }

  // Live-preview a persona preset: applies its hardcoded palette (never
  // applyAccent() — see PERSONA_PRESETS' comment) and fills in the name
  // field, but does NOT push the name to the topbar/chrome yet. That
  // mirrors how typing a name by hand already works here — chrome only
  // picks up a new name on Save (applyAssistantNameToChrome is called
  // there) — so Cancel reverting to the last-saved persona/preset/accent
  // (see closeSkinModal, which re-reads from localStorage rather than any
  // of these in-memory picks) also correctly leaves an unsaved persona's
  // name un-adopted, not just its colors.
  function applyPersonaPreset(persona) {
    if (window.JarvisUI) window.JarvisUI.themes.deactivate();
    currentPersonaId = persona.id;
    currentPresetId = null;
    qs("#skin-custom-color").value = persona.hex;
    qs("#skin-assistant-name").value = persona.assistantName;
    qs("#skin-attitude").value = persona.attitude || SKIN_DEFAULT_ATTITUDE;
    // Only a registered persona ever sets this — none of the hand-written
    // PERSONA_PRESETS pin an address, so leave whatever's already typed
    // there alone unless this persona actually specifies one.
    if (persona.addressAs) qs("#skin-address-as").value = persona.addressAs;
    // Every PERSONA_PRESETS entry always has `vars` (a fixed palette); a
    // registered persona that only gave `hex` instead runs through the
    // normal accent-derivation math, same as picking a plain accent color.
    if (persona.vars) applyHardcodedVars(persona.vars);
    else applyAccent(persona.hex);
    applyPersonaLogo(persona.id);
    // A registered persona can also pin a saturation and/or Interface
    // (corner rounding / glow / text size) setting — apply and reflect
    // them in the sliders themselves, the same way opening the modal
    // already syncs #skin-saturation from a saved value.
    if (persona.saturation != null) {
      currentSaturationPercent = clampSaturationPercent(persona.saturation);
      const saturationSlider = qs("#skin-saturation");
      if (saturationSlider) saturationSlider.value = currentSaturationPercent;
    }
    if (persona.interface) {
      if (window.JarvisUI) window.JarvisUI.themes.setTuning(persona.interface);
      const radius = qs("#tune-radius");
      if (radius && persona.interface.radius != null) radius.value = persona.interface.radius;
      const glow = qs("#tune-glow");
      if (glow && persona.interface.glow != null) glow.value = persona.interface.glow;
      const font = qs("#tune-font");
      if (font && persona.interface.fontScale != null) font.value = persona.interface.fontScale;
    }
    renderSkinSwatches(null);
    renderPersonaPresets(persona.id);
    updateSaturationControlState();
    updateCliNamePreview();
  }

  // Updates every place the assistant's name is echoed back in the UI
  // chrome itself: topbar title, Ask panel title, the "Ask <name>" button,
  // the boot sequence's own title line, and the page title. The boot title
  // used to be the one deliberately-fixed spot ("brand mark stays fixed
  // like a car's dashboard logo") — that's been reconsidered: a renamed
  // assistant should look renamed everywhere, including the loading
  // screen you see before the app itself renders, not just once you're
  // inside it. applySavedSkinEarly() (below) already calls this before
  // boot even paints, so there's no flash of "J.A.R.V.I.S" first.
  function applyAssistantNameToChrome(name) {
    const clean = (name || "").trim() || SKIN_DEFAULT_NAME;
    const topbarTitle = qs("#topbar-title");
    if (topbarTitle) topbarTitle.textContent = clean;
    const askTitle = qs("#ask-panel-title");
    if (askTitle) askTitle.textContent = `ASK ${clean.toUpperCase()}`;
    const askButtonLabel = qs("#btn-ask-jarvis-label");
    if (askButtonLabel) askButtonLabel.textContent = clean;
    const bootTitle = qs("#boot-title");
    if (bootTitle) bootTitle.textContent = clean;
    document.title = `${clean} — Command Interface`;
  }

  // Single source of truth for "whatever the assistant is currently named"
  // — used anywhere we need to label the assistant's own output (e.g. the
  // Ask thread's role labels) instead of hardcoding "Jarvis". Reads back
  // from the topbar chrome (kept in sync by applyAssistantNameToChrome)
  // rather than tracking a separate variable, so it's never able to drift
  // out of sync with what's actually shown as the assistant's name.
  function currentAssistantName() {
    const topbarTitle = qs("#topbar-title");
    const name = topbarTitle && topbarTitle.textContent.trim();
    return name || SKIN_DEFAULT_NAME;
  }

  // Applied once at script start (before boot even renders) from whatever
  // was last saved locally, so there's no flash of default cyan/"J.A.R.V.I.S"
  // before the real ai_config.json persona loads a moment later. The persona
  // half gets refreshed again with the server's actual value once Skin is
  // opened or the config is fetched — this is just a fast, best-effort guess.
  (function applySavedSkinEarly() {
    const prefs = loadSkinPrefs();
    currentSaturationPercent = clampSaturationPercent(prefs.saturation);
    const persona = PERSONA_PRESETS.find((p) => p.id === prefs.personaId);
    // If the NEW theme gallery (ui-kit.js) is what the user last touched,
    // it already painted the page before this script ran (see index.html's
    // load order) — don't immediately overwrite it with the legacy accent
    // restore. window.JarvisUI is guaranteed to exist here since ui-kit.js
    // is loaded first and runs synchronously.
    //
    // BUGFIX: the persona branch used to run BEFORE this check instead of
    // behind it — `if (persona) { ...unconditional... } else if (!themeActive)`
    // — so any saved personaId (the common case; a default persona is
    // normally set) reapplied its hardcoded palette regardless of whether a
    // new-system theme was active. Persona palettes don't define
    // --text/--text-dim/--text-dimmer, so the visible result was exactly
    // this bug: every colour reverted except text. Both colour-applying
    // branches now live inside the SAME `!themeActive` guard so neither can
    // bypass it.
    const themeActive = window.JarvisUI && window.JarvisUI.themes.isActive();
    if (!themeActive) {
      if (persona) {
        applyHardcodedVars(persona.vars);
        applyPersonaLogo(persona.id);
        applyAssistantNameToChrome(prefs.assistantName || persona.assistantName);
      } else {
        const preset = SKIN_PRESETS.find((p) => p.id === prefs.presetId);
        if (preset) applyPreset(preset);
        else if (prefs.accent) applyAccent(prefs.accent);
        else applyPreset(SKIN_PRESETS.find((p) => p.id === "cyan"));
        applyPersonaLogo(null);
        if (prefs.assistantName) applyAssistantNameToChrome(prefs.assistantName);
      }
    } else if (prefs.assistantName) {
      // Colours are the new system's call, but the persona name in the
      // header chrome is independent of both and still applies either way.
      applyAssistantNameToChrome(persona ? (prefs.assistantName || persona.assistantName) : prefs.assistantName);
    }
  })();

  // `activePresetId` is whatever's currently selected (or null for "custom
  // color, no preset"). Matching by id — not by re-checking each preset's
  // hex against the color input's current value — is what lets "Cyan
  // (default)" and "Classic (hardcoded)" both render as the same-colored
  // swatch without one falsely lighting up as active for the other.
  function renderSkinSwatches(activePresetId) {
    const wrap = qs("#skin-swatches");
    if (!wrap) return;
    wrap.innerHTML = "";
    for (const preset of SKIN_PRESETS) {
      const isActive = preset.id === activePresetId;
      wrap.appendChild(el("button", {
        type: "button",
        class: "skin-swatch" + (isActive ? " is-active" : ""),
        style: `background:${preset.hex}; color:${preset.hex};`,
        title: preset.name,
        onclick: () => {
          // Hand colour control back to this (older) picker — see
          // ACTIVE_KEY's comment in ui-kit.js for why this matters: without
          // it, the two systems fight over the same CSS variables and
          // whichever runs its "restore on close" logic last silently wins.
          if (window.JarvisUI) window.JarvisUI.themes.deactivate();
          qs("#skin-custom-color").value = preset.hex;
          currentPresetId = preset.id;
          currentPersonaId = null;
          applyPreset(preset);
          applyPersonaLogo(null);
          renderSkinSwatches(preset.id);
          renderPersonaPresets(null);
          updateSaturationControlState();
        },
      }));
    }
  }

  // Same pattern as renderSkinSwatches, for PERSONA_PRESETS. Rendered as
  // labeled pills rather than bare color circles since a persona is a name
  // + palette bundle, not just a color — the label is what actually
  // identifies "which persona is this" at a glance. Each button's inline
  // `color` is set to the persona's hex so `currentColor` in .persona-
  // preset-btn/__dot's CSS (border, glow, dot fill) picks it up without a
  // dozen hardcoded color rules in style.css; the label itself overrides
  // back to var(--text) there so it stays legible regardless of hue.
  function renderPersonaPresets(activeId) {
    const wrap = qs("#skin-persona-presets");
    if (!wrap) return;
    wrap.innerHTML = "";
    for (const persona of allPersonas()) {
      const isActive = persona.id === activeId;
      const attitudeLabel = (allAttitudes().find((a) => a.id === persona.attitude) || {}).label || persona.attitude;
      const title = attitudeLabel
        ? `${persona.name} — sets the name, a palette, and a "${attitudeLabel}" attitude together`
        : `${persona.name} — sets the name and a palette`;
      wrap.appendChild(el("button", {
        type: "button",
        class: "persona-preset-btn" + (isActive ? " is-active" : ""),
        style: `color:${persona.hex};`,
        title,
        onclick: () => applyPersonaPreset(persona),
      }, [
        el("span", { class: "persona-preset-btn__dot" }),
        el("span", { class: "persona-preset-btn__label" }, persona.name),
      ]));
    }
  }

  // Populates the Attitude <select> once with the fixed list of presets
  // (see SKIN_ATTITUDES above) — the options themselves never change at
  // runtime, only which one is selected, so this only needs to run once.
  function renderAttitudeOptions() {
    const select = qs("#skin-attitude");
    if (!select) return;
    // Re-render-safe (not just once): loadRegisteredPersonas() calls this
    // again once any custom attitude a persona registered has arrived, so
    // it can show up in the dropdown without a page reload. Preserve
    // whatever's currently selected across the rebuild, in case the Skin
    // modal is already open with a choice made.
    const prevValue = select.value;
    select.innerHTML = "";
    for (const attitude of allAttitudes()) {
      select.appendChild(el("option", { value: attitude.id }, attitude.label));
    }
    if (prevValue && [...select.options].some((o) => o.value === prevValue)) {
      select.value = prevValue;
    }
  }

  async function openSkinModal() {
    qs("#skin-error").textContent = "";
    const prefs = loadSkinPrefs();
    const accent = prefs.accent || SKIN_DEFAULT_ACCENT;
    const matchedPersona = allPersonas().find((p) => p.id === prefs.personaId);
    currentPersonaId = matchedPersona ? matchedPersona.id : null;
    // A saved persona always wins the accent-swatch slot too — the two are
    // mutually exclusive (see currentPersonaId's comment) — so only look
    // for a matching SKIN_PRESETS entry when no persona is active.
    const matchedPreset = currentPersonaId ? null : SKIN_PRESETS.find((p) => p.id === prefs.presetId);
    currentPresetId = matchedPreset ? matchedPreset.id : null;
    qs("#skin-custom-color").value = matchedPersona ? matchedPersona.hex : accent;
    renderSkinSwatches(currentPresetId);
    renderPersonaPresets(currentPersonaId);
    currentSaturationPercent = clampSaturationPercent(prefs.saturation);
    const saturationSlider = qs("#skin-saturation");
    if (saturationSlider) saturationSlider.value = currentSaturationPercent;
    updateSaturationControlState();

    // Persona fields are authoritative on the server (ai_config.json), not
    // in localStorage — always fetch the real current value so Skin never
    // shows/saves-over a stale local guess if ai_config.json was hand-edited
    // or changed from another browser/session since the last visit here.
    qs("#skin-assistant-name").value = prefs.assistantName || SKIN_DEFAULT_NAME;
    qs("#skin-address-as").value = prefs.addressAs || SKIN_DEFAULT_ADDRESS;
    qs("#skin-attitude").value = SKIN_DEFAULT_ATTITUDE;
    try {
      const { text } = await Api.getConfigFile("ai_config.json");
      const parsed = JSON.parse(text);
      const persona = (parsed && parsed.persona) || {};
      qs("#skin-assistant-name").value = persona.assistant_name || SKIN_DEFAULT_NAME;
      qs("#skin-address-as").value = persona.address_user_as || SKIN_DEFAULT_ADDRESS;
      qs("#skin-attitude").value = persona.attitude || SKIN_DEFAULT_ATTITUDE;
    } catch (e) {
      // Fall back to whatever localStorage/defaults already filled in above
      // — Skin should still be usable (accent at least) even if the CLI
      // backend is offline or ai_config.json is missing/corrupt.
    }
    updateCliNamePreview();
    qs("#skin-backdrop").hidden = false;
  }

  function closeSkinModal() {
    qs("#skin-backdrop").hidden = true;
    // Revert any live-preview accent/saturation back to whatever's actually
    // saved, in case the person clicked around swatches or dragged the
    // slider and then hit Cancel. Same fork as applySavedSkinEarly(): if the
    // new theme gallery is the active system, restoring ITS saved theme is
    // the correct "back to what's actually saved" — reapplying the legacy
    // accent here was the bug (see ACTIVE_KEY's comment in ui-kit.js).
    if (window.JarvisUI && window.JarvisUI.themes.isActive()) {
      window.JarvisUI.themes.apply();
      return;
    }
    const prefs = loadSkinPrefs();
    currentSaturationPercent = clampSaturationPercent(prefs.saturation);
    const persona = allPersonas().find((p) => p.id === prefs.personaId);
    if (persona) {
      if (persona.vars) applyHardcodedVars(persona.vars);
      else applyAccent(persona.hex);
      applyPersonaLogo(persona.id);
      if (persona.interface && window.JarvisUI) window.JarvisUI.themes.setTuning(persona.interface);
      return;
    }
    const preset = SKIN_PRESETS.find((p) => p.id === prefs.presetId);
    if (preset) applyPreset(preset);
    else if (prefs.accent) applyAccent(prefs.accent);
    else applyPreset(SKIN_PRESETS.find((p) => p.id === "cyan"));
    applyPersonaLogo(null);
  }

  async function saveSkin() {
    const accent = qs("#skin-custom-color").value || SKIN_DEFAULT_ACCENT;
    const assistantName = qs("#skin-assistant-name").value.trim() || SKIN_DEFAULT_NAME;
    const addressAs = qs("#skin-address-as").value.trim() || SKIN_DEFAULT_ADDRESS;
    const attitude = qs("#skin-attitude").value || SKIN_DEFAULT_ATTITUDE;
    const errEl = qs("#skin-error");
    errEl.textContent = "";

    try {
      const { text } = await Api.getConfigFile("ai_config.json");
      const parsed = JSON.parse(text);
      parsed.persona = parsed.persona || {};
      parsed.persona.assistant_name = assistantName;
      parsed.persona.address_user_as = addressAs;
      parsed.persona.attitude = attitude;
      await Api.putConfigFile("ai_config.json", JSON.stringify(parsed, null, 2));
    } catch (e) {
      errEl.textContent = `Couldn't save persona: ${e.message}`;
      return;
    }

    saveSkinPrefs({ accent, presetId: currentPresetId, personaId: currentPersonaId, assistantName, addressAs, saturation: currentSaturationPercent });
    const persona = allPersonas().find((p) => p.id === currentPersonaId);
    const preset = SKIN_PRESETS.find((p) => p.id === currentPresetId);
    const themeActive = window.JarvisUI && window.JarvisUI.themes.isActive();
    // BUGFIX: `persona` and `preset` used to be checked BEFORE `themeActive`,
    // so a stale currentPersonaId/currentPresetId (openSkinModal() seeds
    // both from localStorage every time the modal opens, whether or not the
    // new theme gallery is what's actually in effect) reapplied that old
    // palette on every Save — reverting the active theme's colours except
    // text, since neither applyHardcodedVars() nor applyAccent() touch
    // --text/--text-dim/--text-dimmer. All three colour-applying branches
    // now sit behind the ONE `!themeActive` check, matching closeSkinModal()
    // and the boot restore above.
    if (!themeActive) {
      if (persona) {
        if (persona.vars) applyHardcodedVars(persona.vars);
        else applyAccent(persona.hex);
      } else if (preset) {
        applyPreset(preset);
      } else {
        applyAccent(accent);
      }
      applyPersonaLogo(currentPersonaId);
    }
    applyAssistantNameToChrome(assistantName);
    qs("#skin-backdrop").hidden = true;
    toast("Skin saved.", "info");
  }

  function resetSkinToDefaults() {
    qs("#skin-assistant-name").value = SKIN_DEFAULT_NAME;
    qs("#skin-address-as").value = SKIN_DEFAULT_ADDRESS;
    qs("#skin-attitude").value = SKIN_DEFAULT_ATTITUDE;
    qs("#skin-custom-color").value = SKIN_DEFAULT_ACCENT;
    currentPresetId = "cyan";
    currentPersonaId = null;
    currentSaturationPercent = SKIN_DEFAULT_SATURATION;
    const saturationSlider = qs("#skin-saturation");
    if (saturationSlider) saturationSlider.value = SKIN_DEFAULT_SATURATION;
    applyPreset(SKIN_PRESETS.find((p) => p.id === "cyan"));
    applyPersonaLogo(null);
    renderSkinSwatches(currentPresetId);
    renderPersonaPresets(null);
    updateSaturationControlState();
    updateCliNamePreview();
  }

  function wireSkinModal() {
    renderAttitudeOptions();
    qs("#btn-skin").addEventListener("click", openSkinModal);
    qs("#skin-close").addEventListener("click", closeSkinModal);
    qs("#btn-skin-cancel").addEventListener("click", closeSkinModal);
    qs("#skin-backdrop").addEventListener("click", (e) => {
      if (e.target === qs("#skin-backdrop")) closeSkinModal();
    });
    qs("#btn-skin-save").addEventListener("click", saveSkin);
    qs("#btn-skin-reset").addEventListener("click", resetSkinToDefaults);
    // Live preview of the CLI command name as the person types a new name —
    // see updateCliNamePreview()/sanitizeCliName() above. Purely visual;
    // the actual exe/command isn't renamed until the next script.bat run.
    qs("#skin-assistant-name").addEventListener("input", updateCliNamePreview);
    // Live preview while picking a custom color, same as clicking a swatch.
    // A manually typed/picked color is never a preset, even if it happens
    // to match one's hex — see renderSkinSwatches()'s id-matching comment.
    qs("#skin-custom-color").addEventListener("input", (e) => {
      if (window.JarvisUI) window.JarvisUI.themes.deactivate();
      currentPresetId = null;
      currentPersonaId = null;
      applyAccent(e.target.value);
      applyPersonaLogo(null);
      renderSkinSwatches(null);
      renderPersonaPresets(null);
      updateSaturationControlState();
    });
    // Saturation slider — live preview. Since saturation is baked into the
    // same HSL derivation as everything else in applyAccent() (not a
    // separate filter), moving the slider re-runs the whole derivation
    // against whatever's currently active: the matched preset if one's
    // selected, otherwise the raw custom-color value. Guarded against the
    // hardcoded preset for safety even though the slider is also disabled
    // (via updateSaturationControlState) whenever "Classic (hardcoded)" is
    // active, so this shouldn't normally fire in that state at all.
    const saturationSlider = qs("#skin-saturation");
    if (saturationSlider) {
      saturationSlider.addEventListener("input", (e) => {
        if (window.JarvisUI) window.JarvisUI.themes.deactivate();
        currentSaturationPercent = clampSaturationPercent(e.target.value);
        if (currentPresetId === "classic-hardcoded" || currentPersonaId) return;
        const preset = SKIN_PRESETS.find((p) => p.id === currentPresetId);
        if (preset) applyPreset(preset);
        else applyAccent(qs("#skin-custom-color").value);
      });
    }
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function highlightTokens(runText) {
    const escaped = escapeHtml(runText);
    return escaped.replace(/\{\{|\}\}|\{[a-zA-Z0-9_]+\}/g, (m) => {
      if (m === "{{" || m === "}}") return m;
      return `<span class="tok">${m}</span>`;
    });
  }

  // Lightweight JSON syntax coloring for the confirmation prompts (the
  // "are you sure" popups shown before a flagged tool/command runs — see
  // showRunConfirmPopup, addAskConfirmBubble, renderResolvedConfirmBubble,
  // debugRenderConfirmPending). Those used to just dump a plain
  // JSON.stringify(..., null, 2) into a <pre>, the same undifferentiated
  // block of text as the Settings tab's read-only raw JSON viewer. This
  // colors keys/strings/numbers/booleans/null so the arguments and
  // resolved command content are actually easy to scan at a glance before
  // approving something. Only ever reads its own escaped output back into
  // innerHTML, so nothing here can inject anything the value itself didn't
  // already contain (already escaped first).
  function jsonSyntaxHtml(value) {
    // Match on the RAW JSON text, not an already-escaped copy — escaping
    // first turns every `"` into `&quot;`, which the string-matching part
    // of this regex would never see. Every match gets escaped individually
    // inside the callback instead; the only characters left untouched are
    // JSON's own structural punctuation/whitespace ({}[]:, and newlines),
    // none of which need HTML-escaping.
    const json = JSON.stringify(value, null, 2);
    return json.replace(
      /("(?:\\u[a-fA-F0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\btrue\b|\bfalse\b|\bnull\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g,
      (match) => {
        let cls = "json-num";
        if (match.startsWith('"')) {
          cls = /:\s*$/.test(match) ? "json-key" : "json-str";
        } else if (match === "true" || match === "false") {
          cls = "json-bool";
        } else if (match === "null") {
          cls = "json-null";
        }
        return `<span class="${cls}">${escapeHtml(match)}</span>`;
      },
    );
  }

  // A confirmation prompt's "Command:" line is sometimes a plain shell
  // string (a saved command's `run`, not JSON) and sometimes a JSON value
  // (resolved_run_for_review's {command, vars, run} object, or a chain's
  // list of those) — color it as JSON only when it actually is some.
  function confirmValuePre(value, className) {
    const html = typeof value === "string" ? escapeHtml(value) : jsonSyntaxHtml(value);
    return el("pre", { class: className, html });
  }

  function describeCondition(cond) {
    if (cond == null) return null;
    if (typeof cond === "string") return cond;
    if (typeof cond === "object") {
      return Object.entries(cond)
        .map(([k, v]) => (Array.isArray(v) ? `${k} in [${v.join(", ")}]` : `${k} == ${v}`))
        .join(" and ");
    }
    return String(cond);
  }

  // ===========================================================================
  // Shared JSON tree component — collapsible, optionally editable.
  //
  // Used by (1) the "organize-json <path>" chat-bubble result and (2) the
  // Settings modal's Config tab. Renders straight from an already-parsed JS
  // value (never re-serializes to send anywhere), so browsing/editing a
  // config file here never touches the AI layer or costs a token.
  // ===========================================================================

  function jsonTreeTypeOf(v) {
    if (v === null) return "null";
    if (Array.isArray(v)) return "array";
    return typeof v; // "object" | "string" | "number" | "boolean"
  }

  function jsonTreeIsContainer(v) {
    return v !== null && typeof v === "object";
  }

  function jsonTreeCountLabel(v) {
    if (Array.isArray(v)) {
      const n = v.length;
      return `[ ]  ${n} item${n === 1 ? "" : "s"}`;
    }
    const n = Object.keys(v).length;
    return `{ }  ${n} key${n === 1 ? "" : "s"}`;
  }

  function jsonTreeScalarLabel(v) {
    if (v === null) return "null";
    if (typeof v === "boolean") return v ? "true" : "false";
    if (typeof v === "string") return JSON.stringify(v);
    return String(v);
  }

  // Turns whatever text a person typed into an editable leaf back into a
  // proper JS value, the way JSON itself would read it — "true"/"false"/
  // "null"/numbers become their real types, anything else (including text
  // that merely looks numeric-ish but isn't, or fails to parse) stays a
  // plain string so nothing is silently misinterpreted.
  function jsonTreeCoerce(raw) {
    const t = String(raw);
    if (t === "null") return null;
    if (t === "true") return true;
    if (t === "false") return false;
    if (/^-?\d+(\.\d+)?([eE][-+]?\d+)?$/.test(t.trim()) && t.trim() !== "") return Number(t);
    return t;
  }

  // Builds a read-only OR editable collapsible tree for `rootValue` inside
  // `mount`. Editable mode mutates `rootValue` in place (objects/arrays are
  // references) and calls `onChange()` after every structural or leaf edit
  // so the caller can re-derive raw JSON text for saving/Raw-view.
  function buildJsonTree(mount, rootValue, { editable = false, onChange = () => {} } = {}) {
    const collapsedPaths = new Set(); // paths the user explicitly collapsed
    const expandedPaths = new Set();  // paths the user explicitly expanded

    function isOpen(pathKey, depth) {
      if (collapsedPaths.has(pathKey)) return false;
      if (expandedPaths.has(pathKey)) return true;
      return depth < 2; // default: first two levels open, rest start collapsed
    }

    function rerender() {
      mount.innerHTML = "";
      if (jsonTreeIsContainer(rootValue)) {
        mount.appendChild(el("div", { class: "json-tree__meta json-tree__meta--root" }, jsonTreeCountLabel(rootValue)));
        buildChildren(mount, rootValue, "$", 0);
      } else {
        mount.appendChild(el("div", { class: "json-tree__row" }, [
          el("span", { class: `json-tree__val json-tree__val--${jsonTreeTypeOf(rootValue)}` }, jsonTreeScalarLabel(rootValue)),
        ]));
      }
    }

    function startEditValue(container, key, valEl) {
      const current = container[key];
      const input = el("input", {
        class: "json-tree__edit-input",
        type: "text",
        value: jsonTreeTypeOf(current) === "string" ? current : jsonTreeScalarLabel(current),
      });
      valEl.replaceWith(input);
      input.focus();
      input.select();
      const commit = () => {
        container[key] = jsonTreeCoerce(input.value);
        onChange();
        rerender();
      };
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); commit(); }
        else if (e.key === "Escape") { e.preventDefault(); rerender(); }
      });
      input.addEventListener("blur", commit);
    }

    function startRenameKey(container, key, keyEl) {
      const input = el("input", { class: "json-tree__edit-input json-tree__edit-input--key", type: "text", value: key });
      keyEl.replaceWith(input);
      input.focus();
      input.select();
      const commit = () => {
        const newKey = input.value.trim();
        if (!newKey || newKey === key) { rerender(); return; }
        if (Object.prototype.hasOwnProperty.call(container, newKey)) {
          toast(`"${newKey}" already exists at this level.`);
          rerender();
          return;
        }
        // Rebuild the object to preserve key order with the rename in place.
        const rebuilt = {};
        for (const k of Object.keys(container)) {
          rebuilt[k === key ? newKey : k] = container[k];
        }
        for (const k of Object.keys(container)) delete container[k];
        Object.assign(container, rebuilt);
        onChange();
        rerender();
      };
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); commit(); }
        else if (e.key === "Escape") { e.preventDefault(); rerender(); }
      });
      input.addEventListener("blur", commit);
    }

    function buildAddControls(value, pathKey, depth) {
      const wrap = el("div", { class: "json-tree__row json-tree__add-row", style: `padding-left:${(depth + 1) * 16}px` });
      if (Array.isArray(value)) {
        wrap.appendChild(el("button", {
          type: "button", class: "json-tree__add",
          onclick: () => { value.push(""); expandedPaths.add(pathKey); onChange(); rerender(); },
        }, "+ add item"));
      } else {
        wrap.appendChild(el("button", {
          type: "button", class: "json-tree__add",
          onclick: () => {
            let base = "new_key", n = 1, name = base;
            while (Object.prototype.hasOwnProperty.call(value, name)) name = `${base}_${n++}`;
            value[name] = "";
            expandedPaths.add(pathKey);
            onChange();
            rerender();
          },
        }, "+ add key"));
      }
      return wrap;
    }

    function buildChildren(container, value, pathKey, depth) {
      const isArr = Array.isArray(value);
      const keys = isArr ? value.map((_, i) => i) : Object.keys(value);
      for (const key of keys) {
        const childPathKey = `${pathKey}.${key}`;
        const child = value[key];
        const childIsContainer = jsonTreeIsContainer(child);
        const open = childIsContainer ? isOpen(childPathKey, depth + 1) : false;

        const row = el("div", { class: "json-tree__row", style: `padding-left:${depth * 16}px` });

        const toggle = el("span", {
          class: "json-tree__toggle" + (childIsContainer ? "" : " json-tree__toggle--leaf"),
        }, childIsContainer ? (open ? "\u25be" : "\u25b8") : "\u00b7");
        if (childIsContainer) {
          toggle.addEventListener("click", () => {
            if (isOpen(childPathKey, depth + 1)) {
              expandedPaths.delete(childPathKey);
              collapsedPaths.add(childPathKey);
            } else {
              collapsedPaths.delete(childPathKey);
              expandedPaths.add(childPathKey);
            }
            rerender();
          });
        }
        row.appendChild(toggle);

        const keyEl = el("span", { class: "json-tree__key" }, isArr ? `[${key}]` : String(key));
        if (editable && !isArr) {
          keyEl.classList.add("json-tree__key--editable");
          keyEl.title = "Click to rename";
          keyEl.addEventListener("click", () => startRenameKey(value, key, keyEl));
        }
        row.appendChild(keyEl);
        row.appendChild(el("span", { class: "json-tree__colon" }, ":"));

        if (childIsContainer) {
          row.appendChild(el("span", { class: "json-tree__meta" }, jsonTreeCountLabel(child)));
        } else {
          const valEl = el("span", { class: `json-tree__val json-tree__val--${jsonTreeTypeOf(child)}` }, jsonTreeScalarLabel(child));
          if (editable) {
            valEl.classList.add("json-tree__val--editable");
            valEl.title = "Click to edit";
            valEl.addEventListener("click", () => startEditValue(value, key, valEl));
          }
          row.appendChild(valEl);
        }

        if (editable) {
          row.appendChild(el("button", {
            type: "button", class: "json-tree__del", title: isArr ? "Remove item" : "Remove key",
            onclick: () => {
              if (isArr) value.splice(Number(key), 1);
              else delete value[key];
              onChange();
              rerender();
            },
          }, "\u00d7"));
        }

        container.appendChild(row);

        if (childIsContainer && open) {
          buildChildren(container, child, childPathKey, depth + 1);
          if (editable) container.appendChild(buildAddControls(child, childPathKey, depth + 1));
        }
      }
    }

    rerender();
  }

  // Small grey "conv id" tag shown next to the Ask panel's action row and
  // next to the Logs "Settings" pane header — purely a copy/paste aid for
  // reporting bugs against a specific conversation, no behavior depends on
  // it. Safe to call with an empty/undefined id (clears the tag).
  function updateConvoIdTag(id) {
    const tag = qs("#ask-convo-id-tag");
    if (tag) tag.textContent = id || "";
  }
  function updateLogsConvoIdTag(id) {
    const tag = qs("#logs-convo-id-tag");
    if (tag) tag.textContent = id || "";
  }

  let toastTimer = null;
  function toast(message, kind = "error") {
    const t = qs("#toast");
    t.textContent = message;
    t.className = "toast" + (kind === "info" ? " is-info" : "");
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, 4200);
  }

  // Bottom-left popup for a directly-run saved command that's paused on
  // its own confirm_required/ai_review flags (see server.js's
  // "confirm-request" message, fired from cli.py's confirm_direct_command
  // — completely separate from the in-thread ask-confirm bubble, since a
  // direct run has no chat thread to render into). Shows the actual
  // command content (risk_note.command_run) alongside any AI risk note
  // and the command's own safety flags, so the user has real information
  // before approving — not just the bare tool name and typed args.
  function showRunConfirmPopup(tool, args, riskNote) {
    const popup = qs("#run-confirm-popup");
    popup.innerHTML = "";
    popup.hidden = false;

    popup.appendChild(el("div", { class: "run-confirm-popup__title" }, [
      "\u26a0 Run ", el("span", { class: "run-confirm-popup__tool" }, tool || "(unknown command)"), "?",
    ]));

    popup.appendChild(confirmValuePre(args || {}, "run-confirm-popup__args"));

    if (riskNote && riskNote.command_run !== undefined && riskNote.command_run !== null) {
      popup.appendChild(el("div", { class: "run-confirm-popup__risk-label" }, "Command:"));
      popup.appendChild(confirmValuePre(riskNote.command_run, "run-confirm-popup__args"));
    }

    if (riskNote && riskNote.note) {
      const label = riskNote.provider ? `AI review \u2014 ${riskNote.provider}` : "AI review";
      popup.appendChild(el("div", { class: "run-confirm-popup__risk" }, [
        el("div", { class: "run-confirm-popup__risk-label" }, label),
        el("div", { class: "run-confirm-popup__risk-note" }, riskNote.note),
      ]));
    }

    if (riskNote && riskNote.command_flags) {
      const cf = riskNote.command_flags;
      popup.appendChild(el("div", { class: "run-confirm-popup__flags" },
        `Flags: confirm_required=${!!cf.confirm_required}, ai_review=${!!cf.ai_review}`));
    }

    const yesBtn = el("button", { class: "btn btn--primary", type: "button" }, "Yes, run it");
    const noBtn = el("button", { class: "btn btn--ghost", type: "button" }, "No, cancel");
    popup.appendChild(el("div", { class: "run-confirm-popup__actions" }, [noBtn, yesBtn]));

    function resolve(approved) {
      popup.hidden = true;
      popup.innerHTML = "";
      wsSend({ type: "confirm-response", approved });
    }
    yesBtn.addEventListener("click", () => resolve(true));
    noBtn.addEventListener("click", () => resolve(false));
  }

  function jarvisTabVisible() {
    return document.visibilityState === "visible";
  }

  function ensureNotifPermission() {
    if (!("Notification" in window)) return;
    if (Notification.permission === "default") {
      Notification.requestPermission().catch(() => {});
    }
  }

  function summarizeText(text) {
    const plain = String(text || "")
      .replace(/[#*_`>+-]/g, " ")
      .replace(/\s+/g, " ")
      .trim();
    if (!plain) return "Finished.";
    const sentence = (plain.match(/^[^.!?]+[.!?]?/) || [plain])[0];
    return sentence.slice(0, 120);
  }

  function notifyIfAway(title, body) {
    if (jarvisTabVisible()) return;
    if (!("Notification" in window) || Notification.permission !== "granted") return;
    try {
      const n = new Notification(title.slice(0, 88), {
        body: (body || "").slice(0, 140),
        tag: "jarvis-task",
        silent: true,
      });
      n.onclick = () => {
        window.focus();
        n.close();
      };
    } catch {
      /* private mode / unsupported */
    }
  }

  // One place every notification is displayed, whichever path it arrived by
  // (live stderr stream during an ask, or a WS push from the tick loop).
  // Always shows an in-page toast; additionally raises a real OS
  // notification when the tab isn't visible, since the whole point of a
  // reminder is that it reaches you when you're not looking at Jarvis.
  function showNotification(note) {
    if (!note || (!note.title && !note.message)) return;
    const title = String(note.title || "Jarvis").slice(0, 120);
    const body = String(note.message || "").slice(0, 400);
    const isFailure = Boolean(note.failed);
    toast(`${title}${body ? " \u2014 " + body.slice(0, 160) : ""}`, isFailure ? "error" : "info");
    if (!jarvisTabVisible() && "Notification" in window && Notification.permission === "granted") {
      try {
        // Tagged per notification id, not a shared tag: two reminders
        // firing in the same tick must not collapse into one toast the way
        // notifyIfAway's fixed "jarvis-task" tag deliberately does.
        const n = new Notification(title, {
          body,
          tag: `jarvis-note-${note.id || Date.now()}`,
        });
        n.onclick = () => { window.focus(); n.close(); };
      } catch {
        /* private mode / unsupported */
      }
    }
  }

  function notifyTaskDone(summary, failed) {
    const task = (state.lastTaskLabel || "that").replace(/\s+/g, " ").trim().slice(0, 42) || "that";
    const title = `Your task of doing ${task} is done sir`;
    notifyIfAway(title, failed ? (summary || "It didn't finish cleanly.") : (summary || "All set."));
  }

  // ===========================================================================
  // State
  // ===========================================================================

  const state = {
    commands: {},          // name -> spec
    selected: null,        // currently selected command name
    sequence: [],          // [{name, flags, label}]
    running: false,        // true while ANY child process is in flight (a run OR an ask \u2014 the
                            // server allows only one at a time per connection, see server.js)
    ws: null,
    wsBackoff: 1000,
    editingOriginalName: null, // set when the builder modal is editing an existing command

    // Ask Jarvis \u2014 state for the turn currently streaming in, if any.
    askPendingBubble: null,  // the DOM node for Jarvis's in-progress reply bubble
    askReplyLines: [],       // accumulated (post prefix-strip) lines of that reply, raw (untriaged)
    askTraceBubble: null,    // the DOM node for the current turn's console-dump bubble, if any
    askQuotes: [],           // highlighted excerpts attached to the next ask
    lastTaskLabel: "",       // user request / command name for away notifications
    cmdSearch: "",           // current text in the command-list search field

    // Debug dashboard \u2014 tool catalog is read live from /api/tools, never hardcoded.
    debugTools: [],          // [{name, description, parameters}] as returned by the CLI
    debugLoaded: false,
    debugSelected: null,     // name of the currently selected tool
    debugSearch: "",
    debugResponseMode: "organized", // "organized" | "raw"
    debugLastResult: null,   // last {ok, result, raw, stderr, error} from /api/tools/run
    debugLastResultError: false,
    debugPendingConfirm: null, // {name, arguments, risk_note} awaiting Yes/No before /api/tools/run
    debugLastUsage: null,    // Phase 0 (new_plan.md): last ask-usage payload (see handleWsMessage)
    debugMode: null,          // local-only capacity override for this panel, e.g. "compact" \u2014
                               // never sent to /api/mode, never affects the real global mode
    debugLastUsage: null,    // Phase 0 (new_plan.md): last ask-usage payload (see handleWsMessage)

    // Conversations — every saved chat lives in ~/.jarvis/conversations
    // (see conversations.py); this is just the in-memory mirror for the
    // sidebar list, refreshed from /api/conversations.
    conversations: [],           // [{id,title,soft_context,created_at,updated_at,exchange_count}]
    activeConversationId: null,  // which one the open thread + next ask belong to
    askConversationId: null,     // which conversation the in-flight ask/redo actually belongs to
    convoSearch: "",
    askTraceByConv: {},          // convId -> [{text, cls}] recorded "commands Jarvis runs" lines,
                                  // so switching away and back doesn't lose them (see askPromptLine)

    // Provider-override picker — list of {name, model, type} from
    // /api/ai/providers (already-eligible-and-priority-ordered, see that
    // route in server.js), plus which ones (if any) are currently picked
    // and in what order. An empty array means "Auto" — no override, exact
    // previous behavior. A non-empty array is an ordered try-list: the
    // order names were *clicked* in the picker is the order they're tried
    // in for that ask (first pick first), same shape as cli.py's
    // '--provider a,b,c'. See providerOverrideValue() for how this
    // becomes the wire value sent on "ask"/redo.
    aiProviders: [],
    providerOverride: [],
    pendingConfirmByConv: {},    // convId -> {tool, arguments, risk_note, extraItem} for a confirm
                                  // request that arrived while that conversation wasn't being viewed
    threadExtrasByConv: {},      // convId -> [{bucket, type, data}] non-text thread items (screenshots,
                                  // downloads, organize-json results, console dumps, resolved confirms)
                                  // so they survive switching away and back — see pushThreadExtra() and
                                  // renderThreadExtra(). "bucket" is the exchange index they belong
                                  // after (see exchangeCountByConv).
    exchangeCountByConv: {},     // convId -> number of completed (saved) exchanges, used to bucket
                                  // threadExtrasByConv entries against loadConversationIntoThread's replay

    // Logs overlay — conversation-scoped raw model↔backend traffic, read
    // live from /api/logs (see logs.py). Independent of the Ask sidebar's
    // own conversation list/search state above.
    logsLoaded: false,
    logsConvos: [],           // [{id, title, updated_at, exists}] from /api/logs
    logsSearch: "",
    logsSelected: null,       // conv id currently shown in the middle pane
    logsEntries: [],          // entries for logsSelected, oldest first
    logsViewMode: "organized", // "organized" | "raw"

    // Voice (jarvis-enhancement-plan.md §3.5/§3a) — the browser owns the
    // mic/speaker; these just track the one recording (if any) and the
    // one reply audio (if any) currently in flight, so a second click on
    // the mic or a Speak button always has a single, unambiguous thing to
    // stop rather than stacking overlapping streams.
    voiceRecorder: null,      // active MediaRecorder, or null when not recording
    voiceStream: null,        // its MediaStream, kept around so tracks can be stopped
    voiceChunks: [],          // recorded Blob chunks for the in-progress recording
    voiceTurnPending: false,  // true after a mic-originated ask is sent, until its
                              // reply bubble finalizes — see finalizeAskBubble's hook
    voiceAudioEl: null,       // the single <audio> used for Speak playback
    voiceSpeakingBtn: null,   // the "Speak" button currently showing "Stop" (if any)
  };

  // ===========================================================================
  // API
  // ===========================================================================

  async function api(method, url, body) {
    const res = await fetch(url, {
      method,
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    let data = null;
    try { data = await res.json(); } catch { /* no body */ }
    if (!res.ok) {
      const message = (data && data.error) || `${method} ${url} failed (${res.status})`;
      const err = new Error(message);
      err.data = data; // preserves extra fields (e.g. organize-json's line/column/snippet)
      throw err;
    }
    return data;
  }

  const Api = {
    // Generic verbs, for endpoints whose call sites read better as a plain
    // path than as another named wrapper (the scheduling/MCP panel builds
    // per-job URLs like /api/scheduled/<id>/snooze). The named helpers below
    // are still the right shape for anything called from several places.
    get: (path) => api("GET", path),
    post: (path, body) => api("POST", path, body),
    status: () => api("GET", "/api/status"),
    reconnect: () => api("POST", "/api/reconnect"),
    // Favorited commands: server-persisted (see server.js), independent of
    // whether the jarvis CLI itself is reachable — never gated behind
    // requireJarvis on the server side, so this always works.
    getFavorites: () => api("GET", "/api/favorites"),
    setFavorites: (names) => api("POST", "/api/favorites", { names }),
    listCommands: () => api("GET", "/api/commands"),
    createCommand: (name, spec) => api("POST", "/api/commands", { name, spec }),
    updateCommand: (oldName, name, spec) => api("PUT", `/api/commands/${encodeURIComponent(oldName)}`, { name, spec }),
    deleteCommand: (name) => api("DELETE", `/api/commands/${encodeURIComponent(name)}`),
    clearAiHistory: (conversationId) => api("POST", "/api/ai/clear", conversationId ? { conversationId } : {}),
    configList: () => api("GET", "/api/config/list"),
    getConfigFile: (name) => api("GET", `/api/config/file/${encodeURIComponent(name)}/raw`),
    putConfigFile: (name, text) => api("PUT", `/api/config/file/${encodeURIComponent(name)}/raw`, { text }),
    organizeJson: (targetPath) => api("POST", "/api/json/organize", { path: targetPath }),
    listTools: () => api("GET", "/api/tools"),
    getPersonas: () => api("GET", "/api/personas"),
    getChannels: () => api("GET", "/api/channels"),
    setChannelEntry: (platform, set, entry, remove) =>
      api("POST", `/api/channels/${encodeURIComponent(platform)}/${encodeURIComponent(set)}`, { entry, remove: !!remove }),
    runTool: (name, arguments_, mode) => api("POST", "/api/tools/run", { name, arguments: arguments_, mode }),
    previewTool: (name, arguments_, mode) => api("POST", "/api/tools/preview", { name, arguments: arguments_, mode }),
    setToolSafety: (name, key, value) => api("POST", "/api/tools/safety", { name, key, value }),
    listConversations: (q) => api("GET", `/api/conversations${q ? `?q=${encodeURIComponent(q)}` : ""}`),
    createConversation: (title) => api("POST", "/api/conversations", title ? { title } : {}),
    getConversation: (id) => api("GET", `/api/conversations/${encodeURIComponent(id)}`),
    deleteConversation: (id) => api("DELETE", `/api/conversations/${encodeURIComponent(id)}`),
    getMode: () => api("GET", "/api/mode"),
    setMode: (mode) => api("POST", "/api/mode", { mode }),
    listLogs: () => api("GET", "/api/logs"),
    getLog: (id, limit) => api("GET", `/api/logs/${encodeURIComponent(id)}${limit ? `?limit=${limit}` : ""}`),
    clearLog: (id) => api("DELETE", `/api/logs/${encodeURIComponent(id)}`),
    listAiProviders: () => api("GET", "/api/ai/providers"),
    // Voice endpoints don't go through api(): /api/voice/speak's success
    // response body is raw audio, not JSON, and /api/voice/transcribe's
    // request body is raw audio, not JSON — both need their own fetch()
    // rather than api()'s always-JSON assumption in both directions.
    voiceSpeak: async (text) => {
      const res = await fetch("/api/voice/speak", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
      if (!res.ok) {
        let message = `voice/speak failed (${res.status})`;
        try { message = (await res.json()).error || message; } catch { /* not JSON */ }
        throw new Error(message);
      }
      return { blob: await res.blob(), mime: res.headers.get("Content-Type") || "audio/mpeg" };
    },
    voiceTranscribe: async (wavBlob) => {
      const res = await fetch("/api/voice/transcribe", {
        method: "POST",
        headers: { "Content-Type": "audio/wav" },
        body: wavBlob,
      });
      let data = null;
      try { data = await res.json(); } catch { /* no body */ }
      if (!res.ok || !data || data.error) {
        throw new Error((data && data.error) || `voice/transcribe failed (${res.status})`);
      }
      return data; // {ok, text, provider, language}
    },
  };

  // ===========================================================================
  // Boot sequence
  // ===========================================================================

  async function bootLine(text, opts = {}) {
    const holder = qs("#boot-lines");
    const line = el("div", {}, text);
    if (opts.warn) line.style.color = "var(--red)";
    if (opts.ok) line.style.color = "var(--green)";
    holder.appendChild(line);
    await sleep(opts.delay ?? 260);
  }

  function hideBoot() {
    const boot = qs("#boot");
    if (boot.classList.contains("is-hidden")) return;
    boot.classList.add("is-hidden");
    setTimeout(() => { boot.hidden = true; }, 500);
    qs("#app").hidden = false;
  }

  async function runBoot() {
    let skipped = false;
    qs("#boot").addEventListener("click", () => { skipped = true; hideBoot(); }, { once: true });

    const step = async (text, opts) => { if (!skipped) await bootLine(text, opts); };

    await step("ESTABLISHING UPLINK\u2026");
    const statusPromise = Api.status().catch(() => ({ online: false }));
    await step("LOCATING JARVIS BINARY\u2026", { delay: 320 });
    const status = await statusPromise;

    if (status.online) {
      await step(`LINKED \u2014 ${status.invocation}`, { ok: true, delay: 260 });
      await step("INDEXING COMMAND SET\u2026", { delay: 260 });
      await step("ALL SYSTEMS NOMINAL.", { ok: true, delay: 420 });
    } else {
      await step("BINARY NOT FOUND.", { warn: true, delay: 260 });
      await step("ENTERING DEGRADED MODE.", { warn: true, delay: 420 });
    }

    await initApp(status);
    hideBoot(); // no-op if the click handler above already hid it early
  }

  // ===========================================================================
  // Clock
  // ===========================================================================

  function tickClock() {
    const now = new Date();
    qs("#clock").textContent = now.toLocaleTimeString([], { hour12: false });
  }

  // ===========================================================================
  // Status pill
  // ===========================================================================

  function renderStatus(status) {
    applyVoiceEnabled(status.voiceEnabled !== false);
    const pill = qs("#status-pill");
    const text = qs("#status-text");
    const meta = qs("#status-meta");
    pill.classList.remove("is-online", "is-offline");
    if (status.online) {
      pill.classList.add("is-online");
      // `stale` means the last reconnect attempt couldn't re-resolve the
      // jarvis binary, so we're still riding on a previously-working
      // invocation rather than a freshly-confirmed one — most commonly
      // because code was edited without running `pip install .` again.
      // Still fully usable; just flag it so it's obvious a rebuild (then
      // restart) is what will actually pick up those changes.
      text.textContent = status.stale ? "ONLINE (stale build — rebuild to refresh)" : "ONLINE";
      meta.textContent = `${status.invocation} \u00b7 ${status.configPath}`;
      pill.title = status.stale ? "Still using the last working jarvis executable. Run pip install . and restart to pick up recent changes." : "";
      pill.style.cursor = "default";
    } else {
      pill.classList.add("is-offline");
      text.textContent = "OFFLINE \u2014 click to retry";
      meta.textContent = "jarvis CLI not found on PATH";
      pill.title = "Retry locating the jarvis binary";
      pill.style.cursor = "pointer";
    }
  }

  qs("#status-pill").addEventListener("click", async () => {
    const pill = qs("#status-pill");
    if (!pill.classList.contains("is-offline")) return;
    qs("#status-text").textContent = "RETRYING\u2026";
    const status = await Api.reconnect().catch(() => ({ online: false }));
    renderStatus(status);
    if (status.online) await loadCommands();
  });

  // ===========================================================================
  // Capacity switch — see ai_client.PROMPT_MODE_DEFS on the CLI side (today:
  // 400% / 100% / 50%, i.e. full / compact / ultra). Fully generic: the
  // cycle order, labels, and tooltip summaries all come from /api/mode's
  // `options` array (itself ai_client.mode_options(), one source of truth),
  // never hardcoded here — adding a mode to PROMPT_MODE_DEFS is enough for
  // it to show up in this switch with zero front-end changes. Click always
  // steps to the next mode in `options` order and persists it via
  // /api/mode, same "thin client over the CLI" pattern as everything else
  // in this file.
  // ===========================================================================

  let modeOptions = [];   // [{mode,label,summary}, ...] from the server, in cycle order
  const FALLBACK_MODE = { mode: "compact", label: "Capacity", summary: "" };

  function optionFor(mode) {
    return modeOptions.find((o) => o.mode === mode) || null;
  }

  function renderMode(mode) {
    const btn = qs("#btn-mode-switch");
    const current = optionFor(mode) || optionFor(FALLBACK_MODE.mode) || FALLBACK_MODE;
    const idx = modeOptions.indexOf(current);
    const next = modeOptions.length ? modeOptions[(Math.max(idx, 0) + 1) % modeOptions.length] : null;
    btn.dataset.mode = current.mode;
    btn.title = current.summary
      ? `${current.label} — ${current.summary}${next ? ` Click to switch to ${next.label}.` : ""}`
      : current.label;
    qs("#mode-switch-label").textContent = current.label;
  }

  async function loadMode() {
    try {
      const data = await Api.getMode();
      if (Array.isArray(data.options) && data.options.length) modeOptions = data.options;
      renderMode(data.mode);
    } catch {
      // Non-fatal — leave the button on its default label rather than
      // blocking the rest of the app over a cosmetic switch.
    }
  }

  qs("#btn-mode-switch").addEventListener("click", async () => {
    const btn = qs("#btn-mode-switch");
    if (!modeOptions.length) { await loadMode(); if (!modeOptions.length) return; }
    const current = btn.dataset.mode || FALLBACK_MODE.mode;
    const idx = modeOptions.findIndex((o) => o.mode === current);
    const next = modeOptions[(Math.max(idx, 0) + 1) % modeOptions.length];
    btn.disabled = true;
    try {
      const data = await Api.setMode(next.mode);
      if (Array.isArray(data.options) && data.options.length) modeOptions = data.options;
      renderMode(data.mode);
    } catch (e) {
      toast(e.message || "Couldn't switch capacity mode.", "error");
    } finally {
      btn.disabled = false;
    }
  });

  // ===========================================================================
  // Command list
  // ===========================================================================

  function stepCount(spec) {
    return Array.isArray(spec.run) ? spec.run.length : 1;
  }
  function hasConditions(spec) {
    if (!Array.isArray(spec.run)) return false;
    return spec.run.some((s) => typeof s === "object" && (s.if != null || s.unless != null));
  }

  // Default flag values for a quick run/queue straight from the list card
  // (no var-form on screen yet) — only vars with a default get filled in.
  function defaultFlagsFor(spec) {
    const flags = {};
    for (const [vname, vspec] of Object.entries(spec.vars || {})) {
      if (vspec && typeof vspec === "object" && "default" in vspec) flags[vname] = vspec.default;
    }
    return flags;
  }
  function hasRequiredVars(spec) {
    return Object.values(spec.vars || {}).some((v) => !(v && typeof v === "object" && "default" in v));
  }

  const ICON_RUN = '<svg viewBox="0 0 24 24" width="11" height="11" aria-hidden="true"><path d="M6 4l14 8-14 8V4z" fill="currentColor"/></svg>';
  const ICON_ADD = '<svg viewBox="0 0 24 24" width="11" height="11" aria-hidden="true"><path d="M11 5h2v6h6v2h-6v6h-2v-6H5v-2h6V5z" fill="currentColor"/></svg>';
  const ICON_STAR_OUTLINE = '<svg viewBox="0 0 24 24" width="11" height="11" aria-hidden="true"><path d="M12 3.5l2.47 5.51 5.98.58-4.5 4.03 1.32 5.88L12 16.62l-5.27 2.88 1.32-5.88-4.5-4.03 5.98-.58L12 3.5z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg>';
  const ICON_STAR_FILLED = '<svg viewBox="0 0 24 24" width="11" height="11" aria-hidden="true"><path d="M12 3.5l2.47 5.51 5.98.58-4.5 4.03 1.32 5.88L12 16.62l-5.27 2.88 1.32-5.88-4.5-4.03 5.98-.58L12 3.5z" fill="currentColor"/></svg>';

  // Purely front-end command favoriting (still no commands.json/CLI
  // involvement — a favorite is just which command cards sort to the
  // top) — but persisted server-side via /api/favorites (see server.js)
  // instead of this browser's localStorage. localStorage meant favoriting
  // something in one tab/browser never showed up anywhere else pointed at
  // the same running jarvis instance; a tiny JSON file the server reads/
  // writes makes it a property of the instance instead, like conversations
  // and commands.json already are. Same try/catch-and-shrug best-effort
  // style as everything else in this file that touches its own small bit
  // of state: a failed fetch just means this tab's edit doesn't stick,
  // nothing else breaks.
  //
  // favoriteCommands starts empty and is populated asynchronously by
  // loadFavoriteCommands() during initApp() (see Init section at the
  // bottom of this file) — anything that reads it before that resolves
  // just sees "nothing favorited yet" for a moment, then renderCommandList()
  // re-sorts once the real list is in.
  async function loadFavoriteCommands() {
    try {
      const names = await Api.getFavorites();
      return new Set(Array.isArray(names) ? names : []);
    } catch (e) {
      return new Set();
    }
  }

  function saveFavoriteCommands(favoriteSet) {
    Api.setFavorites([...favoriteSet]).catch(() => {
      // Best-effort only — a failed save just means this change didn't
      // persist to disk; the in-memory Set (and this tab's rendering)
      // still reflects it until the next reload.
    });
  }

  let favoriteCommands = new Set();

  function isFavoriteCommand(name) {
    return favoriteCommands.has(name);
  }

  function toggleFavoriteCommand(name) {
    if (favoriteCommands.has(name)) favoriteCommands.delete(name);
    else favoriteCommands.add(name);
    saveFavoriteCommands(favoriteCommands);
    renderCommandList();
  }

  function quickRunCommand(name) {
    const spec = state.commands[name];
    if (!spec) return;
    if (hasRequiredVars(spec)) {
      selectCommand(name);
      toast(`"${name}" needs input \u2014 fill in the required fields, then Execute.`, "info");
      return;
    }
    runSegments([{ name, flags: defaultFlagsFor(spec) }]);
  }

  function quickQueueCommand(name) {
    const spec = state.commands[name];
    if (!spec) return;
    const flags = defaultFlagsFor(spec);
    const bits = Object.entries(flags).map(([k, v]) => `--${k} ${v}`).join(" ");
    state.sequence.push({ name, flags, label: bits ? `${name} ${bits}` : name, mode: "then" });
    renderSequenceBar();
    toast(`Added "${name}" to sequence.`, "info");
  }

  function commandMatchesSearch(name, spec, query) {
    if (!query) return true;
    const haystack = `${name} ${spec.description || ""}`.toLowerCase();
    return haystack.includes(query);
  }

  function renderCommandList() {
    const list = qs("#cmd-list");
    const allNames = Object.keys(state.commands);
    if (allNames.length === 0) {
      list.innerHTML = "";
      list.appendChild(el("div", { class: "empty-hint" }, "No commands yet. Build your first one."));
      return;
    }
    const query = state.cmdSearch.trim().toLowerCase();
    const names = allNames
      .filter((name) => commandMatchesSearch(name, state.commands[name], query))
      // Favorited commands float to the top; within each group (favorited
      // / not), relative order is left exactly as it came in — a plain
      // .sort() would be unstable-in-spirit here, so this compares only
      // favorite-status and lets JS's stable sort preserve everything else.
      .sort((a, b) => Number(isFavoriteCommand(b)) - Number(isFavoriteCommand(a)));
    list.innerHTML = "";
    if (names.length === 0) {
      list.appendChild(el("div", { class: "empty-hint" }, `No commands match \u201c${state.cmdSearch.trim()}\u201d.`));
      return;
    }
    for (const name of names) {
      const spec = state.commands[name];
      const favorited = isFavoriteCommand(name);
      const card = el("div", {
        class: "cmd-card" + (name === state.selected ? " is-active" : "") + (favorited ? " is-favorited" : ""),
        onclick: () => selectCommand(name),
      }, [
        el("div", { class: "cmd-card__quick" }, [
          el("button", {
            type: "button",
            class: "cmd-card__quick-btn cmd-card__quick-btn--favorite" + (favorited ? " is-active" : ""),
            title: favorited ? `Unfavorite "${name}"` : `Favorite "${name}"`,
            "aria-label": favorited ? `Unfavorite ${name}` : `Favorite ${name}`,
            html: favorited ? ICON_STAR_FILLED : ICON_STAR_OUTLINE,
            onclick: (e) => { e.stopPropagation(); toggleFavoriteCommand(name); },
          }),
          el("button", {
            type: "button",
            class: "cmd-card__quick-btn cmd-card__quick-btn--run",
            title: `Run "${name}"`,
            "aria-label": `Run ${name}`,
            html: ICON_RUN,
            onclick: (e) => { e.stopPropagation(); quickRunCommand(name); },
          }),
          el("button", {
            type: "button",
            class: "cmd-card__quick-btn cmd-card__quick-btn--add",
            title: `Add "${name}" to sequence`,
            "aria-label": `Add ${name} to sequence`,
            html: ICON_ADD,
            onclick: (e) => { e.stopPropagation(); quickQueueCommand(name); },
          }),
        ]),
        el("div", { class: "cmd-card__name" }, name),
        el("div", { class: "cmd-card__desc" }, spec.description || ""),
        el("div", { class: "cmd-card__meta" }, [
          el("span", { class: "badge" }, `${stepCount(spec)} step${stepCount(spec) === 1 ? "" : "s"}`),
          el("span", { class: "badge" }, `${Object.keys(spec.vars || {}).length} var${Object.keys(spec.vars || {}).length === 1 ? "" : "s"}`),
          hasConditions(spec) ? el("span", { class: "badge badge--cond" }, "conditional") : null,
        ]),
      ]);
      list.appendChild(card);
    }
  }

  async function loadCommands({ silent = false } = {}) {
    try {
      state.commands = await Api.listCommands();
    } catch (e) {
      state.commands = {};
      if (!silent) toast(e.message);
    }
    applyCommandsToUi();
  }

  function applyCommandsToUi() {
    renderCommandList();
    if (state.selected && !state.commands[state.selected]) {
      state.selected = null;
    }
    renderDetail();
    // If the Settings modal has commands.json open right now, refresh that
    // tab's in-memory copy so an external change (another tab, another
    // client, the CLI itself) doesn't get clobbered by a stale Save.
    // syncCommandsIntoSettingsTab is defined further down (Settings modal
    // section) — function declarations are hoisted, so this call is safe.
    syncCommandsIntoSettingsTab(state.commands);
  }

  function applyCommandsFromServer(commands) {
    if (!commands || typeof commands !== "object" || Array.isArray(commands)) return;
    state.commands = commands;
    applyCommandsToUi();
  }

  // ===========================================================================
  // Detail / run panel
  // ===========================================================================

  function selectCommand(name) {
    state.selected = name;
    renderCommandList();
    renderDetail();
  }

  function currentVarValues() {
    const values = {};
    qsa("#var-form [data-var]").forEach((input) => { values[input.dataset.var] = input.value; });
    return values;
  }

  function validateVarForm() {
    let ok = true;
    qsa("#var-form [data-var]").forEach((input) => {
      const required = input.dataset.required === "1";
      const empty = input.value.trim() === "";
      input.classList.toggle("is-invalid", required && empty);
      if (required && empty) ok = false;
    });
    return ok;
  }

  function renderDetail() {
    const empty = qs("#detail-empty");
    const content = qs("#detail-content");
    if (!state.selected || !state.commands[state.selected]) {
      empty.hidden = false;
      content.hidden = true;
      return;
    }
    empty.hidden = true;
    content.hidden = false;

    const name = state.selected;
    const spec = state.commands[name];
    qs("#detail-name").textContent = name;
    qs("#detail-desc").textContent = spec.description || "";

    const form = qs("#var-form");
    form.innerHTML = "";
    const varNames = Object.keys(spec.vars || {});
    if (varNames.length === 0) {
      form.appendChild(el("div", { class: "empty-hint" }, "This command takes no variables."));
    }
    for (const vname of varNames) {
      const vspec = spec.vars[vname] || {};
      const required = !("default" in vspec);
      const field = el("div", { class: "var-field" }, [
        el("label", { class: "var-field__label" }, [
          `--${vname}`,
          required ? el("span", { class: "var-field__req" }, "REQUIRED") : null,
        ]),
        el("input", {
          type: "text",
          "data-var": vname,
          "data-required": required ? "1" : "0",
          placeholder: required ? "(no default \u2014 required)" : String(vspec.default ?? ""),
          value: vspec.default != null ? String(vspec.default) : "",
        }),
        vspec.description ? el("div", { class: "var-field__hint" }, vspec.description) : null,
      ]);
      form.appendChild(field);
    }

    const stepsList = qs("#steps-preview");
    stepsList.innerHTML = "";
    const steps = Array.isArray(spec.run) ? spec.run : [spec.run];
    steps.forEach((step, i) => {
      const isObj = typeof step === "object" && step !== null;
      const runText = isObj ? step.run : step;
      const tags = [];
      if (isObj && step.if != null) tags.push(el("span", { class: "tag tag--if" }, `IF ${describeCondition(step.if)}`));
      if (isObj && step.unless != null) tags.push(el("span", { class: "tag tag--if" }, `UNLESS ${describeCondition(step.unless)}`));
      if (isObj && step.continueOnError) tags.push(el("span", { class: "tag tag--coe" }, "continue on error"));
      if (isObj && step.parallel && i > 0) tags.push(el("span", { class: "tag tag--parallel" }, "\u2225 parallel with previous"));
      if (isObj && step.showCommand === false) tags.push(el("span", { class: "tag tag--hidden" }, "command hidden"));

      stepsList.appendChild(el("li", { class: "step-preview" }, [
        el("div", { class: "step-preview__num" }, String(i + 1).padStart(2, "0")),
        el("div", { class: "step-preview__body" }, [
          isObj && step.name ? el("div", { class: "step-preview__name" }, step.name) : null,
          el("div", { class: "step-preview__run", html: highlightTokens(runText) }),
          tags.length ? el("div", { class: "step-preview__tags" }, tags) : null,
        ]),
      ]));
    });
  }

  qs("#btn-execute").addEventListener("click", () => {
    if (!state.selected) return;
    if (!validateVarForm()) { toast("Fill in all required variables first."); return; }
    runSegments([{ name: state.selected, flags: currentVarValues() }]);
  });

  // #var-form is a real <form> so a lone text field (very common — most
  // commands take one var) implicitly submits on Enter, which would
  // otherwise reload the page and wipe all state. Route that into EXECUTE.
  qs("#var-form").addEventListener("submit", (e) => {
    e.preventDefault();
    qs("#btn-execute").click();
  });

  qs("#btn-queue").addEventListener("click", () => {
    if (!state.selected) return;
    if (!validateVarForm()) { toast("Fill in all required variables first."); return; }
    const flags = currentVarValues();
    const bits = Object.entries(flags).map(([k, v]) => `--${k} ${v}`).join(" ");
    state.sequence.push({ name: state.selected, flags, label: bits ? `${state.selected} ${bits}` : state.selected, mode: "then" });
    renderSequenceBar();
    toast(`Added "${state.selected}" to sequence.`, "info");
  });

  qs("#btn-edit").addEventListener("click", () => {
    if (state.selected) openBuilder("edit", state.selected);
  });

  qs("#btn-delete").addEventListener("click", async () => {
    if (!state.selected) return;
    const name = state.selected;
    if (!confirm(`Delete "${name}" from commands.json? This can't be undone.`)) return;
    try {
      await Api.deleteCommand(name);
      state.selected = null;
      pruneSequence(name);
      if (favoriteCommands.delete(name)) saveFavoriteCommands(favoriteCommands);
      await loadCommands();
      toast(`Deleted "${name}".`, "info");
    } catch (e) {
      toast(e.message);
    }
  });

  // ===========================================================================
  // Sequence bar
  // ===========================================================================

  function renderSequenceBar() {
    const bar = qs("#sequence-bar");
    const items = qs("#sequence-items");
    if (state.sequence.length === 0) {
      bar.hidden = true;
      return;
    }
    bar.hidden = false;
    items.innerHTML = "";
    state.sequence.forEach((item, i) => {
      // A connector between this item and the previous one \u2014 click to
      // flip it between "then" (wait for the previous item) and "and"
      // (run alongside it), same then/and relationship as the CLI's own
      // chain syntax and a step's own "parallel" toggle, one level up.
      if (i > 0) {
        const isParallel = item.mode === "and";
        items.appendChild(el("button", {
          type: "button",
          class: "seq-connector" + (isParallel ? " is-parallel" : ""),
          title: isParallel
            ? "Runs together with the previous item \u2014 click to run after it instead"
            : "Runs after the previous item finishes \u2014 click to run them together instead",
          onclick: () => { item.mode = isParallel ? "then" : "and"; renderSequenceBar(); },
        }, isParallel ? "\u2225" : "\u2192"));
      }
      items.appendChild(el("div", { class: "seq-chip" }, [
        el("span", { class: "seq-chip__idx" }, `${i + 1}`),
        el("span", {}, item.name),
        el("span", { class: "seq-chip__x", onclick: () => { state.sequence.splice(i, 1); renderSequenceBar(); } }, "\u00d7"),
      ]));
    });
  }

  function pruneSequence(name) {
    const before = state.sequence.length;
    state.sequence = state.sequence.filter((s) => s.name !== name);
    if (state.sequence.length !== before) renderSequenceBar();
  }

  function renameInSequence(oldName, newName) {
    let changed = false;
    for (const item of state.sequence) {
      if (item.name === oldName) {
        item.name = newName;
        changed = true;
      }
    }
    if (changed) renderSequenceBar();
  }

  qs("#btn-seq-clear").addEventListener("click", () => { state.sequence = []; renderSequenceBar(); });
  qs("#btn-seq-run").addEventListener("click", () => {
    if (state.sequence.length === 0) return;
    runSegments(state.sequence.map((s) => ({ name: s.name, flags: s.flags, mode: s.mode })));
    state.sequence = [];
    renderSequenceBar();
  });

  // ===========================================================================
  // Console + WebSocket execution
  // ===========================================================================

  function consoleAppend(text, cls) {
    const c = qs("#console");
    const idle = qs(".console__idle", c);
    if (idle) idle.remove();
    c.appendChild(el("div", { class: `console-line console-line--${cls}` }, text));
    c.scrollTop = c.scrollHeight;
  }

  qs("#btn-clear-console").addEventListener("click", () => {
    qs("#console").innerHTML = '<div class="console__idle">Awaiting instructions.</div>';
  });

  qs("#btn-abort").addEventListener("click", () => {
    wsSend({ type: "cancel" });
  });

  function setRunning(running) {
    state.running = running;
    qs("#btn-execute").disabled = running;
    qs("#btn-seq-run").disabled = running;
    qs("#btn-abort").hidden = !running;
    refreshAskBusyUI();
  }

  // The ask panel's own busy indicators (input, send/stop buttons) reflect
  // whether *the conversation currently on screen* is the one an ask is
  // running for — not just "is anything running at all" — so switching to
  // an idle conversation while a background ask keeps going elsewhere
  // doesn't leave a stray Stop button (or a disabled input) behind in a
  // conversation where, as far as its own view is concerned, nothing is
  // happening. Call this both when state.running changes (setRunning) and
  // whenever the visible conversation changes (selectConversation et al).
  function refreshAskBusyUI() {
    const busyHere = state.running && isViewingAskThread();
    qs("#ask-input").disabled = busyHere;
    qs("#btn-ask-send").disabled = busyHere;
    qs("#btn-ask-stop").hidden = !busyHere;
  }

  function connectWs() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${location.host}/ws`);
    state.ws = ws;

    ws.addEventListener("open", () => { state.wsBackoff = 1000; });
    ws.addEventListener("close", () => {
      state.ws = null;
      if (state.running) consoleAppend("\u26a0 uplink to server lost mid-run", "exit-bad");
      setRunning(false);
      setTimeout(connectWs, state.wsBackoff);
      state.wsBackoff = Math.min(state.wsBackoff * 1.6, 10000);
    });
    ws.addEventListener("error", () => {});
    ws.addEventListener("message", (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      handleWsMessage(msg);
    });
  }

  function wsSend(obj) {
    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
      state.ws.send(JSON.stringify(obj));
    } else {
      toast("Not connected to the server yet \u2014 try again in a moment.");
    }
  }

  function handleWsMessage(msg) {
    switch (msg.type) {
      case "notifications":
        // Pushed from the server's tick loop, drained from the durable
        // inbox (jarvis/notifier.py). These are notifications raised while
        // this tab may not have been running anything at all -- a reminder
        // that fired from cron, a scheduled task that finished overnight --
        // so they arrive here rather than through any ask's stderr stream.
        (msg.notifications || []).forEach(showNotification);
        recordLiveNotifications(msg.notifications || []);
        break;
      case "scheduled-tick":
        // Only refresh the panel if it's actually open; a background tick
        // shouldn't cost a re-render nobody is looking at.
        if ((msg.ran || []).length && typeof refreshScheduledPanel === "function") {
          refreshScheduledPanel();
        }
        break;
      case "commands":
        applyCommandsFromServer(msg.commands);
        break;
      case "start":
        setRunning(true);
        consoleAppend(msg.cmdline, "cmd");
        break;
      case "stdout":
        consoleAppend(msg.line, "out");
        break;
      case "stderr":
        consoleAppend(msg.line, "err");
        break;
      case "exit":
        setRunning(false);
        qs("#run-confirm-popup").hidden = true;
        if (msg.signal) {
          consoleAppend(`\u25a0 stopped (${msg.signal})`, "exit-bad");
          notifyTaskDone(`Stopped (${msg.signal}).`, true);
        } else if (msg.code === 0) {
          consoleAppend("\u25a0 done \u2014 exit code 0", "exit-ok");
          notifyTaskDone("Command finished.");
        } else {
          consoleAppend(`\u25a0 exit code ${msg.code}`, "exit-bad");
          notifyTaskDone(`Exit code ${msg.code}.`, true);
        }
        break;
      case "error":
        setRunning(false);
        qs("#run-confirm-popup").hidden = true;
        consoleAppend(`\u26a0 ${msg.message}`, "exit-bad");
        toast(msg.message);
        notifyTaskDone(msg.message, true);
        break;

      // A tool asked the user something through jarvis/ui_bridge.py. The
      // child process is BLOCKED on stdin until we answer, so every path
      // out of here must send exactly one ui-response — including the
      // error path, or the tool hangs until its own timeout.
      case "ui-request": {
        const req = data.request || {};
        const answer = (value) => wsSend({ type: "ui-response", value });
        let pending;
        try {
          switch (req.kind) {
            case "confirm": pending = JarvisUI.confirm(req); break;
            case "choose":  pending = JarvisUI.choose(req); break;
            case "prompt":  pending = JarvisUI.prompt(req); break;
            case "form":    pending = JarvisUI.form(req); break;
            default:        pending = Promise.resolve(req.default ?? "");
          }
        } catch (e) {
          pending = Promise.resolve(req.default ?? "");
        }
        Promise.resolve(pending)
          .then((value) => {
            if (req.kind === "confirm") return answer(value ? "y" : "n");
            return answer(value);
          })
          .catch(() => answer(req.default ?? ""));
        break;
      }

      case "confirm-request":
        showRunConfirmPopup(msg.tool, msg.arguments, msg.risk_note);
        break;

      case "ask-start":
        setRunning(true);
        state.askReplyLines = [];
        state.askTraceBubble = null;
        state.askPendingBubble = addJarvisBubblePending();
        setAskStatus("thinking\u2026", "busy");
        askPromptBegin();
        break;
      case "ask-stdout":
        appendAskReplyLine(msg.line);
        break;
      case "ask-stderr":
        addAskPromptTrace(msg.line);
        break;
      case "ask-usage":
        // Phase 0 (new_plan.md) baseline: {input_tokens, output_tokens,
        // total_tokens, rounds: [{round, input_tokens, output_tokens, source}],
        // tool_calls: [{name, round, input_tokens, output_tokens, source}]}
        // from ai_providers.get_usage_summary(), one per successful ask.
        // Shown two places: a line in this turn's trace, and the debug
        // menu's "Last ask" token panel (renderDebugUsage below).
        state.debugLastUsage = msg.usage || null;
        if (msg.usage) {
          const u = msg.usage;
          const rounds = u.rounds || [];
          const tools = u.tool_calls || [];
          askPromptLine(
            `$ tokens  in=${u.input_tokens || 0} out=${u.output_tokens || 0} ` +
            `total=${u.total_tokens || 0}  rounds=${rounds.length} tools=${tools.length}`,
            "tool"
          );
        }
        renderDebugUsage();
        break;
      case "ask-confirm-request": {
        const convId = state.askConversationId;
        const extraItem = pushThreadExtra(convId, "confirm", {
          tool: msg.tool, arguments: msg.arguments, risk_note: msg.risk_note, resolved: null,
        });
        if (convId != null) {
          state.pendingConfirmByConv[convId] = { tool: msg.tool, arguments: msg.arguments, risk_note: msg.risk_note, extraItem };
        }
        if (isViewingAskThread()) {
          addAskConfirmBubble(msg.tool, msg.arguments, msg.risk_note, convId, extraItem);
          setAskStatus("waiting for your confirmation\u2026", "busy");
        } else {
          toast("Jarvis needs your OK on something in another chat.", "info");
        }
        break;
      }
      case "ask-exit": {
        setRunning(false);
        const raw = state.askReplyLines.length ? state.askReplyLines.join("\n") : "";
        finalizeAskBubble();
        askPromptEnd(msg.code, msg.signal);
        setAskStatus(msg.code === 0 ? "online" : "last attempt failed", msg.code === 0 ? "" : "error");
        notifyTaskDone(summarizeText(raw), msg.code !== 0);
        // The reply may have just (re)titled this conversation — refresh
        // the sidebar so its card picks up the new title/gist.
        if (msg.code === 0) refreshConvoList();
        // A successful turn is now a saved exchange server-side — bump the
        // bucket counter so the NEXT turn's extras (screenshots, console
        // dumps, etc.) get filed after it rather than merged into this one.
        if (msg.code === 0 && state.askConversationId != null) {
          const convId = state.askConversationId;
          state.exchangeCountByConv[convId] = (state.exchangeCountByConv[convId] || 0) + 1;
        }
        state.askConversationId = null;
        break;
      }
      case "ask-error":
        setRunning(false);
        if (state.askPendingBubble) {
          finalizeAskBubble(msg.message);
        } else {
          toast(msg.message);
        }
        askPromptEnd(1, null, msg.message);
        setAskStatus("error", "error");
        notifyTaskDone(msg.message, true);
        state.askConversationId = null;
        break;
    }
  }

  function runSegments(segments) {
    if (state.running) { toast("A command is already running."); return; }
    ensureNotifPermission();
    state.lastTaskLabel = (segments || []).map((s) => s.name).filter(Boolean).join(" then ") || "that command";
    // Tagging the run with the conversation that's open when it's launched
    // is what lets server.js persist its console output there (see
    // logs-append-run in cli.py) so it survives a reload/switch instead of
    // only living in this tab's live websocket stream.
    wsSend({ type: "run", segments, conversationId: state.activeConversationId });
  }

  // ===========================================================================
  // ===========================================================================
  // Math extraction — pulls $...$/$$...$$/\(...\)/\[...\] segments out of
  // the raw text BEFORE marked.parse() ever sees them, and puts the exact
  // original source back in after marked+DOMPurify have run. This is not
  // optional politeness — two real bugs make it necessary, not just nice:
  //
  //  1. CommonMark's backslash-escape rule silently EATS the backslash in
  //     \( and \[ (backslash followed by ASCII punctuation is "this is an
  //     escaped literal", and the backslash is dropped from the output).
  //     marked.parse("\\(x_B = 1\\)") produces "(x_B = 1)" — the escaped
  //     delimiter marker is gone before KaTeX's auto-render ever runs
  //     over the DOM, so \(...\)/\[...\] would silently never match.
  //  2. A bare "*" or "_" inside $...$ is ordinary GFM emphasis syntax to
  //     marked, which doesn't know it's looking at math. Confirmed:
  //     marked.parse("$2*x + 3*y$") produces "$2<em>x + 3</em>y$" — real,
  //     unremarkable LaTeX (any multiplication or subscript written
  //     without spaces) silently corrupted, not a contrived edge case.
  //
  // Extracting first means marked and DOMPurify never see the LaTeX
  // source at all — they see an opaque placeholder token instead — so
  // neither can mangle it. KaTeX's auto-render then runs on the finished
  // DOM as normal, seeing the exact original delimiters and content.
  // ===========================================================================

  // Private Use Area characters: valid anywhere in HTML text content,
  // never produced by marked/DOMPurify's own output, so a placeholder
  // built from them can't collide with anything either library emits.
  const MATH_PLACEHOLDER_OPEN = "\uE000";
  const MATH_PLACEHOLDER_CLOSE = "\uE001";

  function extractMath(text) {
    const stash = [];
    const stow = (m) => {
      stash.push(m);
      return MATH_PLACEHOLDER_OPEN + (stash.length - 1) + MATH_PLACEHOLDER_CLOSE;
    };
    // Order matters: block forms first, so a later inline pattern can't
    // tear a block delimiter in half (e.g. matching just the first "$" of
    // a "$$" pair). Each is non-greedy and (for the dollar forms) barred
    // from crossing a blank line, so a stray unmatched "$" earlier in a
    // long reply can't swallow everything after it as one giant match.
    let out = text.replace(/\$\$[\s\S]+?\$\$/g, stow);
    out = out.replace(/\\\[[\s\S]+?\\\]/g, stow);
    out = out.replace(/\$[^\n$]+?\$/g, stow);
    out = out.replace(/\\\([^\n]+?\\\)/g, stow);
    return { text: out, stash };
  }
  // Known, accepted trade-off — not unique to this implementation, every
  // tool supporting bare $...$ inline math has the same ambiguity: two
  // unrelated dollar amounts on one line with nothing else between them
  // ("It costs $5 and $10") greedily reads as one inline math span.
  // KaTeX (throwOnError: false, see renderMathIn) shows a small inline
  // error for the resulting nonsense rather than crashing — the same
  // failure mode every other $...$-based renderer accepts, not a reason
  // to drop inline math support. \(...\)/\[...\] are unambiguous and
  // never hit this.

  function escapeHtml(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function restoreMath(html, stash) {
    const re = new RegExp(MATH_PLACEHOLDER_OPEN + "(\\d+)" + MATH_PLACEHOLDER_CLOSE, "g");
    return html.replace(re, (m, i) => {
      const src = stash[Number(i)];
      // src is about to be dropped back into an HTML string as literal
      // text content, so it needs the same escaping marked's own text
      // nodes already got — otherwise LaTeX containing < or > (e.g.
      // "$a < b$") would be parsed as a stray HTML tag once this string
      // is assigned to .innerHTML.
      return src === undefined ? m : escapeHtml(src);
    });
  }

  // Markdown renderer (marked.js — loaded via CDN before this script)
  // ===========================================================================

  function renderMarkdown(text) {
    if (typeof marked === "undefined") {
      const d = document.createElement("div");
      d.textContent = text;
      return d.innerHTML.replace(/\n/g, "<br>");
    }
    const { text: withPlaceholders, stash } = extractMath(text);
    const html = marked.parse(withPlaceholders, {
      breaks: true,
      gfm: true,
    });
    const clean = typeof DOMPurify !== "undefined" ? DOMPurify.sanitize(html) : html;
    return stash.length ? restoreMath(clean, stash) : clean;
  }

  // KaTeX auto-render, applied to an element's rendered HTML AFTER
  // marked+DOMPurify+restoreMath have run — a post-process pass over the
  // DOM, which is what katex's own auto-render extension is built for: it
  // walks an element's text nodes looking for $...$/$$...$$/\(...\)/\[...\],
  // which by this point are back to their exact original source text
  // (see extractMath's docstring for why that step has to happen first).
  // Without this, a reply with math in it renders literal "\vec{OI}" and
  // dollar signs instead of typeset math — this is what actually turns
  // the LaTeX Jarvis is asked to write into math a person can read,
  // rather than requiring the model to avoid math notation entirely.
  //
  // Silently a no-op if the CDN script failed to load (offline, blocked,
  // whatever) — same graceful-degradation spirit as the `typeof marked
  // === "undefined"` fallback above, math just stays as plain text
  // instead of the whole bubble failing to render.
  function renderMathIn(el) {
    if (typeof renderMathInElement === "undefined" || !el) return;
    try {
      renderMathInElement(el, {
        delimiters: [
          { left: "$$", right: "$$", display: true },
          { left: "\\[", right: "\\]", display: true },
          { left: "$", right: "$", display: false },
          { left: "\\(", right: "\\)", display: false },
        ],
        throwOnError: false,
        // Code blocks/inline code are the one place a bare "$" is common
        // and never meant as math (shell prompts, prices in examples).
        ignoredTags: ["script", "noscript", "style", "textarea", "pre", "code"],
      });
    } catch (e) {
      // Never let a malformed formula take the whole bubble down.
    }
  }

  // ===========================================================================
  // Path linkification — turn file/folder paths mentioned in a rendered
  // reply into clickable links that open them, using the same
  // reveal_in_explorer / open_file_location / open_file tools present_file
  // (see jarvis-cli/jarvis/present_tools.py) uses under the hood. Web
  // console only, no CLI equivalent — there's no clickable surface in a
  // terminal.
  // ===========================================================================

  // Matches Windows paths (`C:\Users\...`, `\\server\share\...`) and
  // Unix-ish absolute paths (`/home/user/...`), each optionally followed by
  // a trailing file extension segment. Deliberately conservative: requires
  // at least one path separator after the root so we don't snag bare words
  // or drive letters mentioned in passing (e.g. "the C: drive").
  const PATH_RE = /(?:[a-zA-Z]:\\(?:[^\s\\/:*?"<>|]+\\)*[^\s\\/:*?"<>|]+|\\\\[^\s\\/:*?"<>|]+(?:\\[^\s\\/:*?"<>|]+)+|\/(?:[^\s/]+\/)*[^\s/]+)/g;

  // Trailing punctuation that's almost always sentence structure, not part
  // of the path itself (closing parens/brackets are kept if they're
  // balanced against an opener earlier in the match).
  function trimTrailingPunctuation(str) {
    let end = str.length;
    while (end > 0 && /[.,;:!?]/.test(str[end - 1])) end--;
    while (end > 0 && ")]}".includes(str[end - 1])) {
      const closer = str[end - 1];
      const opener = closer === ")" ? "(" : closer === "]" ? "[" : "{";
      const opens = str.slice(0, end - 1).split(opener).length - 1;
      const closes = str.slice(0, end - 1).split(closer).length - 1;
      if (opens > closes) break; // balanced against something earlier — keep it
      end--;
    }
    return str.slice(0, end);
  }

  // Cheap upfront guess so we pick the right tool on the first try in the
  // common case: a trailing separator is unambiguous, otherwise assume a
  // file if the last path segment has a dot-extension, folder otherwise.
  // Not load-bearing — see the retry in openPathLink() below, which
  // corrects a wrong guess using the tool's own error message rather than
  // trying to perfect this heuristic (we have no filesystem access here).
  function guessIsFolder(path) {
    if (/[\\/]$/.test(path)) return true;
    const lastSegment = path.split(/[\\/]/).pop() || "";
    return !/\.[^.]+$/.test(lastSegment);
  }

  async function openPathLink(rawPath, linkEl) {
    let isFolder = linkEl.dataset.isFolder === "1";
    linkEl.classList.add("is-busy");
    try {
      let res = await Api.runTool(isFolder ? "open_file_location" : "open_file", { path: rawPath });
      let errMsg = res.result && res.result.error;
      // Our folder/file guess was wrong — the tool just told us so
      // ("X is a folder, not a file"). Flip and retry once rather than
      // surfacing an error the user has no way to act on.
      if (!isFolder && errMsg && /is a folder, not a file/i.test(errMsg)) {
        isFolder = true;
        linkEl.dataset.isFolder = "1";
        res = await Api.runTool("open_file_location", { path: rawPath });
        errMsg = res.result && res.result.error;
      }
      const failed = res.ok === false || errMsg;
      if (failed) {
        toast(errMsg || res.error || "Couldn't open that.");
      }
    } catch (e) {
      toast(e.message || "Couldn't open that.");
    } finally {
      linkEl.classList.remove("is-busy");
    }
  }

  // A looser check used only for inline `code` spans: the model chose to
  // mark this text as a literal, so we trust it even if it contains spaces
  // (real Windows paths routinely do, e.g. "C:\Program Files\...") — we
  // just need it to *look* like a path at all, rather than picking out a
  // path-shaped substring from a run of prose.
  const LOOKS_LIKE_PATH_RE = /^(?:[a-zA-Z]:[\\/]|\\\\|\/)[^\n]*[^\s]$/;

  function makePathLink(rawPath) {
    const trimmed = trimTrailingPunctuation(rawPath);
    const isFolder = guessIsFolder(trimmed);
    const link = el("span", {
      class: "path-link",
      "data-is-folder": isFolder ? "1" : "0",
      title: `Open ${isFolder ? "folder" : "file"}: ${trimmed}`,
      onclick: (e) => { e.preventDefault(); openPathLink(trimmed, link); },
    }, trimmed);
    return link;
  }

  // Walks the rendered bubble's DOM, skipping real code blocks (<pre>,
  // i.e. fenced ```code```) and existing <a>/.path-link nodes so we don't
  // mangle code or double-link things, and wraps any path-looking text in
  // a clickable span. Call this right after setting a bubble's innerHTML
  // to renderMarkdown(...).
  //
  // Inline `code` spans get special handling: markdown renders a
  // single-backtick path like `C:\Program Files\Jarvis\log.txt` as
  // <code>...</code>, and models mention paths this way constantly. If the
  // whole span's content looks like a path (LOOKS_LIKE_PATH_RE), we treat
  // it as one regardless of internal spaces, since the backticks are the
  // model's own signal that it's a literal, not prose to search inside.
  // Plain (non-code) text still goes through PATH_RE, which is
  // space-free/conservative since it has to pick a path out of a sentence.
  function linkifyPaths(container) {
    if (!container) return;
    const codeSpans = Array.from(container.querySelectorAll("code")).filter((c) => !c.closest("pre"));
    for (const span of codeSpans) {
      const text = span.textContent;
      if (LOOKS_LIKE_PATH_RE.test(text.trim())) {
        span.replaceWith(makePathLink(text.trim()));
      }
    }

    const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        const parent = node.parentElement;
        if (!parent) return NodeFilter.FILTER_REJECT;
        if (parent.closest("code, pre, a, .path-link")) return NodeFilter.FILTER_REJECT;
        return PATH_RE.test(node.nodeValue) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP;
      },
    });
    const textNodes = [];
    let n;
    while ((n = walker.nextNode())) textNodes.push(n);

    for (const node of textNodes) {
      const text = node.nodeValue;
      PATH_RE.lastIndex = 0;
      const frag = document.createDocumentFragment();
      let lastIndex = 0;
      let match;
      let any = false;
      while ((match = PATH_RE.exec(text))) {
        const raw = match[0];
        const trimmed = trimTrailingPunctuation(raw);
        if (trimmed.length < 3) continue; // too short to be a meaningful path (e.g. stray "/x")
        any = true;
        const start = match.index;
        frag.appendChild(document.createTextNode(text.slice(lastIndex, start)));
        frag.appendChild(makePathLink(trimmed));
        lastIndex = start + raw.length; // resume after the *untrimmed* match so leftover punctuation is preserved as text
        PATH_RE.lastIndex = lastIndex;
      }
      if (!any) continue;
      frag.appendChild(document.createTextNode(text.slice(lastIndex)));
      node.parentNode.replaceChild(frag, node);
    }
  }

  // ===========================================================================
  // Ask Jarvis
  // ===========================================================================

  const askOverlay = qs("#ask-overlay");
  const askThread = qs("#ask-thread");

  function setAskStatus(text, kind) {
    if (!isViewingAskThread()) return;
    const el = qs("#ask-status-line");
    el.textContent = text;
    el.className = "ask-panel__subtitle" + (kind ? ` is-${kind}` : "");
  }

  function askThreadScrollToEnd() {
    askThread.scrollTop = askThread.scrollHeight;
  }

  function clearAskEmptyHint() {
    const hint = qs(".ask-empty", askThread);
    if (hint) hint.remove();
  }

  function addAskMsgActions(msg) {
    if (qs(".ask-msg__actions", msg)) return;
    const buttons = [
      el("button", {
        type: "button",
        class: "ask-msg__act",
        title: "Copy as raw text",
        onclick: () => copyAskRaw(msg),
      }, "Copy"),
      el("button", {
        type: "button",
        class: "ask-msg__act",
        title: "Redo this prompt",
        onclick: () => redoAskMessage(msg),
      }, "Redo"),
    ];
    // Speak only makes sense on Jarvis's own replies, not the echoed user
    // bubble or the console-dump trace bubble (see finalizeAskBubble).
    if (msg.classList.contains("ask-msg--jarvis") && micSupported()) {
      const speakBtn = el("button", {
        type: "button",
        class: "ask-msg__act",
        title: "Read this reply aloud",
        onclick: () => speakText(msg.dataset.raw || "", speakBtn),
      }, "Speak");
      buttons.push(speakBtn);
    }
    msg.appendChild(el("div", { class: "ask-msg__actions" }, buttons));
  }

  async function copyAskRaw(msg) {
    const text = msg.dataset.raw || "";
    if (!text) {
      toast("Nothing to copy.");
      return;
    }
    try {
      await navigator.clipboard.writeText(text);
      toast("Copied raw text.", "info");
    } catch {
      toast("Couldn't copy to clipboard.");
    }
  }

  function previousUserMessage(fromMsg) {
    let node = fromMsg.previousElementSibling;
    while (node) {
      if (node.classList.contains("ask-msg--user")) return node;
      node = node.previousElementSibling;
    }
    return null;
  }

  function redoAskMessage(msg) {
    if (state.running) {
      toast("Wait for the current reply to finish.");
      return;
    }
    const userMsg = msg.classList.contains("ask-msg--user") ? msg : previousUserMessage(msg);
    const text = (userMsg && userMsg.dataset.raw || "").trim();
    if (!text) {
      toast("No prompt to redo.");
      return;
    }
    let node = userMsg.nextSibling;
    while (node) {
      const next = node.nextSibling;
      node.remove();
      node = next;
    }
    ensureNotifPermission();
    state.lastTaskLabel = text;
    state.askConversationId = state.activeConversationId;
    wsSend({ type: "ask", text, redo: true, conversationId: state.activeConversationId, provider: providerOverrideValue() });
  }

  function addUserBubble(text, quotes) {
    clearAskEmptyHint();
    const kids = [el("div", { class: "ask-msg__role" }, "You")];
    if (quotes && quotes.length) {
      kids.push(el("div", { class: "ask-msg__quotes" }, quotes.map((q) =>
        el("blockquote", { class: "ask-msg__quote" }, q)
      )));
    }
    kids.push(el("div", { class: "ask-msg__bubble" }, text || "About the quoted part"));
    const msg = el("div", { class: "ask-msg ask-msg--user" }, kids);
    msg.dataset.raw = text || quotes.join("\n\n") || "";
    addAskMsgActions(msg);
    askThread.appendChild(msg);
    askThreadScrollToEnd();
    return msg;
  }

  // A pending-bubble reference can go stale (its DOM node destroyed) if the
  // thread was rebuilt from scratch by loadConversationIntoThread while we
  // were viewing a different conversation (see selectConversation, which
  // re-creates a fresh pending bubble on switching back to a still-running
  // conversation — but this guard is here in case anything else races it,
  // since insertBefore throws outright on a detached reference node).
  // A tool's bubble belongs IN the conversation, in turn order, so it's
  // rendered here rather than by ui-kit.js — which owns floating surfaces
  // but has no idea where the current turn ends.
  document.addEventListener("jarvis:ui-bubble", (e) => {
    const event = e.detail || {};
    const lvl = ["info", "success", "warn", "error"].includes(event.level) ? event.level : "info";
    const mark = { info: "\u2139", success: "\u2713", warn: "\u26a0", error: "\u2717" }[lvl];
    clearAskEmptyHint();
    const card = el("div", { class: `jui-card jui-card--${lvl}` }, [
      el("div", { class: "jui-card__head" }, [
        el("span", { class: "jui-card__mark" }, mark),
        el("span", {}, String(event.title || "")),
      ]),
      event.body ? el("div", { class: "jui-card__body" }, String(event.body)) : null,
    ]);
    insertIntoAskThread(card);
    // Recorded as a thread extra so a page reload replays it, exactly like
    // a screenshot or a download card.
    pushThreadExtra({ type: "uiBubble", data: { title: event.title, body: event.body, level: lvl } });
    askThreadScrollToEnd();
  });

  function insertIntoAskThread(msg) {
    if (state.askPendingBubble && askThread.contains(state.askPendingBubble)) {
      askThread.insertBefore(msg, state.askPendingBubble);
    } else {
      askThread.appendChild(msg);
    }
  }

  // True while the conversation thread currently on screen is the same one
  // an in-flight ask/redo actually belongs to. If the user has switched to
  // a different conversation while a reply is still streaming in, we must
  // not paint that reply into the (now unrelated) visible thread — the
  // exchange is still being saved server-side regardless, and will show up
  // correctly next time this conversation is opened.
  function isViewingAskThread() {
    return state.askConversationId == null || state.askConversationId === state.activeConversationId;
  }

  function addJarvisBubblePending() {
    if (!isViewingAskThread()) return null;
    clearAskEmptyHint();
    const msg = el("div", { class: "ask-msg ask-msg--jarvis is-pending" }, [
      el("div", { class: "ask-msg__role" }, currentAssistantName()),
      el("div", { class: "ask-msg__bubble" }, [
        el("span", { class: "ask-typing" }, [el("span", {}), el("span", {}), el("span", {})]),
      ]),
    ]);
    askThread.appendChild(msg);
    askThreadScrollToEnd();
    return msg;
  }

  // ---------------------------------------------------------------------
  // Thread "extras" — the non-text items (screenshots, downloads,
  // organize-json results, console dumps, resolved confirmations) that get
  // inserted straight into the ask thread as they happen. Unlike the plain
  // user/jarvis text exchanges, these never round-tripped through the
  // server, so loadConversationIntoThread() had nothing to replay them
  // from — they simply vanished the moment a conversation was rebuilt
  // (switching away and back, or a page refresh landing back on it).
  // We mirror them here, bucketed by how many exchanges had completed in
  // that conversation when they occurred, so they can be replayed in the
  // right place relative to the text exchanges.
  // ---------------------------------------------------------------------

  function extraBucketFor(convId) {
    return state.exchangeCountByConv[convId] || 0;
  }

  function pushThreadExtra(convId, type, data) {
    if (convId == null) return null;
    if (!state.threadExtrasByConv[convId]) state.threadExtrasByConv[convId] = [];
    const item = { bucket: extraBucketFor(convId), type, data };
    state.threadExtrasByConv[convId].push(item);
    return item;
  }

  // Console dumps stream in incrementally (more lines keep arriving for the
  // same turn) — find-or-create the one extra for this conversation+bucket
  // instead of pushing a new one on every update.
  function upsertConsoleExtra(convId, dumpLines) {
    if (convId == null) return;
    const bucket = extraBucketFor(convId);
    if (!state.threadExtrasByConv[convId]) state.threadExtrasByConv[convId] = [];
    const arr = state.threadExtrasByConv[convId];
    let item = arr.find((it) => it.type === "console" && it.bucket === bucket);
    if (!item) {
      item = { bucket, type: "console", data: { dumpLines: [] } };
      arr.push(item);
    }
    item.data.dumpLines = dumpLines.slice();
  }

  // Renders one recorded extra into the (currently on-screen) thread —
  // used both for a freshly-arrived event and for replaying history when a
  // conversation is (re)loaded.
  function renderThreadExtra(item) {
    switch (item.type) {
      case "screenshot":
        renderScreenshotBubble(item.data.filename);
        break;
      case "download":
        renderDownloadBubble(item.data.jobId, item.data.filename, item.data.title);
        break;
      case "organizeJson":
        if (item.data.payload) {
          renderOrganizeJsonExtra(item.data.targetPath, item.data.payload);
        } else if (item.data.targetPath) {
          // Server-persisted extras only ever carry the path (the full
          // parsed JSON isn't saved to disk) — insert the placeholder now,
          // in its correct spot in the replay order, then refetch exactly
          // like a live organize_json call does and fill it in once ready.
          // Caching the payload on the item means a second replay in this
          // tab won't refetch.
          clearAskEmptyHint();
          const msg = el("div", { class: "ask-msg ask-msg--jarvis is-pending" }, [
            el("div", { class: "ask-msg__role" }, currentAssistantName()),
            el("div", { class: "ask-msg__bubble" }, [
              el("span", { class: "ask-typing" }, [el("span", {}), el("span", {}), el("span", {})]),
            ]),
          ]);
          insertIntoAskThread(msg);
          (async () => {
            let payload;
            try {
              payload = await Api.organizeJson(item.data.targetPath);
            } catch (err) {
              payload = err.data || { ok: false, error: err.message };
            }
            item.data.payload = payload;
            if (askThread.contains(msg)) renderOrganizeJsonResult(msg, payload);
          })();
        }
        break;
      case "console":
        renderConsoleDumpBubble(item.data.dumpLines);
        break;
      case "thinking":
        // BUGFIX: ai_client.py has captured and saved a full thinking trace
        // per round since Phase 0 (see extras.append({"type": "thinking"...
        // in ai_client.py) — this case was simply never written, so that
        // data reached the browser on every conversation reload and was
        // silently dropped. Grep confirms zero other references to
        // item.data.text/.rounds/.level anywhere in this file before this.
        // Collapsed by default and rendered after the round finishes (not
        // token-by-token) — true live streaming needs the backend to move
        // off blocking `stream: false` calls per provider, which is a
        // bigger, separately-planned change.
        if (item.data && item.data.text) {
          const details = el("details", { class: "thinking-block" }, [
            el("summary", { class: "thinking-block__summary" },
              `Thinking (${item.data.rounds || 1} round${item.data.rounds === 1 ? "" : "s"}, ${THINK_LEVEL_LABEL[item.data.level] || item.data.level || "default"})`),
            el("pre", { class: "thinking-block__text" }, item.data.text),
          ]);
          insertIntoAskThread(details);
        }
        break;
      case "uiBubble": {
        const lvl = ["info", "success", "warn", "error"].includes(item.data.level)
          ? item.data.level : "info";
        const mark = { info: "\u2139", success: "\u2713", warn: "\u26a0", error: "\u2717" }[lvl];
        insertIntoAskThread(el("div", { class: `jui-card jui-card--${lvl}` }, [
          el("div", { class: "jui-card__head" }, [
            el("span", { class: "jui-card__mark" }, mark),
            el("span", {}, String(item.data.title || "")),
          ]),
          item.data.body ? el("div", { class: "jui-card__body" }, String(item.data.body)) : null,
        ]));
        break;
      }
      case "confirm":
        if (item.data.resolved !== null) renderResolvedConfirmBubble(item.data);
        break;
      case "presentFile":
        // The replay half of the present_file fix. The live path reaches
        // showAskPresentFile() from the JARVIS_MEDIA branch in
        // addAskPromptTrace; this reaches the SAME function with the same
        // field names (ai_client._extras_from_runs builds its data dict to
        // match this signature deliberately), so a reloaded card is the
        // card, not a lookalike rebuilt from different fields.
        showAskPresentFile({
          jobId: item.data.jobId || null,
          filename: item.data.filename || null,
          name: item.data.name || "",
          type: item.data.type || "file",
          sizeBytes: typeof item.data.sizeBytes === "number" ? item.data.sizeBytes : null,
          path: item.data.path || "",
        });
        break;
      case "devAgent":
        // A replayed devAgent extra already has its complete `steps` array
        // (persisted per ai_client._extras_from_runs — see §6 of the §3.6
        // plan), so this builds the whole finished stepper in one pass —
        // no incremental upsert needed, unlike the live path in
        // addAskPromptTrace/upsertDevAgentCard below. Same
        // renderDevAgentCard() either way so live and replayed never
        // visually drift apart.
        item.dom = renderDevAgentCard(item);
        insertIntoAskThread(item.dom);
        break;
    }
  }

  function addAskPromptTrace(raw) {
    const line = stripAnsi(raw).trim();
    if (!line) return;
    if (line.startsWith("JARVIS_MEDIA\t")) {
      const parts = line.split("\t");
      if (parts[1] === "screenshot" && parts[2]) {
        showAskScreenshot(parts[2].trim());
        askPromptLine(`$ screenshot  ${parts[2].trim()}`, "tool");
        return;
      }
      if (parts[1] === "download" && parts[2] && parts[3]) {
        const jobId = parts[2].trim();
        const filename = parts[3].trim();
        const title = (parts[4] || filename).trim();
        showAskDownload(jobId, filename, title);
        askPromptLine(`$ download  ${title}`, "tool");
        return;
      }
      if (parts[1] === "organize_json" && parts[2]) {
        showAskOrganizeJson(parts[2].trim());
        askPromptLine(`$ organize_json  ${parts[2].trim()}`, "tool");
        return;
      }
      if (parts[1] === "present_file" && parts[6] !== undefined) {
        const jobId = (parts[2] || "-").trim();
        const filename = (parts[3] || "-").trim();
        const name = (parts[4] || "").trim();
        const ftype = (parts[5] || "file").trim();
        const sizeBytes = parts[6] && parts[6] !== "-" ? Number(parts[6]) : null;
        const fullPath = (parts[7] || "").trim();
        showAskPresentFile({
          jobId: jobId === "-" ? null : jobId,
          filename: filename === "-" ? null : filename,
          name, type: ftype, sizeBytes, path: fullPath,
        });
        askPromptLine(`$ present  ${name || fullPath}`, "tool");
        return;
      }
      if (parts[1] === "notification" && parts[2]) {
        // A scheduled job / reminder firing inside THIS ask's process (see
        // jarvis/notifier.py's "stream" channel). Same three-field envelope
        // and JSON-payload shape dev_agent uses, so it needs no change to
        // the split("\t") dispatch around it.
        let note;
        try { note = JSON.parse(parts[2]); } catch { return; }
        showNotification(note);
        askPromptLine(`$ notify  ${note.title || ""}`, "tool");
        return;
      }
      if (parts[1] === "ui" && parts[2]) {
        // Generic UI event from any tool (jarvis/ui_bridge.py). Same
        // three-field envelope dev_agent uses, dispatched to the shared
        // popup layer instead of a per-feature renderer — this is the
        // branch that means a NEW kind of popup needs no change here.
        let event;
        try { event = JSON.parse(parts[2]); } catch { return; }
        JarvisUI.handleEvent(event);
        const label = event.title || event.message || event.label || event.kind;
        askPromptLine(`$ ui  ${event.kind}  ${String(label).slice(0, 60)}`, "tool");
        return;
      }
      if (parts[1] === "dev_agent" && parts[2]) {
        // dev_agent's progress events (see jarvis-cli/jarvis/dev_agent_events.py)
        // don't fit present_file's fixed positional fields — the whole event
        // is one JSON blob in parts[2] instead. Still exactly 3 tab-separated
        // parts overall, so this still fits the plain line.split("\t") dispatch
        // every other JARVIS_MEDIA branch above uses.
        let event;
        try { event = JSON.parse(parts[2]); } catch { return; }
        const item = upsertDevAgentCard(event);
        if (item && isViewingAskThread()) {
          clearAskEmptyHint();
          if (!item.dom) {
            item.dom = renderDevAgentCard(item);
            insertIntoAskThread(item.dom);
          } else {
            updateDevAgentCard(item);
          }
          askThreadScrollToEnd();
        }
        const detail = event.path || event.command || "";
        askPromptLine(`$ dev_agent  ${event.phase}:${event.status}` + (detail ? `  ${detail}` : ""), "tool");
        return;
      }
    }
    let cls = "sys";
    if (line.includes("\u2717")) cls = "fail";
    else if (line.includes("$")) cls = "tool";
    askPromptLine(line, cls);
  }

  function formatFileSize(bytes) {
    if (typeof bytes !== "number" || !isFinite(bytes) || bytes < 0) return "\u2014";
    if (bytes < 1024) return `${bytes} B`;
    const units = ["KB", "MB", "GB", "TB"];
    let value = bytes / 1024;
    let i = 0;
    while (value >= 1024 && i < units.length - 1) { value /= 1024; i += 1; }
    return `${value.toFixed(value >= 10 ? 0 : 1)} ${units[i]}`;
  }

  // Shared by present_file's chat card — runs reveal_in_explorer /
  // open_file_location / open_file straight through /api/tools/run, no
  // model involved, disabling the row's other buttons while it's in flight.
  function runFileAction(name, path, btn, siblings) {
    siblings.forEach((b) => { b.disabled = true; });
    const prevLabel = btn.textContent;
    btn.textContent = "\u2026";
    Api.runTool(name, { path })
      .then((res) => {
        toast(res.ok !== false && !(res.result && res.result.error) ? "Done." : ((res.result && res.result.error) || res.error || "Failed."));
      })
      .catch((e) => toast(e.message || "Failed."))
      .finally(() => {
        siblings.forEach((b) => { b.disabled = false; });
        btn.textContent = prevLabel;
      });
  }

  // Fired when the AI itself calls the organize_json tool (as opposed to
  // the person typing "organize-json <path>" directly into the Ask box —
  // see handleOrganizeJsonCommand). The tool already validated the file and
  // told the model only a tiny ok/type/count summary; this re-runs
  // /api/json/organize itself (still zero tokens — a plain local REST call)
  // purely to get the full parsed data for the Organized/Raw JSON sheet,
  // and renders it with the exact same renderOrganizeJsonResult() used by
  // the typed shortcut, so both paths look identical to the user.
  // Renders the Organized/Raw JSON sheet bubble; reused for a fresh result
  // and for replaying an already-fetched one from threadExtrasByConv.
  function renderOrganizeJsonExtra(targetPath, payload) {
    clearAskEmptyHint();
    const msg = el("div", { class: "ask-msg ask-msg--jarvis" }, [
      el("div", { class: "ask-msg__role" }, currentAssistantName()),
      el("div", { class: "ask-msg__bubble" }),
    ]);
    insertIntoAskThread(msg);
    renderOrganizeJsonResult(msg, payload);
    askThreadScrollToEnd();
    return msg;
  }

  function showAskOrganizeJson(targetPath) {
    const convId = state.askConversationId;
    const item = pushThreadExtra(convId, "organizeJson", { targetPath, payload: null });
    let msg = null;
    if (isViewingAskThread()) {
      clearAskEmptyHint();
      msg = el("div", { class: "ask-msg ask-msg--jarvis is-pending" }, [
        el("div", { class: "ask-msg__role" }, currentAssistantName()),
        el("div", { class: "ask-msg__bubble" }, [
          el("span", { class: "ask-typing" }, [el("span", {}), el("span", {}), el("span", {})]),
        ]),
      ]);
      insertIntoAskThread(msg);
      askThreadScrollToEnd();
    }
    (async () => {
      let payload;
      try {
        payload = await Api.organizeJson(targetPath);
      } catch (err) {
        payload = err.data || { ok: false, error: err.message };
      }
      // Keep the fetched result even if the conversation was switched away
      // from mid-fetch, so it's there next time this conversation is opened.
      if (item) item.data.payload = payload;
      // Thread may have been rebuilt (conversation switch) while we waited.
      if (!msg || !askThread.contains(msg)) return;
      renderOrganizeJsonResult(msg, payload);
    })();
  }

  function renderScreenshotBubble(filename) {
    clearAskEmptyHint();
    const url = `/api/screenshots/${encodeURIComponent(filename)}`;
    const msg = el("div", { class: "ask-msg ask-msg--jarvis ask-msg--media" }, [
      el("div", { class: "ask-msg__role" }, currentAssistantName()),
      el("div", { class: "ask-msg__bubble ask-msg__bubble--media" }, [
        el("a", { href: url, target: "_blank", rel: "noopener", class: "ask-shot-link" }, [
          el("img", {
            class: "ask-shot",
            src: url,
            alt: "Desktop screenshot",
            loading: "lazy",
          }),
        ]),
        el("div", { class: "ask-shot-cap" }, "Screenshot"),
      ]),
    ]);
    insertIntoAskThread(msg);
    askThreadScrollToEnd();
    return msg;
  }

  function showAskScreenshot(filename) {
    if (!/^ss_[A-Za-z0-9_.-]+\.png$/.test(filename)) return;
    pushThreadExtra(state.askConversationId, "screenshot", { filename });
    if (!isViewingAskThread()) return;
    renderScreenshotBubble(filename);
  }

  // A finished ytdl_download (see jarvis-cli/jarvis/ytdl_tools.py) — offers
  // inline playback plus a real download link, keyed by the job's own
  // folder so filenames (drawn from the video's title) never collide.
  const DOWNLOAD_EXT_RE = /\.([A-Za-z0-9]+)$/;
  const AUDIO_EXTS = new Set(["mp3", "m4a", "opus", "wav", "flac", "ogg"]);

  function renderDownloadBubble(jobId, filename, title) {
    const url = `/api/downloads/${encodeURIComponent(jobId)}/${encodeURIComponent(filename)}`;
    const extMatch = DOWNLOAD_EXT_RE.exec(filename);
    const ext = extMatch ? extMatch[1].toLowerCase() : "";
    const isAudio = AUDIO_EXTS.has(ext);

    const player = isAudio
      ? el("audio", { class: "ask-dl-player", src: url, controls: "true", preload: "none" })
      : el("video", { class: "ask-dl-player", src: url, controls: "true", preload: "none" });

    clearAskEmptyHint();
    const msg = el("div", { class: "ask-msg ask-msg--jarvis ask-msg--media" }, [
      el("div", { class: "ask-msg__role" }, currentAssistantName()),
      el("div", { class: "ask-msg__bubble ask-msg__bubble--media" }, [
        player,
        el("div", { class: "ask-dl-footer" }, [
          el("div", { class: "ask-shot-cap" }, title || filename),
          el("a", { href: url, download: filename, class: "ask-dl-link" }, "Download"),
        ]),
      ]),
    ]);
    insertIntoAskThread(msg);
    askThreadScrollToEnd();
    return msg;
  }

  function showAskDownload(jobId, filename, title) {
    if (!/^dl_[A-Za-z0-9_-]+$/.test(jobId) || !filename) return;
    pushThreadExtra(state.askConversationId, "download", { jobId, filename, title });
    if (!isViewingAskThread()) return;
    renderDownloadBubble(jobId, filename, title);
  }

  // A present_file call (see jarvis-cli/jarvis/present_tools.py) — a card
  // with the file/folder's name, type, size and path, plus Open/Reveal
  // buttons (straight REST calls via runFileAction, no model round-trip)
  // and, when the tool prepared one, a Download link identical in shape to
  // showAskDownload's (same job-folder/route, just a different tool made it).
  function showAskPresentFile(info) {
    if (!isViewingAskThread()) return;
    clearAskEmptyHint();
    const isFolder = info.type === "folder";
    const icon = isFolder ? "\ud83d\udcc1" : "\ud83d\udcc4";

    const revealBtn = el("button", { class: "btn btn--ghost btn--sm", type: "button" }, "Reveal in Explorer");
    const openBtn = el("button", { class: "btn btn--ghost btn--sm", type: "button" }, isFolder ? "Open" : "Open file");
    const actionButtons = [revealBtn, openBtn];
    revealBtn.addEventListener("click", () => runFileAction("reveal_in_explorer", info.path, revealBtn, actionButtons));
    openBtn.addEventListener("click", () => runFileAction(isFolder ? "open_file_location" : "open_file", info.path, openBtn, actionButtons));

    const actions = [revealBtn, openBtn];
    if (info.jobId && info.filename) {
      const url = `/api/downloads/${encodeURIComponent(info.jobId)}/${encodeURIComponent(info.filename)}`;
      actions.push(el("a", { href: url, download: info.filename, class: "ask-dl-link" }, "Download"));
    }

    const msg = el("div", { class: "ask-msg ask-msg--jarvis ask-msg--media" }, [
      el("div", { class: "ask-msg__role" }, currentAssistantName()),
      el("div", { class: "ask-msg__bubble ask-msg__bubble--media" }, [
        el("div", { class: "ask-file-card" }, [
          el("div", { class: "ask-file-card__icon" }, icon),
          el("div", { class: "ask-file-card__body" }, [
            el("div", { class: "ask-file-card__name" }, info.name || info.path || "(unnamed)"),
            el("div", { class: "ask-file-card__meta" }, `${isFolder ? "Folder" : "File"} \u00b7 ${formatFileSize(info.sizeBytes)}`),
            el("div", { class: "ask-file-card__path" }, info.path || ""),
            el("div", { class: "ask-file-card__actions" }, actions),
          ]),
        ]),
      ]),
    ]);
    insertIntoAskThread(msg);
    askThreadScrollToEnd();
  }

  // ---------------------------------------------------------------------
  // dev_agent — live-progress card (see jarvis-cli/jarvis/dev_agent_events.py
  // and jarvis-cli/jarvis/actions/dev_agent.py). Progress events arrive as
  // JARVIS_MEDIA\tdev_agent\t<json> lines on stderr (parsed in
  // addAskPromptTrace above); the persisted replay shape is
  // {jobId, ok, projectDir, steps} (ai_client._extras_from_runs' "devAgent"
  // branch, §6 of the §3.6 plan). renderDevAgentCard/fillDevAgentCardBody
  // are shared by both the live path (upsertDevAgentCard) and the replay
  // path (renderThreadExtra's "devAgent" case) so the two never visually
  // drift apart — same principle as §3.3's steps/live-stream identity.
  // ---------------------------------------------------------------------

  // Finds (or creates) this job's extra entry for the current conversation
  // and appends the event to its steps — mirrors upsertConsoleExtra's
  // find-or-create-by-bucket pattern, keyed additionally by job_id since a
  // single turn could in principle run more than one dev_agent job.
  function upsertDevAgentCard(event) {
    const convId = state.askConversationId;
    if (convId == null) return null;
    const bucket = extraBucketFor(convId);
    if (!state.threadExtrasByConv[convId]) state.threadExtrasByConv[convId] = [];
    const arr = state.threadExtrasByConv[convId];
    let item = arr.find((it) => it.type === "devAgent" && it.bucket === bucket && it.data.jobId === event.job_id);
    if (!item) {
      item = { bucket, type: "devAgent", data: { jobId: event.job_id, ok: null, projectDir: null, steps: [] } };
      arr.push(item);
    }
    item.data.steps.push(event);
    if (event.phase === "done") {
      item.data.ok = event.status === "ok";
      item.data.projectDir = event.project_dir || null;
    }
    return item;
  }

  function devAgentStatusGlyph(status) {
    if (status === "ok") return "\u2713";
    if (status === "fail") return "\u2717";
    if (status === "start" || status === "progress" || status === "pending") return "\u22ef";
    return "\u25cb";
  }

  const DEV_AGENT_PHASE_LABELS = { plan: "Plan", write: "Write", install: "Install", run: "Run", fix: "Fix", done: "Done" };
  function devAgentPhaseLabel(phase) {
    return DEV_AGENT_PHASE_LABELS[phase] || phase;
  }

  // One-line row summary built from a step event's own fields — different
  // phases carry different fields (see dev_agent_events.py's event-shape
  // table and the real per-phase field names in actions/dev_agent.py:
  // write uses "path"/"bytes", not "file"/"bytes_written").
  function devAgentStepSummary(e) {
    const firstLine = (s) => (s || "").split("\n")[0];
    switch (e.phase) {
      case "plan": {
        if (e.status === "ok") {
          const nFiles = (e.files || []).length;
          const nDeps = (e.dependencies || []).length;
          return `${nFiles} file${nFiles === 1 ? "" : "s"}, ${nDeps} dep${nDeps === 1 ? "" : "s"}, run: ${e.run_command || ""}`;
        }
        if (e.status === "fail") return e.error || "planning failed";
        return e.description || "";
      }
      case "write": {
        if (e.status === "ok") return `${e.path || ""}  ${formatFileSize(e.bytes)}`;
        if (e.status === "fail") return `${e.path || ""} \u2014 ${e.error || "write failed"}`;
        return e.path || "";
      }
      case "install": {
        const deps = (e.dependencies || []).join(", ");
        if (e.status === "fail") return firstLine(e.stderr_tail) || "install failed";
        if (e.status === "start") return deps || "no dependencies";
        return deps || "nothing to install";
      }
      case "run": {
        if (e.status === "start") return e.command || "";
        if (e.status === "ok") return `exit ${e.exit_code}`;
        return `exit ${e.exit_code}  \u2014 ${firstLine(e.stderr_tail)}`;
      }
      case "fix": {
        const base = `attempt ${e.attempt}/${e.max_attempts}  ${e.classified_error || ""}`;
        return e.note ? `${base} \u2014 ${e.note}` : base;
      }
      case "done": {
        if (e.status === "ok") {
          const n = e.total_attempts || 0;
          return n ? `running \u2014 ${n} fix attempt${n === 1 ? "" : "s"}` : "running";
        }
        return firstLine(e.last_error) || e.reason || "gave up";
      }
      default:
        return "";
    }
  }

  // Groups the raw start/ok/fail event stream into one row per step —
  // matching a "start" to its later "ok"/"fail" by phase (and, for write,
  // by path — writes happen one file at a time, never interleaved, per
  // _write_files' sequential loop, so the most recent open row for a key
  // is always the right one to close). A fix row is flagged `nested` so
  // it renders indented under the run row it followed.
  function buildDevAgentRows(steps) {
    const rows = [];
    const open = {};
    const keyFor = (e) => (e.phase === "write" ? `write:${e.path || ""}` : e.phase === "fix" ? `fix:${e.attempt}` : e.phase);
    for (const e of steps || []) {
      const key = keyFor(e);
      if (e.status === "start") {
        const row = { phase: e.phase, status: "pending", summary: devAgentStepSummary(e), event: e, nested: e.phase === "fix" };
        rows.push(row);
        open[key] = row;
        continue;
      }
      const row = open[key];
      if (row) {
        row.status = e.status;
        row.summary = devAgentStepSummary(e);
        row.event = e;
        delete open[key];
      } else {
        // "done" never has its own "start" event — and any other
        // orphaned ok/fail still gets shown rather than silently dropped.
        rows.push({ phase: e.phase, status: e.status, summary: devAgentStepSummary(e), event: e, nested: e.phase === "fix" });
      }
    }
    return rows;
  }

  function renderDevAgentStepRow(row) {
    const glyph = devAgentStatusGlyph(row.status);
    const statusCls = row.status === "ok" ? "dev-agent-card__step--ok"
      : row.status === "fail" ? "dev-agent-card__step--fail"
      : "dev-agent-card__step--pending";
    const cls = ["dev-agent-card__step", statusCls, row.nested ? "dev-agent-card__step--nested" : null]
      .filter(Boolean).join(" ");
    const rowEl = el("div", { class: cls }, [
      el("span", { class: "dev-agent-card__step-glyph" }, glyph),
      el("span", { class: "dev-agent-card__step-phase" }, devAgentPhaseLabel(row.phase)),
      el("span", { class: "dev-agent-card__step-summary" }, row.summary || ""),
    ]);
    // A completed write row with a preview expands in place to show the
    // truncated file content — reusing the console-dump bubble's <pre>
    // styling family rather than inventing a new one.
    if (row.phase === "write" && row.status === "ok" && row.event && row.event.preview) {
      rowEl.classList.add("is-expandable");
      rowEl.appendChild(el("pre", { class: "dev-agent-card__step-detail" }, row.event.preview));
      rowEl.addEventListener("click", () => rowEl.classList.toggle("is-expanded"));
    }
    return rowEl;
  }

  function fillDevAgentCardBody(body, item) {
    body.innerHTML = "";
    for (const row of buildDevAgentRows(item.data.steps)) body.appendChild(renderDevAgentStepRow(row));
    if (item.data.ok === true) {
      body.appendChild(el("div", { class: "dev-agent-card__footer" },
        item.data.projectDir ? `Project ready \u2014 ${item.data.projectDir}` : "Done."));
    } else if (item.data.ok === false) {
      const lastStep = item.data.steps[item.data.steps.length - 1] || {};
      const lastError = lastStep.last_error ? String(lastStep.last_error).split("\n")[0] : "";
      const note = item.data.projectDir
        ? ` \u2014 the project folder is still here: ${item.data.projectDir}`
        : "";
      body.appendChild(el("div", { class: "dev-agent-card__footer dev-agent-card__footer--fail" },
        (lastError || "gave up") + note));
    }
  }

  // Builds a fresh card and fills it from item.data.steps as it stands
  // right now — used both to insert the very first live row and to
  // replay an already-finished job in one pass.
  function renderDevAgentCard(item) {
    clearAskEmptyHint();
    const body = el("div", { class: "dev-agent-card__body" });
    const msg = el("div", { class: "ask-msg ask-msg--jarvis ask-msg--dev-agent" }, [
      el("div", { class: "ask-msg__role" }, currentAssistantName()),
      el("div", { class: "ask-msg__bubble ask-msg__bubble--dev-agent" }, [
        el("div", { class: "dev-agent-card__header" }, "dev_agent"),
        body,
      ]),
    ]);
    fillDevAgentCardBody(body, item);
    return msg;
  }

  // Re-renders just this card's step list in place, not the whole thread.
  function updateDevAgentCard(item) {
    if (!item.dom) return;
    const body = qs(".dev-agent-card__body", item.dom);
    if (!body) return;
    fillDevAgentCardBody(body, item);
  }

  function stripAnsi(s) {
    return String(s || "").replace(/\u001b\[[0-9;]*[A-Za-z]/g, "").replace(/\u001b\][^\u0007]*\u0007/g, "");
  }

  function askPromptTerm() {
    return qs("#ask-prompt-term");
  }

  function setAskPromptState(text, live) {
    if (!isViewingAskThread()) return;
    const node = qs("#ask-prompt-state");
    node.textContent = text;
    node.classList.toggle("is-live", !!live);
  }

  function askPromptClearIdle() {
    const idle = qs(".ask-prompt__idle", askPromptTerm());
    if (idle) idle.remove();
  }

  function askPromptCursor(show) {
    if (!isViewingAskThread()) return;
    const term = askPromptTerm();
    let cur = qs(".ask-prompt__cursor", term);
    if (!show) {
      if (cur) cur.remove();
      return;
    }
    if (!cur) cur = el("span", { class: "ask-prompt__cursor" });
    term.appendChild(cur);
  }

  // The single place a trace line gets added. Always records it against
  // whichever conversation the running ask belongs to (state.askConversationId)
  // so switching away and back doesn't lose it (see renderAskTraceForConv) —
  // and only touches the actual DOM if that conversation happens to be the
  // one on screen right now, so a background ask can never paint into (or
  // clobber) an unrelated, currently-visible conversation's panel.
  function askPromptLine(text, cls) {
    const convId = state.askConversationId;
    if (convId != null) {
      if (!state.askTraceByConv[convId]) state.askTraceByConv[convId] = [];
      state.askTraceByConv[convId].push({ text, cls });
    }
    if (!isViewingAskThread()) return;
    const term = askPromptTerm();
    askPromptClearIdle();
    const cur = qs(".ask-prompt__cursor", term);
    const line = el("div", { class: `ask-prompt-line ask-prompt-line--${cls}` }, text);
    if (cur) term.insertBefore(line, cur);
    else term.appendChild(line);
    term.scrollTop = term.scrollHeight;
  }

  function askPromptBegin() {
    if (state.askConversationId != null) state.askTraceByConv[state.askConversationId] = [];
    setAskPromptState("live", true);
    askPromptLine("$ jarvis", "cmd");
    askPromptCursor(true);
  }

  function askPromptEnd(code, signal, errorMessage) {
    if (errorMessage) askPromptLine(errorMessage, "fail");
    else if (signal) askPromptLine(`stopped (${signal})`, "fail");
    else if (code === 0) askPromptLine("done", "done");
    else askPromptLine(`exit ${code}`, "fail");
    askPromptCursor(false);
    setAskPromptState("idle", false);
  }

  function askPromptReset() {
    askPromptTerm().innerHTML = '<div class="ask-prompt__idle">Commands Jarvis runs will show up here live.</div>';
    setAskPromptState("idle", false);
  }

  // Called instead of a blind askPromptReset() whenever the visible
  // conversation changes: replays that conversation's own recorded trace
  // lines (if any) rather than always wiping the panel to idle, so
  // switching away and back no longer loses "the console output on the
  // right". If that conversation's ask is still actively running, restores
  // the live cursor/state too.
  function renderAskTraceForConv(convId) {
    const lines = state.askTraceByConv[convId];
    const term = askPromptTerm();
    if (!lines || !lines.length) {
      askPromptReset();
      return;
    }
    term.innerHTML = "";
    for (const { text, cls } of lines) {
      term.appendChild(el("div", { class: `ask-prompt-line ask-prompt-line--${cls}` }, text));
    }
    const stillRunning = state.running && state.askConversationId === convId;
    if (stillRunning) {
      term.appendChild(el("span", { class: "ask-prompt__cursor" }));
      qs("#ask-prompt-state").textContent = "live";
      qs("#ask-prompt-state").classList.add("is-live");
    } else {
      qs("#ask-prompt-state").textContent = "idle";
      qs("#ask-prompt-state").classList.remove("is-live");
    }
    term.scrollTop = term.scrollHeight;
  }

  // Jarvis's own CLI output is plain text like "J.A.R.V.I.S: <reply>" (see
  // cli.py: handle_ai_prompt, which does `print(f"{prefix}{result.text}")`).
  // Normally that "Name: " prefix is on line 1 and we just lift it off (and
  // adopt Name as the role label, so a renamed persona in ai_config.json is
  // reflected automatically) instead of showing it twice.
  //
  // But when the model calls a tool like run_command, it sometimes echoes
  // the tool's raw output *verbatim as the start of its own answer*, and
  // only *then* writes its actual signed reply \u2014 e.g.:
  //   Hello, World! Jarvis at your service.
  //   J.A.R.V.I.S: Executed, sir. The hello command completed successfully.
  // Here line 1 is straight from the command's stdout, not from Jarvis "the
  // persona" \u2014 the "Name: " prefix only shows up on line 2. Stripping only
  // ever looked at line 1, so that raw output used to get glued into the
  // same bubble as the real reply. Now we scan every line for the first one
  // that looks like "<Name>: <text>" and treat everything *before* it as a
  // console dump (its own bubble), keeping only that line onward (prefix
  // stripped) as Jarvis's actual reply.
  const NAME_PREFIX_LINE = /^([^\n:]{1,40}):\s(.*)$/;
  // Some providers (mainly weaker/local ones, or a mid-stream fallback) don't
  // always route tool calls through the real function-calling API and instead
  // have the model echo its own tool-call/tool-result scaffolding as plain
  // text \u2014 e.g. a line like "[called run_command with {...}]" or
  // "[tool result ...]" (see ai_client.py's _TOOL_TRACE_LINE). Treat those as
  // console dump too, wherever they show up in the reply.
  const INLINE_TOOL_TRACE_LINE = /^\[(called\s|tool result\b)/i;

  function splitConsoleDump(lines) {
    let splitAt = -1;
    let name = null;
    let firstReplyLine = null;
    for (let i = 0; i < lines.length; i++) {
      const m = NAME_PREFIX_LINE.exec(String(lines[i]));
      if (m) {
        splitAt = i;
        name = m[1];
        firstReplyLine = m[2];
        break;
      }
    }

    const dump = splitAt === -1 ? [] : lines.slice(0, splitAt);
    const candidateReply = splitAt === -1 ? lines.slice() : [firstReplyLine, ...lines.slice(splitAt + 1)];

    const reply = [];
    for (const line of candidateReply) {
      if (INLINE_TOOL_TRACE_LINE.test(String(line).trim())) dump.push(String(line).trim());
      else reply.push(line);
    }
    return { name, dump, reply };
  }

  function ensureAskTraceBubble() {
    if (state.askTraceBubble && askThread.contains(state.askTraceBubble)) return state.askTraceBubble;
    const msg = el("div", { class: "ask-msg ask-msg--jarvis ask-msg--console" }, [
      el("div", { class: "ask-msg__role" }, "Console"),
      el("div", { class: "ask-msg__bubble ask-msg__bubble--console" }),
    ]);
    insertIntoAskThread(msg);
    state.askTraceBubble = msg;
    return msg;
  }

  function renderAskTrace(dumpLines) {
    if (!dumpLines.length) return;
    upsertConsoleExtra(state.askConversationId, dumpLines);
    if (!isViewingAskThread()) return;
    renderConsoleDumpBubble(dumpLines);
  }

  // Renders (or updates) the "Console" bubble with the given dump lines —
  // used both for a live-streaming reply and for replaying a saved one.
  function renderConsoleDumpBubble(dumpLines) {
    const msg = ensureAskTraceBubble();
    qs(".ask-msg__bubble", msg).textContent = dumpLines.join("\n");
    msg.dataset.raw = dumpLines.join("\n");
    askThreadScrollToEnd();
  }

  // A tool flagged confirm_required (see jarvis-cli/jarvis/tool_safety.py)
  // pauses the running ask and asks the browser to decide. cli.py's
  // on_confirm_request is blocked on a synchronous stdin read right now —
  // server.js relays the answer straight to it, so until Yes/No is
  // clicked here the ask genuinely cannot proceed.
  function addAskConfirmBubble(tool, args, riskNote, convId, extraItem) {
    clearAskEmptyHint();
    const bubbleChildren = [
      el("div", { class: "ask-confirm__tool" }, tool || "(unknown tool)"),
      confirmValuePre(args || {}, "ask-confirm__args"),
    ];
    if (riskNote && riskNote.command_run !== undefined && riskNote.command_run !== null) {
      bubbleChildren.push(el("div", { class: "ask-confirm__risk-label" }, "Command:"));
      bubbleChildren.push(confirmValuePre(riskNote.command_run, "ask-confirm__args"));
    }
    if (riskNote && riskNote.note) {
      const label = riskNote.provider ? `AI review \u2014 ${riskNote.provider}` : "AI review";
      bubbleChildren.push(el("div", { class: "ask-confirm__risk" }, [
        el("div", { class: "ask-confirm__risk-label" }, label),
        el("div", { class: "ask-confirm__risk-note" }, riskNote.note),
      ]));
    }
    if (riskNote && riskNote.command_flags) {
      const cf = riskNote.command_flags;
      bubbleChildren.push(el("div", { class: "ask-confirm__flags" },
        `Flags: confirm_required=${!!cf.confirm_required}, ai_review=${!!cf.ai_review}`));
    }
    const yesBtn = el("button", { class: "btn btn--primary ask-confirm__btn", type: "button" }, "Yes, run it");
    const noBtn = el("button", { class: "btn btn--ghost ask-confirm__btn", type: "button" }, "No, cancel");
    const actions = el("div", { class: "ask-confirm__actions" }, [yesBtn, noBtn]);
    bubbleChildren.push(actions);

    const msg = el("div", { class: "ask-msg ask-msg--jarvis ask-msg--confirm" }, [
      el("div", { class: "ask-msg__role" }, "Confirm"),
      el("div", { class: "ask-msg__bubble ask-msg__bubble--confirm" }, bubbleChildren),
    ]);

    function resolve(approved) {
      yesBtn.disabled = true;
      noBtn.disabled = true;
      actions.appendChild(el("div", { class: `ask-confirm__result ${approved ? "is-yes" : "is-no"}` },
        approved ? "\u2713 Approved \u2014 running\u2026" : "\u2717 Declined"));
      if (convId != null) delete state.pendingConfirmByConv[convId];
      if (extraItem) extraItem.data.resolved = approved;
      wsSend({ type: "ask-confirm-response", approved });
      setAskStatus("thinking\u2026", "busy");
    }
    yesBtn.addEventListener("click", () => resolve(true));
    noBtn.addEventListener("click", () => resolve(false));

    insertIntoAskThread(msg);
    askThreadScrollToEnd();
    return msg;
  }

  // A static, already-resolved confirm bubble — used to replay history
  // (see renderThreadExtra) instead of the live interactive one above.
  function renderResolvedConfirmBubble(data) {
    clearAskEmptyHint();
    const bubbleChildren = [
      el("div", { class: "ask-confirm__tool" }, data.tool || "(unknown tool)"),
      confirmValuePre(data.arguments || {}, "ask-confirm__args"),
    ];
    if (data.risk_note && data.risk_note.command_run !== undefined && data.risk_note.command_run !== null) {
      bubbleChildren.push(el("div", { class: "ask-confirm__risk-label" }, "Command:"));
      bubbleChildren.push(confirmValuePre(data.risk_note.command_run, "ask-confirm__args"));
    }
    if (data.risk_note && data.risk_note.note) {
      const label = data.risk_note.provider ? `AI review \u2014 ${data.risk_note.provider}` : "AI review";
      bubbleChildren.push(el("div", { class: "ask-confirm__risk" }, [
        el("div", { class: "ask-confirm__risk-label" }, label),
        el("div", { class: "ask-confirm__risk-note" }, data.risk_note.note),
      ]));
    }
    if (data.risk_note && data.risk_note.command_flags) {
      const cf = data.risk_note.command_flags;
      bubbleChildren.push(el("div", { class: "ask-confirm__flags" },
        `Flags: confirm_required=${!!cf.confirm_required}, ai_review=${!!cf.ai_review}`));
    }
    bubbleChildren.push(el("div", { class: "ask-confirm__actions" }, [
      el("div", { class: `ask-confirm__result ${data.resolved ? "is-yes" : "is-no"}` },
        data.resolved ? "\u2713 Approved \u2014 running\u2026" : "\u2717 Declined"),
    ]));
    const msg = el("div", { class: "ask-msg ask-msg--jarvis ask-msg--confirm" }, [
      el("div", { class: "ask-msg__role" }, "Confirm"),
      el("div", { class: "ask-msg__bubble ask-msg__bubble--confirm" }, bubbleChildren),
    ]);
    insertIntoAskThread(msg);
    askThreadScrollToEnd();
    return msg;
  }

  // Shared between a normal incoming reply line and re-painting whatever's
  // accumulated so far into a freshly (re)created pending bubble — e.g.
  // after switching back into a still-running conversation, where the old
  // bubble's DOM node was destroyed by loadConversationIntoThread rebuilding
  // the thread from scratch (see selectConversation).
  function rerenderAskPendingBubble() {
    if (!state.askPendingBubble || !state.askReplyLines.length) return;
    const { name, dump, reply } = splitConsoleDump(state.askReplyLines);
    if (name) qs(".ask-msg__role", state.askPendingBubble).textContent = name;
    renderAskTrace(dump);
    const bubble = qs(".ask-msg__bubble", state.askPendingBubble);
    bubble.innerHTML = renderMarkdown(reply.join("\n"));
    linkifyPaths(bubble);
    renderMathIn(bubble);
  }

  function appendAskReplyLine(line) {
    if (!state.askPendingBubble) return;
    state.askReplyLines.push(line);
    if (!isViewingAskThread()) return;
    rerenderAskPendingBubble();
    askThreadScrollToEnd();
  }

  function finalizeAskBubble(overrideMessage) {
    if (!isViewingAskThread()) {
      state.askPendingBubble = null;
      state.askReplyLines = [];
      state.askTraceBubble = null;
      // A mic-originated turn that finishes on a conversation we've since
      // navigated away from has nothing to speak back to — drop the flag
      // rather than leaving it set for whatever bubble finalizes next.
      state.voiceTurnPending = false;
      return;
    }
    const bubble = state.askPendingBubble;
    if (bubble) {
      bubble.classList.remove("is-pending");
      if (state.askReplyLines.length === 0) {
        const raw = overrideMessage || "(no response)";
        bubble.dataset.raw = raw;
        bubble.classList.add("is-error");
        qs(".ask-msg__bubble", bubble).textContent = raw;
      } else {
        const { name, dump, reply } = splitConsoleDump(state.askReplyLines);
        if (name) qs(".ask-msg__role", bubble).textContent = name;
        renderAskTrace(dump);
        // If the model's entire "reply" somehow turned out to be dump lines,
        // fall back to showing everything rather than leaving the bubble blank.
        const replyLines = reply.length ? reply : state.askReplyLines;
        const raw = replyLines.join("\n");
        bubble.dataset.raw = raw;
        const bubbleEl = qs(".ask-msg__bubble", bubble);
        bubbleEl.innerHTML = renderMarkdown(raw);
        linkifyPaths(bubbleEl);
        renderMathIn(bubbleEl);
      }
      addAskMsgActions(bubble);
    }
    if (state.askTraceBubble) addAskMsgActions(state.askTraceBubble);
    state.askPendingBubble = null;
    state.askReplyLines = [];
    state.askTraceBubble = null;
    // Mirrors cli.py's `jarvis listen` (record -> transcribe -> ask ->
    // speak) — a mic-originated turn speaks the reply back automatically
    // once its bubble finalizes, using the same "Speak" button/state
    // machine a manual click would (so it correctly shows "Stop" and can
    // be interrupted like any other playback), rather than a separate
    // one-off audio path.
    if (state.voiceTurnPending) {
      state.voiceTurnPending = false;
      if (bubble) {
        const raw = bubble.dataset.raw || "";
        const speakBtn = qs('.ask-msg__act[title="Read this reply aloud"]', bubble);
        if (speakBtn && raw.trim()) speakText(raw, speakBtn);
      }
    }
    askThreadScrollToEnd();
  }

  function openAsk() {
    askOverlay.hidden = false;
    ensureNotifPermission();
    updateConvoIdTag(state.activeConversationId);
    qs("#ask-input").focus();
  }
  function closeAsk() {
    askOverlay.hidden = true;
  }

  qs("#btn-ask-jarvis").addEventListener("click", openAsk);
  qs("#ask-close").addEventListener("click", closeAsk);
  askOverlay.addEventListener("click", (e) => { if (e.target === askOverlay) closeAsk(); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !askOverlay.hidden) closeAsk();
  });

  qs("#btn-ask-stop").addEventListener("click", () => wsSend({ type: "cancel" }));

  const askQuoteBar = qs("#ask-quote-bar");
  const askSelPop = qs("#ask-sel-pop");
  const askPanel = qs(".ask-panel");
  let pendingSelection = "";

  function hideSelPop() {
    askSelPop.hidden = true;
    pendingSelection = "";
  }

  function renderQuoteBar() {
    askQuoteBar.innerHTML = "";
    if (!state.askQuotes.length) {
      askQuoteBar.hidden = true;
      return;
    }
    askQuoteBar.hidden = false;
    state.askQuotes.forEach((q, i) => {
      askQuoteBar.appendChild(el("div", { class: "ask-quote-chip" }, [
        el("span", { class: "ask-quote-chip__text", title: q }, q),
        el("button", {
          type: "button",
          class: "ask-quote-chip__x",
          title: "Remove quote",
          onclick: () => {
            state.askQuotes.splice(i, 1);
            renderQuoteBar();
          },
        }, "\u00d7"),
      ]));
    });
  }

  function addQuote(text) {
    const clipped = text.replace(/\s+/g, " ").trim();
    if (!clipped) return;
    if (state.askQuotes.includes(clipped)) return;
    if (state.askQuotes.length >= 5) {
      toast("You can quote up to 5 excerpts.");
      return;
    }
    state.askQuotes.push(clipped.slice(0, 1200));
    renderQuoteBar();
    qs("#ask-input").focus();
  }

  function selectionInAskThread() {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || !sel.rangeCount) return "";
    const range = sel.getRangeAt(0);
    if (!askThread.contains(range.commonAncestorContainer)) return "";
    const bubble = range.commonAncestorContainer.nodeType === 1
      ? range.commonAncestorContainer.closest(".ask-msg__bubble, .ask-msg__quote")
      : range.commonAncestorContainer.parentElement &&
        range.commonAncestorContainer.parentElement.closest(".ask-msg__bubble, .ask-msg__quote");
    if (!bubble) return "";
    return sel.toString().trim();
  }

  function placeSelPop() {
    const sel = window.getSelection();
    if (!sel.rangeCount) return hideSelPop();
    const rect = sel.getRangeAt(0).getBoundingClientRect();
    const panelRect = askPanel.getBoundingClientRect();
    let left = rect.left + rect.width / 2 - panelRect.left;
    let top = rect.top - panelRect.top - 36;
    askSelPop.hidden = false;
    const popW = askSelPop.offsetWidth || 120;
    left = Math.max(8, Math.min(left - popW / 2, panelRect.width - popW - 8));
    if (top < 8) top = rect.bottom - panelRect.top + 6;
    askSelPop.style.left = `${left}px`;
    askSelPop.style.top = `${top}px`;
  }

  function onAskSelection() {
    if (askOverlay.hidden) return hideSelPop();
    const text = selectionInAskThread();
    if (!text) return hideSelPop();
    pendingSelection = text;
    placeSelPop();
  }

  document.addEventListener("selectionchange", () => {
    if (askOverlay.hidden) return;
    // wait a tick so mouseup can finish
    requestAnimationFrame(onAskSelection);
  });
  askThread.addEventListener("scroll", hideSelPop, { passive: true });

  qs("#ask-sel-quote").addEventListener("mousedown", (e) => e.preventDefault());
  qs("#ask-sel-copy").addEventListener("mousedown", (e) => e.preventDefault());
  qs("#ask-sel-quote").addEventListener("click", () => {
    if (pendingSelection) addQuote(pendingSelection);
    window.getSelection()?.removeAllRanges();
    hideSelPop();
  });
  qs("#ask-sel-copy").addEventListener("click", async () => {
    if (!pendingSelection) return;
    try {
      await navigator.clipboard.writeText(pendingSelection);
      toast("Copied selection.", "info");
    } catch {
      toast("Couldn't copy.");
    }
    hideSelPop();
  });

  // "organize-json <path>" typed into the Ask box is a local shortcut, not
  // an AI prompt — it's intercepted here and routed straight to
  // /api/json/organize (see Api.organizeJson) so it never goes near
  // wsSend/ask, never costs a token, and isn't blocked by state.running
  // (that flag only guards the one-active-child-per-socket ask/run path;
  // organize-json is an independent one-shot REST call on the server).
  const ORGANIZE_JSON_RE = /^organize-json\s+(.+)$/i;

  // ---- Voice input (mic button) -----------------------------------------
  //
  // MediaRecorder only gives us webm/ogg (browsers don't record WAV
  // directly), but stt.py's backends need a WAV file — vosk opens it with
  // the stdlib `wave` module, which can't touch anything else. So a
  // recording is decoded via Web Audio and re-encoded to 16-bit PCM WAV
  // client-side before it's ever sent to /api/voice/transcribe; the
  // server (per its own comment) just saves the bytes it's given and
  // shells out to `jarvis transcribe`, no format handling of its own.

  function audioBufferToWavBlob(buffer) {
    const numChannels = 1; // stt backends expect mono; downmix on the way out
    const sampleRate = buffer.sampleRate;
    const chData = buffer.getChannelData(0);
    const bytesPerSample = 2; // 16-bit PCM
    const blockAlign = numChannels * bytesPerSample;
    const dataSize = chData.length * bytesPerSample;

    const arrayBuffer = new ArrayBuffer(44 + dataSize);
    const view = new DataView(arrayBuffer);
    const writeStr = (offset, str) => { for (let i = 0; i < str.length; i++) view.setUint8(offset + i, str.charCodeAt(i)); };

    writeStr(0, "RIFF");
    view.setUint32(4, 36 + dataSize, true);
    writeStr(8, "WAVE");
    writeStr(12, "fmt ");
    view.setUint32(16, 16, true);          // fmt chunk size
    view.setUint16(20, 1, true);           // PCM
    view.setUint16(22, numChannels, true);
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, sampleRate * blockAlign, true); // byte rate
    view.setUint16(32, blockAlign, true);
    view.setUint16(34, bytesPerSample * 8, true); // bits per sample
    writeStr(36, "data");
    view.setUint32(40, dataSize, true);

    let offset = 44;
    for (let i = 0; i < chData.length; i++) {
      const s = Math.max(-1, Math.min(1, chData[i]));
      view.setInt16(offset, s < 0 ? s * 0x8000 : s * 0x7fff, true);
      offset += 2;
    }
    return new Blob([arrayBuffer], { type: "audio/wav" });
  }

  async function recordingBlobToWav(blob) {
    const arrayBuffer = await blob.arrayBuffer();
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    const ctx = new AudioCtx();
    try {
      const decoded = await ctx.decodeAudioData(arrayBuffer);
      return audioBufferToWavBlob(decoded);
    } finally {
      ctx.close();
    }
  }

  // Server-reported master switch from voice_config.json's "enabled" key
  // (see /api/status's "voiceEnabled" and voice/config.py). Starts true so
  // nothing flashes disabled before status has loaded; updated for real in
  // applyVoiceEnabled() once boot's Api.status() resolves. micSupported()
  // is also what gates whether a "Speak" button gets created on jarvis
  // reply bubbles (see the ask-msg--jarvis branch above), so folding the
  // server-side switch into it disables both the mic button and every
  // future Speak button from this one flag.
  let voiceFeatureEnabled = true;

  function micSupported() {
    return voiceFeatureEnabled &&
      !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder);
  }

  const micBtn = qs("#btn-ask-mic");
  if (!micSupported()) {
    micBtn.disabled = true;
    micBtn.title = "Voice input isn't supported in this browser.";
  }

  function applyVoiceEnabled(enabled) {
    voiceFeatureEnabled = enabled;
    if (!enabled) {
      // Hide outright (not just disable) — the user asked for voice to be
      // fully turned off, front and back, not just greyed out.
      micBtn.style.display = "none";
      micBtn.disabled = true;
      micBtn.title = "Voice is disabled (voice_config.json).";
      qsa(".ask-msg__act[title=\"Read this reply aloud\"]").forEach((btn) => {
        btn.style.display = "none";
      });
    } else {
      micBtn.style.display = "";
      if (navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder) {
        micBtn.disabled = false;
        micBtn.title = "Record a voice message";
      }
    }
  }

  function setMicRecording(isRecording) {
    micBtn.classList.toggle("is-recording", isRecording);
    micBtn.title = isRecording ? "Stop recording" : "Record a voice message";
  }

  async function startVoiceRecording() {
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (e) {
      toast("Couldn't access the microphone.");
      return;
    }
    state.voiceStream = stream;
    state.voiceChunks = [];
    let recorder;
    try {
      recorder = new MediaRecorder(stream);
    } catch (e) {
      stream.getTracks().forEach((t) => t.stop());
      toast("This browser can't record audio.");
      return;
    }
    recorder.addEventListener("dataavailable", (e) => {
      if (e.data && e.data.size > 0) state.voiceChunks.push(e.data);
    });
    recorder.addEventListener("stop", onVoiceRecordingStopped);
    state.voiceRecorder = recorder;
    recorder.start();
    setMicRecording(true);
  }

  function stopVoiceRecording() {
    if (state.voiceRecorder && state.voiceRecorder.state !== "inactive") {
      state.voiceRecorder.stop();
    }
    if (state.voiceStream) {
      state.voiceStream.getTracks().forEach((t) => t.stop());
      state.voiceStream = null;
    }
    setMicRecording(false);
  }

  async function onVoiceRecordingStopped() {
    const chunks = state.voiceChunks;
    state.voiceChunks = [];
    state.voiceRecorder = null;
    if (!chunks.length) return;

    const rawBlob = new Blob(chunks, { type: chunks[0].type || "audio/webm" });
    micBtn.disabled = true;
    try {
      const wavBlob = await recordingBlobToWav(rawBlob);
      const { text } = await Api.voiceTranscribe(wavBlob);
      if (!text || !text.trim()) {
        toast("Didn't catch any speech.");
        return;
      }
      const input = qs("#ask-input");
      input.value = text;
      askInputAutoGrow(input);
      // Mirrors cli.py's `jarvis listen` (record -> transcribe -> ask ->
      // speak) — a mic-originated turn auto-sends and, per §3a, speaks
      // the reply back too (see finalizeAskBubble's hook), rather than
      // leaving text input as the only path.
      state.voiceTurnPending = true;
      qs("#ask-form").requestSubmit();
    } catch (e) {
      toast(e.message || "Transcription failed.");
    } finally {
      micBtn.disabled = false;
    }
  }

  micBtn.addEventListener("click", () => {
    if (state.voiceRecorder) {
      stopVoiceRecording();
    } else {
      startVoiceRecording();
    }
  });

  // ---- Voice output (Speak button on a jarvis reply bubble) -------------
  //
  // One shared <audio> element rather than a fresh one per click, so
  // starting a second Speak (or a mic-originated auto-speak) always
  // interrupts whatever was already playing instead of overlapping it.

  function voiceAudioEl() {
    if (!state.voiceAudioEl) {
      state.voiceAudioEl = new Audio();
      state.voiceAudioEl.addEventListener("ended", () => setSpeakingButton(null));
    }
    return state.voiceAudioEl;
  }

  function setSpeakingButton(btn) {
    if (state.voiceSpeakingBtn && state.voiceSpeakingBtn !== btn) {
      state.voiceSpeakingBtn.classList.remove("is-active");
      state.voiceSpeakingBtn.textContent = "Speak";
    }
    state.voiceSpeakingBtn = btn;
    if (btn) {
      btn.classList.add("is-active");
      btn.textContent = "Stop";
    }
  }

  async function speakText(text, btn) {
    const audio = voiceAudioEl();
    if (state.voiceSpeakingBtn === btn) {
      // Clicking "Stop" on the bubble that's currently speaking.
      audio.pause();
      setSpeakingButton(null);
      return;
    }
    if (!text || !text.trim()) {
      toast("Nothing to speak.");
      return;
    }
    if (btn) { btn.disabled = true; btn.textContent = "\u2026"; }
    // BUGFIX (stale response): two Speak clicks fired close together on
    // different bubbles could resolve out of order — nothing tracked which
    // request was still wanted, so a slow first request finishing after a
    // fast second one would clobber it and start playing the wrong reply.
    // A per-call token, checked after the await, makes a response that's no
    // longer the latest one a no-op instead.
    const requestToken = (state.voiceRequestSeq = (state.voiceRequestSeq || 0) + 1);
    let becameActive = false;
    try {
      const { blob } = await Api.voiceSpeak(text);
      if (state.voiceRequestSeq !== requestToken) return; // superseded by a newer Speak click
      audio.pause();
      // BUGFIX (leak): audio.src used to be reassigned to a fresh
      // createObjectURL() on every click with the previous one never
      // revoked — confirmed nowhere else in this file calls
      // revokeObjectURL either, so every Speak click leaked that blob for
      // the life of the tab.
      if (state.voiceAudioUrl) URL.revokeObjectURL(state.voiceAudioUrl);
      state.voiceAudioUrl = URL.createObjectURL(blob);
      audio.src = state.voiceAudioUrl;
      await audio.play();
      setSpeakingButton(btn);
      becameActive = true;
    } catch (e) {
      toast(e.message || "Speech synthesis failed.");
    } finally {
      if (btn) {
        btn.disabled = false;
        // Only the button that actually won the race stays "Stop" (already
        // set by setSpeakingButton above); a stale or failed one goes back
        // to its resting label instead of getting stuck on "…".
        if (!becameActive) btn.textContent = "Speak";
      }
    }
  }

  qs("#ask-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const input = qs("#ask-input");
    const text = input.value.trim();
    const quotes = state.askQuotes.slice();
    if (!text && quotes.length === 0) return;

    const skillSlashMatch = quotes.length === 0 ? SKILL_SLASH_RE.exec(text) : null;
    if (skillSlashMatch) {
      input.value = "";
      askInputAutoGrow(input);
      hideSkillSuggest();
      await handleSkillSlashCommand(skillSlashMatch[1].toLowerCase(), skillSlashMatch[2].trim());
      return;
    }

    const organizeMatch = quotes.length === 0 ? ORGANIZE_JSON_RE.exec(text) : null;
    if (organizeMatch) {
      if (!state.activeConversationId) await startNewConversation();
      input.value = "";
      askInputAutoGrow(input);
      hideSelPop();
      handleOrganizeJsonCommand(text, organizeMatch[1].trim());
      return;
    }

    // There is only one active child process per websocket connection
    // (server.js: ws.activeChild), so an ask really can't run concurrently
    // with another ask (or a plain run) even across conversations — that
    // part is by design, not a bug (see JARVIS_CONTEXT.md).
    //
    // The bug: when the in-flight ask belongs to a DIFFERENT conversation
    // than the one on screen, refreshAskBusyUI()'s isViewingAskThread()
    // check (correctly) leaves #ask-input/#btn-ask-send enabled here, since
    // it's only scoping the Stop-button/spinner UI to the conversation
    // that's actually busy. That left this handler's bare `if (state.running)
    // return;` as the only thing standing in the way — and it returned
    // silently, with no toast, no shake, nothing. From the user's
    // perspective, switching to an idle conversation while another one was
    // still replying made the whole app look permanently stuck: typing did
    // nothing and there was no indication why or when it would recover.
    // Surface it instead of eating the submit silently.
    if (state.running) {
      toast(
        isViewingAskThread()
          ? "Still replying \u2014 hang tight."
          : "Jarvis is still replying in another chat \u2014 wait for it to finish."
      );
      return;
    }
    if (!state.activeConversationId) await startNewConversation();
    // Captured once, right after the conversation is guaranteed to exist —
    // not re-read from state.activeConversationId further down. Between
    // this await and wsSend below, the user could switch to a different
    // conversation; reading state.activeConversationId twice could then
    // send this ask under one id but mark askConversationId with another,
    // misattributing every ask-stdout/ask-exit line that follows.
    const conversationId = state.activeConversationId;
    input.value = "";
    askInputAutoGrow(input);
    state.askQuotes = [];
    renderQuoteBar();
    hideSelPop();
    addUserBubble(text, quotes);
    ensureNotifPermission();
    state.lastTaskLabel = text || (quotes[0] || "that");
    state.askConversationId = conversationId;
    wsSend({
      type: "ask",
      text,
      quote: quotes.length ? quotes.join("\n---\n") : undefined,
      conversationId,
      provider: providerOverrideValue(),
    });
  });

  // ---- organize-json chat-bubble result --------------------------------

  // Echoes the typed command as a user-style bubble but deliberately
  // WITHOUT the shared addAskMsgActions() Redo button — Redo resends
  // through the AI ask pipeline (wsSend/ask), which would defeat the
  // entire point of this shortcut (zero API tokens, however big the file).
  function addOrganizeJsonUserBubble(rawText) {
    clearAskEmptyHint();
    const msg = el("div", { class: "ask-msg ask-msg--user" }, [
      el("div", { class: "ask-msg__role" }, "You"),
      el("div", { class: "ask-msg__bubble" }, rawText),
    ]);
    msg.dataset.raw = rawText;
    askThread.appendChild(msg);
    askThreadScrollToEnd();
    return msg;
  }

  function addOrganizeJsonPendingBubble() {
    clearAskEmptyHint();
    const msg = el("div", { class: "ask-msg ask-msg--jarvis is-pending" }, [
      el("div", { class: "ask-msg__role" }, currentAssistantName()),
      el("div", { class: "ask-msg__bubble" }, [
        el("span", { class: "ask-typing" }, [el("span", {}), el("span", {}), el("span", {})]),
      ]),
    ]);
    askThread.appendChild(msg);
    askThreadScrollToEnd();
    return msg;
  }

  function renderOrganizeJsonResult(msg, payload) {
    msg.classList.remove("is-pending");
    const bubble = qs(".ask-msg__bubble", msg);
    bubble.innerHTML = "";
    bubble.classList.add("ask-msg__bubble--jsontree");

    if (!payload || payload.ok === false) {
      msg.classList.add("is-error");
      bubble.appendChild(el("div", { class: "json-org-error" },
        (payload && payload.error) || "organize-json failed."));
      if (payload && payload.snippet) {
        bubble.appendChild(el("pre", { class: "json-org-snippet" }, payload.snippet));
      }
      msg.dataset.raw = (payload && payload.error) || "organize-json failed.";
      askThreadScrollToEnd();
      return;
    }

    const btnOrganized = el("button", { type: "button", class: "json-org-toggle-btn is-active" }, "Organized");
    const btnRaw = el("button", { type: "button", class: "json-org-toggle-btn" }, "Raw JSON");
    const viewWrap = el("div", { class: "json-org-view" });

    function showOrganized() {
      btnOrganized.classList.add("is-active");
      btnRaw.classList.remove("is-active");
      viewWrap.innerHTML = "";
      const treeMount = el("div", { class: "json-tree" });
      viewWrap.appendChild(treeMount);
      buildJsonTree(treeMount, payload.data, { editable: false });
    }
    function showRaw() {
      btnRaw.classList.add("is-active");
      btnOrganized.classList.remove("is-active");
      viewWrap.innerHTML = "";
      viewWrap.appendChild(el("pre", { class: "json-org-raw" }, JSON.stringify(payload.data, null, 2)));
    }
    btnOrganized.addEventListener("click", showOrganized);
    btnRaw.addEventListener("click", showRaw);

    bubble.appendChild(el("div", { class: "json-org-path" }, payload.path));
    bubble.appendChild(el("div", { class: "json-org-toggle" }, [btnOrganized, btnRaw]));
    bubble.appendChild(viewWrap);
    showOrganized();

    msg.dataset.raw = JSON.stringify(payload.data, null, 2);
    bubble.appendChild(el("div", { class: "ask-msg__actions ask-msg__actions--static" }, [
      el("button", {
        type: "button", class: "ask-msg__act", title: "Copy the underlying JSON",
        onclick: () => copyAskRaw(msg),
      }, "Copy JSON"),
    ]));
    askThreadScrollToEnd();
  }

  async function handleOrganizeJsonCommand(rawText, targetPath) {
    addOrganizeJsonUserBubble(rawText);
    const pending = addOrganizeJsonPendingBubble();
    if (!targetPath) {
      renderOrganizeJsonResult(pending, { ok: false, error: "usage: organize-json <path>" });
      return;
    }
    try {
      const payload = await Api.organizeJson(targetPath);
      renderOrganizeJsonResult(pending, payload);
    } catch (err) {
      renderOrganizeJsonResult(pending, err.data || { ok: false, error: err.message });
    }
  }

  // #ask-input is a <textarea> so a message can span multiple lines (the
  // CLI already handles embedded "\n" in a prompt string fine — this is
  // just about letting the web UI type one in). Enter sends, same as the
  // old single-line <input> used to; Shift+Enter inserts a real newline
  // instead, the same convention as Slack/Discord/etc.
  qs("#ask-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      qs("#ask-form").requestSubmit();
    }
  });

  // Grows the textarea to fit its content (up to the CSS max-height, after
  // which it scrolls) instead of staying a fixed single line.
  function askInputAutoGrow(input) {
    input.style.height = "auto";
    input.style.height = `${input.scrollHeight}px`;
  }
  qs("#ask-input").addEventListener("input", (e) => askInputAutoGrow(e.target));

  qs("#btn-ask-clear").addEventListener("click", async () => {
    try {
      await Api.clearAiHistory(state.activeConversationId);
    } catch (e) {
      toast(e.message);
      return;
    }
    state.askQuotes = [];
    renderQuoteBar();
    hideSelPop();
    askThread.innerHTML = "";
    askThread.appendChild(el("div", { class: "ask-empty" }, "Ask about anything, or tell me what you need done, sir."));
    askPromptReset();
    refreshConvoList();
    toast("Conversation cleared.", "info");
  });

  // ---- Ask panel: provider-override picker -----------------------------
  //
  // "Auto" (state.providerOverride is empty) means exactly the previous
  // behavior: ai_client.ask() tries every eligible provider in
  // defaults.provider_priority order, failing over as usual.
  //
  // Clicking providers in the menu builds an ordered try-list: the first
  // one you click becomes try #1, the second try #2, and so on — a badge
  // on each selected item shows its position. Clicking a selected item
  // again removes it and the remaining badges renumber. The resulting
  // order is sent as `provider` (a comma-joined string) on every
  // "ask"/redo WS message (see providerOverrideValue() and the wsSend
  // calls above) — server.js passes that straight through as
  // JARVIS_PROVIDER_OVERRIDE for that one subprocess call (see
  // jarvis-provider-override.patch and cli.py's
  // _parse_provider_override_value, which already treats a comma-
  // separated value as an ordered list). It's a client-side-only choice:
  // nothing is persisted server-side, and it resets to Auto on a full
  // page reload. Unlike "Auto", the menu stays open while you build a
  // multi-provider order — it only closes on "Auto", Escape, or an
  // outside click, so you can pick several in a row.
  const providerPickerEl = qs("#provider-picker");
  const providerBtn = qs("#btn-provider-override");
  const providerLabel = qs("#provider-override-label");
  const providerMenu = qs("#provider-picker-menu");

  // The wire value for "ask"/redo: undefined for Auto (send nothing, same
  // as before this existed), otherwise the picked names joined in click
  // order — e.g. ["openai","anthropic"] -> "openai,anthropic".
  function providerOverrideValue() {
    return state.providerOverride.length ? state.providerOverride.join(",") : undefined;
  }

  function renderProviderMenu() {
    providerMenu.innerHTML = "";
    const isAuto = state.providerOverride.length === 0;
    const autoItem = el("button", {
      type: "button",
      class: "provider-picker__item" + (isAuto ? " is-active" : ""),
      onclick: () => clearProviderOverride(),
    }, "Auto (priority order)");
    providerMenu.appendChild(autoItem);

    if (!state.aiProviders.length) {
      providerMenu.appendChild(el("div", { class: "provider-picker__empty" },
        "No configured providers found — check ai_config.json."));
      return;
    }
    for (const p of state.aiProviders) {
      const order = state.providerOverride.indexOf(p.name);
      const picked = order !== -1;
      const item = el("button", {
        type: "button",
        class: "provider-picker__item" + (picked ? " is-active" : ""),
        onclick: () => toggleProviderOverride(p.name),
      }, [
        el("span", { class: "provider-picker__item-main" }, [
          picked ? el("span", { class: "provider-picker__item-order" }, String(order + 1)) : null,
          el("span", {}, p.name),
        ]),
        p.model ? el("span", { class: "provider-picker__item-model" }, p.model) : null,
      ]);
      providerMenu.appendChild(item);
    }
  }

  function updateProviderLabel() {
    const picked = state.providerOverride;
    if (!picked.length) {
      providerLabel.textContent = "Provider: Auto";
    } else if (picked.length === 1) {
      providerLabel.textContent = `Provider: ${picked[0]}`;
    } else {
      providerLabel.textContent = `Provider: ${picked.join(" \u2192 ")}`;
    }
  }

  // Toggling is order-preserving: picking a new name appends it (making it
  // the last try), and un-picking an already-selected name just removes
  // it from wherever it sits — everything after it shifts down a slot,
  // which is exactly what re-rendering `order` from indexOf() reflects.
  function toggleProviderOverride(name) {
    const i = state.providerOverride.indexOf(name);
    if (i === -1) {
      state.providerOverride = [...state.providerOverride, name];
    } else {
      state.providerOverride = state.providerOverride.filter((n) => n !== name);
    }
    updateProviderLabel();
    renderProviderMenu();
  }

  function clearProviderOverride() {
    state.providerOverride = [];
    updateProviderLabel();
    closeProviderMenu();
  }

  function openProviderMenu() {
    renderProviderMenu();
    providerMenu.hidden = false;
    providerBtn.setAttribute("aria-expanded", "true");
  }

  function closeProviderMenu() {
    providerMenu.hidden = true;
    providerBtn.setAttribute("aria-expanded", "false");
  }

  async function loadAiProviders() {
    try {
      const data = await Api.listAiProviders();
      state.aiProviders = Array.isArray(data.providers) ? data.providers : [];
    } catch {
      // Non-fatal — the picker still opens with just "Auto" and a "no
      // providers found" note rather than blocking the rest of the app.
      state.aiProviders = [];
    }
  }

  providerBtn.addEventListener("click", async () => {
    if (!providerMenu.hidden) return closeProviderMenu();
    if (!state.aiProviders.length) await loadAiProviders();
    openProviderMenu();
  });

  document.addEventListener("click", (e) => {
    if (!providerMenu.hidden && !providerPickerEl.contains(e.target)) closeProviderMenu();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !providerMenu.hidden) closeProviderMenu();
  });

  // ===========================================================================
  // Thinking level \u2014 how hard the model reasons before answering (see
  // jarvis-cli/jarvis/reasoning.py). This used to be CLI-only (`jarvis think`)
  // with nothing in the web UI at all, so the only way to change it was a
  // terminal. GET/POST /api/think wrap that same command. Modeled on the
  // provider-picker right next to it: a button + small dropdown, one item
  // per level plus a Show/Hide toggle for the reasoning trace underneath.
  // ===========================================================================
  const THINK_LEVELS = ["off", "low", "medium", "high"];
  const THINK_LEVEL_LABEL = { off: "Off", low: "Low", medium: "Medium", high: "High" };
  const thinkPickerEl = qs("#think-picker");
  const thinkBtn = qs("#btn-think-override");
  const thinkLabel = qs("#think-level-label");
  const thinkMenu = qs("#think-picker-menu");
  const thinkState = { level: "off", show: false, loaded: false };

  function renderThinkMenu() {
    thinkMenu.innerHTML = "";
    for (const lvl of THINK_LEVELS) {
      thinkMenu.appendChild(el("button", {
        type: "button",
        class: "provider-picker__item" + (thinkState.level === lvl ? " is-active" : ""),
        onclick: () => setThinkLevel(lvl),
      }, THINK_LEVEL_LABEL[lvl]));
    }
    thinkMenu.appendChild(el("button", {
      type: "button",
      class: "provider-picker__item" + (thinkState.show ? " is-active" : ""),
      title: "Show the model's reasoning trace as its own bubble in the thread",
      onclick: () => setThinkShow(!thinkState.show),
    }, thinkState.show ? "\u2713 Show reasoning trace" : "Show reasoning trace"));
  }

  function updateThinkLabel() {
    thinkLabel.textContent = `Thinking: ${THINK_LEVEL_LABEL[thinkState.level] || "Off"}`;
  }

  async function setThinkLevel(level) {
    const prev = thinkState.level;
    thinkState.level = level;         // optimistic, same pattern as toggleLayout()
    updateThinkLabel();
    renderThinkMenu();
    try {
      await Api.post("/api/think", { level, show: thinkState.show });
    } catch (err) {
      thinkState.level = prev;
      updateThinkLabel();
      renderThinkMenu();
      toast(err.message || "Couldn't set the thinking level.");
    }
  }

  async function setThinkShow(show) {
    const prev = thinkState.show;
    thinkState.show = show;
    renderThinkMenu();
    try {
      await Api.post("/api/think", { level: thinkState.level, show });
    } catch (err) {
      thinkState.show = prev;
      renderThinkMenu();
      toast(err.message || "Couldn't change that.");
    }
  }

  async function loadThinkLevel() {
    try {
      const data = await Api.get("/api/think");
      thinkState.level = THINK_LEVELS.includes(data.level) ? data.level : "off";
      thinkState.show = Boolean(data.show);
    } catch {
      thinkState.level = "off";
      thinkState.show = false;
    }
    thinkState.loaded = true;
    updateThinkLabel();
  }

  function openThinkMenu() {
    renderThinkMenu();
    thinkMenu.hidden = false;
    thinkBtn.setAttribute("aria-expanded", "true");
  }

  function closeThinkMenu() {
    thinkMenu.hidden = true;
    thinkBtn.setAttribute("aria-expanded", "false");
  }

  thinkBtn?.addEventListener("click", async () => {
    if (!thinkMenu.hidden) return closeThinkMenu();
    if (!thinkState.loaded) await loadThinkLevel();
    openThinkMenu();
  });

  document.addEventListener("click", (e) => {
    if (!thinkMenu.hidden && !thinkPickerEl.contains(e.target)) closeThinkMenu();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !thinkMenu.hidden) closeThinkMenu();
  });

  loadThinkLevel();

  // ===========================================================================
  // Debug dashboard \u2014 every tool Jarvis can call, read live via /api/tools
  // (never hardcoded), with a form to run any of them and see the response.
  // ===========================================================================

  const debugOverlay = qs("#debug-overlay");
  const debugStatusLine = qs("#debug-status-line");
  const debugDocs = qs("#debug-docs");
  const debugUsage = qs("#debug-usage"); // Phase 0 (new_plan.md): see renderDebugUsage below
  const debugArgs = qs("#debug-args");
  const debugResponse = qs("#debug-response");
  const debugToolList = qs("#debug-tool-list");
  const debugToolCount = qs("#debug-tool-count");
  const btnDebugRun = qs("#btn-debug-run");

  // ---- Debug's own capacity switch ---------------------------------------
  // Looks and cycles just like the global #btn-mode-switch (same
  // modeOptions/optionFor/FALLBACK_MODE from the section above), but it
  // only ever writes to state.debugMode \u2014 it never calls Api.setMode, so
  // clicking it can't change what mode Jarvis is actually running in.
  // Nothing currently reads state.debugMode back into a tool call (tool-run
  // bypasses the model entirely), so today this is purely a local display
  // toggle, kept separate in case a debug-scoped mode override is wired up
  // to something later.
  const btnDebugModeSwitch = qs("#btn-debug-mode-switch");
  const debugModeSwitchLabel = qs("#debug-mode-switch-label");

  function renderDebugMode() {
    const current = optionFor(state.debugMode) || optionFor(FALLBACK_MODE.mode) || FALLBACK_MODE;
    const idx = modeOptions.indexOf(current);
    const next = modeOptions.length ? modeOptions[(Math.max(idx, 0) + 1) % modeOptions.length] : null;
    btnDebugModeSwitch.dataset.mode = current.mode;
    const base = "Local override for this debug panel only \u2014 does not touch Jarvis's real capacity mode.";
    btnDebugModeSwitch.title = current.summary
      ? `${base} ${current.label} \u2014 ${current.summary}${next ? ` Click to switch to ${next.label}.` : ""}`
      : base;
    debugModeSwitchLabel.textContent = current.label;
  }

  btnDebugModeSwitch.addEventListener("click", async () => {
    if (!modeOptions.length) { await loadMode(); if (!modeOptions.length) return; }
    const current = state.debugMode || FALLBACK_MODE.mode;
    const idx = modeOptions.findIndex((o) => o.mode === current);
    const next = modeOptions[(Math.max(idx, 0) + 1) % modeOptions.length];
    state.debugMode = next.mode; // local only \u2014 never posted to /api/mode
    renderDebugMode();
  });

  function debugFindTool(name) {
    return state.debugTools.find((t) => t.name === name) || null;
  }

  function debugFilteredTools() {
    const q = state.debugSearch.trim().toLowerCase();
    if (!q) return state.debugTools;
    return state.debugTools.filter((t) =>
      t.name.toLowerCase().includes(q) || (t.description || "").toLowerCase().includes(q)
    );
  }

  // Human-readable type label for a JSON-schema property, e.g. "string",
  // "array<string>", "enum".
  function debugParamTypeLabel(prop) {
    if (!prop || typeof prop !== "object") return "any";
    if (Array.isArray(prop.enum)) return "enum";
    if (prop.type === "array") {
      const items = prop.items && prop.items.type ? prop.items.type : "any";
      return `array<${items}>`;
    }
    return prop.type || "any";
  }

  function debugParamEntries(schema) {
    const props = (schema && schema.properties) || {};
    const required = new Set((schema && schema.required) || []);
    return Object.entries(props).map(([name, prop]) => ({
      name,
      prop: prop || {},
      required: required.has(name),
    }));
  }

  // ---- LEFT pane: description + parameter docs -----------------------------

  // A single "Toggle warning:"/"Toggle AI review:" switch under a tool's
  // description. Flips jarvis-cli/jarvis/tool_safety.json's per-tool flags
  // immediately (no separate save step) via /api/tools/safety, and mutates
  // the in-memory tool object in place so the confirm flow below reads the
  // change right away without a full tool-list reload.
  function debugToggleRow(tool, key, label) {
    const input = el("input", { type: "checkbox", class: "safety-toggle__input" });
    input.checked = !!tool[key];
    const row = el("label", { class: "safety-toggle" }, [
      input,
      el("span", { class: "safety-toggle__track" }),
      el("span", { class: "safety-toggle__label" }, label),
    ]);
    input.addEventListener("change", async () => {
      const next = input.checked;
      input.disabled = true;
      try {
        const updated = await Api.setToolSafety(tool.name, key, next);
        tool[key] = !!(updated && key in updated ? updated[key] : next);
        input.checked = tool[key];
      } catch (e) {
        input.checked = !next; // revert on failure
        toast(`Couldn't update ${label.toLowerCase()} for ${tool.name}: ${e.message}`);
      } finally {
        input.disabled = false;
      }
    });
    return row;
  }

  // Phase 0 (new_plan.md) baseline: render the most recent ask's token/tool/
  // round breakdown into the debug panel. Called on "ask-usage" (handleWsMessage)
  // and once on debug-overlay open so a prior turn's numbers are still visible.
  function renderDebugUsage() {
    if (!debugUsage) return;
    debugUsage.innerHTML = "";
    const u = state.debugLastUsage;
    if (!u) {
      debugUsage.appendChild(el("div", { class: "debug-empty" }, "No ask measured yet this session."));
      return;
    }
    const rounds = u.rounds || [];
    const toolCalls = u.tool_calls || [];
    debugUsage.appendChild(el("div", { class: "debug-docs__desc" },
      `input=${u.input_tokens || 0}  output=${u.output_tokens || 0}  ` +
      `total=${u.total_tokens || 0}  rounds=${rounds.length}  tool calls=${toolCalls.length}`
    ));
    if (rounds.length) {
      debugUsage.appendChild(el("div", { class: "debug-docs__section-title" }, "Per round (reported)"));
      for (const r of rounds) {
        debugUsage.appendChild(el("div", { class: "debug-empty" },
          `round ${r.round}: in=${r.input_tokens || 0} out=${r.output_tokens || 0}`
        ));
      }
    }
    if (toolCalls.length) {
      debugUsage.appendChild(el("div", { class: "debug-docs__section-title" }, "Per tool call (estimated)"));
      for (const t of toolCalls) {
        debugUsage.appendChild(el("div", { class: "debug-empty" },
          `${t.name} (round ${t.round}): in\u2248${t.input_tokens || 0} out\u2248${t.output_tokens || 0}`
        ));
      }
    }
  }

  function renderDebugDocs(tool) {
    debugDocs.innerHTML = "";
    if (!tool) {
      debugDocs.appendChild(el("div", { class: "debug-empty" }, "Select a tool from the list on the right."));
      return;
    }
    debugDocs.appendChild(el("div", { class: "debug-docs__name" }, tool.name));
    debugDocs.appendChild(el("div", { class: "debug-docs__desc" },
      tool.description || "No description provided by this tool."));

    debugDocs.appendChild(el("div", { class: "debug-docs__safety" }, [
      debugToggleRow(tool, "confirm_required", "Toggle warning:"),
      debugToggleRow(tool, "ai_review", "Toggle AI review:"),
    ]));

    const entries = debugParamEntries(tool.parameters);
    if (!entries.length) {
      debugDocs.appendChild(el("div", { class: "debug-docs__section-title" }, "Parameters"));
      debugDocs.appendChild(el("div", { class: "debug-empty" }, "This tool takes no arguments."));
      return;
    }
    debugDocs.appendChild(el("div", { class: "debug-docs__section-title" }, "Parameters"));
    for (const { name, prop, required } of entries) {
      const head = el("div", { class: "debug-param-doc__name" }, [
        name,
        el("span", { class: "debug-param-doc__type" }, debugParamTypeLabel(prop)),
        required ? el("span", { class: "debug-param-doc__req" }, "required") : null,
      ]);
      const doc = el("div", { class: "debug-param-doc" }, [head]);
      if (prop.description) {
        doc.appendChild(el("div", { class: "debug-param-doc__desc" }, prop.description));
      }
      if (Array.isArray(prop.enum)) {
        doc.appendChild(el("div", { class: "debug-param-doc__enum" }, `one of: ${prop.enum.join(", ")}`));
      }
      debugDocs.appendChild(doc);
    }
  }

  // ---- MIDDLE pane: argument form -------------------------------------------

  // Builds one labeled input appropriate to the property's JSON-schema type.
  // Returns the input/textarea/select element so the run handler can read it.
  function debugBuildArgInput(name, prop, required) {
    const type = prop && prop.type;

    if (Array.isArray(prop && prop.enum)) {
      const select = el("select", { "data-arg": name });
      if (!required) select.appendChild(el("option", { value: "" }, "(omit)"));
      for (const v of prop.enum) select.appendChild(el("option", { value: v }, String(v)));
      if (prop.default !== undefined) select.value = String(prop.default);
      return select;
    }
    if (type === "boolean") {
      const wrap = el("label", { class: "check" });
      const input = el("input", { type: "checkbox", "data-arg": name, "data-argtype": "boolean" });
      if (prop.default === true) input.checked = true;
      wrap.appendChild(input);
      wrap.appendChild(document.createTextNode(" true"));
      wrap.__debugInput = input; // caller reads .__debugInput when the field itself is a <label>
      return wrap;
    }
    if (type === "number" || type === "integer") {
      const input = el("input", {
        type: "number", "data-arg": name, "data-argtype": type,
        placeholder: prop.default !== undefined ? String(prop.default) : "",
      });
      if (prop.minimum !== undefined) input.min = prop.minimum;
      if (prop.maximum !== undefined) input.max = prop.maximum;
      return input;
    }
    if (type === "array" || type === "object") {
      const input = el("textarea", {
        "data-arg": name, "data-argtype": type,
        placeholder: type === "array" ? "[\"item1\", \"item2\"]" : "{\"key\": \"value\"}",
      });
      return input;
    }
    // string, or unknown \u2014 default to a plain text input
    const input = el("input", {
      type: "text", "data-arg": name, "data-argtype": "string",
      placeholder: prop && prop.default !== undefined ? String(prop.default) : "",
    });
    return input;
  }

  function renderDebugArgs(tool) {
    debugArgs.innerHTML = "";
    if (!tool) {
      debugArgs.appendChild(el("div", { class: "debug-empty" }, "Nothing selected yet."));
      btnDebugRun.disabled = true;
      return;
    }
    btnDebugRun.disabled = false;
    const entries = debugParamEntries(tool.parameters);
    if (!entries.length) {
      debugArgs.appendChild(el("div", { class: "debug-empty" }, "This tool takes no arguments \u2014 just hit Run."));
      return;
    }
    for (const { name, prop, required } of entries) {
      const label = el("div", { class: "debug-arg-field__label" }, [
        name,
        required ? el("span", { class: "debug-arg-field__req" }, "required") : null,
      ]);
      const input = debugBuildArgInput(name, prop, required);
      const field = el("div", { class: "debug-arg-field" }, [label, input]);
      debugArgs.appendChild(field);
    }
  }

  // Reads every [data-arg] control under #debug-args back into a plain
  // object, coercing each value to the type the schema said it should be.
  // Throws with a friendly message if a field can't be coerced.
  function debugCollectArgs() {
    const out = {};
    for (const node of qsa("[data-arg]", debugArgs)) {
      const name = node.getAttribute("data-arg");
      const argtype = node.getAttribute("data-argtype");
      if (node.tagName === "SELECT") {
        if (node.value !== "") out[name] = node.value;
        continue;
      }
      if (argtype === "boolean") {
        out[name] = node.checked;
        continue;
      }
      const raw = node.value;
      if (raw === "" || raw == null) continue; // omit empty optional/required-but-blank fields
      if (argtype === "number" || argtype === "integer") {
        const n = Number(raw);
        if (Number.isNaN(n)) throw new Error(`"${name}" must be a number.`);
        out[name] = argtype === "integer" ? Math.trunc(n) : n;
        continue;
      }
      if (argtype === "array" || argtype === "object") {
        try {
          out[name] = JSON.parse(raw);
        } catch (e) {
          throw new Error(`"${name}" must be valid JSON (${e.message}).`);
        }
        continue;
      }
      out[name] = raw;
    }
    return out;
  }

  // ---- BOTTOM: response viewer (organized / raw JSON) -----------------------

  // Renders any JSON value as a plain indented key: value tree \u2014 the
  // "organized" view, easier to scan than a raw dump for typical tool
  // results (flat-ish objects, small arrays).
  function debugRenderOrganized(value, depth = 0) {
    if (value === null || value === undefined) {
      return el("span", { class: "debug-kv__val" }, String(value));
    }
    if (Array.isArray(value)) {
      if (!value.length) return el("span", { class: "debug-kv__val" }, "[]");
      const wrap = el("div", { class: "debug-kv" });
      value.forEach((item, i) => {
        const row = el("div", { class: "debug-kv__row" }, [
          el("span", { class: "debug-kv__key" }, `[${i}]`),
        ]);
        if (item !== null && typeof item === "object") {
          row.appendChild(debugRenderOrganized(item, depth + 1));
        } else {
          row.appendChild(el("span", { class: "debug-kv__val" }, String(item)));
        }
        wrap.appendChild(row);
      });
      return wrap;
    }
    if (typeof value === "object") {
      const keys = Object.keys(value);
      if (!keys.length) return el("span", { class: "debug-kv__val" }, "{}");
      const wrap = el("div", { class: "debug-kv" });
      for (const k of keys) {
        const v = value[k];
        const row = el("div", { class: "debug-kv__row" }, [el("span", { class: "debug-kv__key" }, `${k}:`)]);
        if (v !== null && typeof v === "object") {
          row.appendChild(debugRenderOrganized(v, depth + 1));
        } else {
          row.appendChild(el("span", { class: "debug-kv__val" }, String(v)));
        }
        wrap.appendChild(row);
      }
      return wrap;
    }
    return el("span", { class: "debug-kv__val" }, String(value));
  }

  function renderDebugResponse() {
    debugResponse.innerHTML = "";
    debugResponse.classList.remove("is-error");
    if (state.debugPendingConfirm) {
      debugResponse.appendChild(debugRenderConfirmPending(state.debugPendingConfirm));
      return;
    }
    const last = state.debugLastResult;
    if (!last) {
      debugResponse.appendChild(el("div", { class: "debug-empty" }, "Response will appear here after you run a tool."));
      return;
    }
    if (last.error) {
      debugResponse.classList.add("is-error");
      debugResponse.appendChild(el("pre", {}, last.error));
      return;
    }
    const payload = last.result;
    if (state.debugResponseMode === "raw") {
      debugResponse.appendChild(el("pre", {}, JSON.stringify(payload, null, 2)));
    } else {
      debugResponse.appendChild(debugRenderOrganized(payload));
    }
  }

  // Shown in the response pane, in place of a result, while a
  // confirm_required tool's run is waiting on Yes/No — mirrors the same
  // "raw JSON has the prompt + buttons" shape as the chat confirm bubble
  // (see addAskConfirmBubble), just scoped to a manual debug-dashboard run.
  function debugRenderConfirmPending(pending) {
    const wrap = el("div", { class: "debug-confirm" });
    if (state.debugResponseMode === "raw") {
      wrap.appendChild(el("pre", {}, JSON.stringify({
        tool: pending.name, arguments: pending.arguments, risk_note: pending.risk_note,
      }, null, 2)));
    } else {
      wrap.appendChild(el("div", { class: "debug-confirm__tool" }, pending.name));
      wrap.appendChild(confirmValuePre(pending.arguments || {}, "debug-confirm__args"));
      if (pending.risk_note && pending.risk_note.command_run !== undefined && pending.risk_note.command_run !== null) {
        wrap.appendChild(el("div", { class: "debug-confirm__risk-label" }, "Command:"));
        wrap.appendChild(confirmValuePre(pending.risk_note.command_run, "debug-confirm__args"));
      }
      if (pending.risk_note && pending.risk_note.note) {
        const label = pending.risk_note.provider ? `AI review \u2014 ${pending.risk_note.provider}` : "AI review";
        wrap.appendChild(el("div", { class: "debug-confirm__risk" }, [
          el("div", { class: "debug-confirm__risk-label" }, label),
          el("div", { class: "debug-confirm__risk-note" }, pending.risk_note.note),
        ]));
      }
      if (pending.risk_note && pending.risk_note.command_flags) {
        const cf = pending.risk_note.command_flags;
        wrap.appendChild(el("div", { class: "debug-confirm__flags" },
          `Flags: confirm_required=${!!cf.confirm_required}, ai_review=${!!cf.ai_review}`));
      }
    }
    const yesBtn = el("button", { class: "btn btn--primary", type: "button" }, "Y \u2014 run it");
    const noBtn = el("button", { class: "btn btn--ghost", type: "button" }, "N \u2014 cancel");
    wrap.appendChild(el("div", { class: "debug-confirm__actions" }, [yesBtn, noBtn]));

    yesBtn.addEventListener("click", async () => {
      yesBtn.disabled = true;
      noBtn.disabled = true;
      debugStatusLine.textContent = `running ${pending.name}\u2026`;
      debugStatusLine.classList.add("is-busy");
      debugStatusLine.classList.remove("is-error", "is-ok");
      try {
        await debugRunNow(pending.name, pending.arguments);
      } catch (e) {
        state.debugLastResult = { error: e.message };
        debugStatusLine.textContent = "request failed";
        debugStatusLine.classList.add("is-error");
      } finally {
        state.debugPendingConfirm = null;
        debugStatusLine.classList.remove("is-busy");
        renderDebugResponse();
      }
    });
    noBtn.addEventListener("click", () => {
      state.debugPendingConfirm = null;
      state.debugLastResult = { result: { ok: false, cancelled: true, message: `You declined to run '${pending.name}'.` } };
      debugStatusLine.textContent = "cancelled";
      debugStatusLine.classList.remove("is-error", "is-ok", "is-busy");
      renderDebugResponse();
    });
    return wrap;
  }

  qs("#debug-response-toggle").addEventListener("click", (e) => {
    const btn = e.target.closest(".debug-toggle-btn");
    if (!btn) return;
    state.debugResponseMode = btn.dataset.mode;
    qsa(".debug-toggle-btn", debugOverlay).forEach((b) => b.classList.toggle("is-active", b === btn));
    renderDebugResponse();
  });

  // ---- RIGHT pane: tool list -------------------------------------------------

  function renderDebugToolList() {
    const tools = debugFilteredTools();
    debugToolCount.textContent = `${state.debugTools.length}`;
    debugToolList.innerHTML = "";
    if (!tools.length) {
      debugToolList.appendChild(el("div", { class: "debug-empty" },
        state.debugTools.length ? "No tools match your search." : "No tools reported by jarvis."));
      return;
    }
    for (const tool of tools) {
      const card = el("div", {
        class: "debug-tool-card" + (tool.name === state.debugSelected ? " is-active" : ""),
        onclick: () => debugSelectTool(tool.name),
      }, [
        el("div", { class: "debug-tool-card__name" }, tool.name),
        tool.description ? el("div", { class: "debug-tool-card__desc" }, tool.description) : null,
      ]);
      debugToolList.appendChild(card);
    }
  }

  function debugSelectTool(name) {
    state.debugSelected = name;
    state.debugLastResult = null;
    state.debugPendingConfirm = null;
    const tool = debugFindTool(name);
    renderDebugDocs(tool);
    renderDebugArgs(tool);
    renderDebugResponse();
    renderDebugToolList();
  }

  qs("#debug-search").addEventListener("input", (e) => {
    state.debugSearch = e.target.value;
    renderDebugToolList();
  });

  // ---- Run ------------------------------------------------------------------

  // Actually calls /api/tools/run and stashes the result — split out so
  // both the direct (non-sensitive) path below and the confirm dialog's
  // Yes button (debugRenderConfirmPending) can share it.
  async function debugRunNow(name, args) {
    const res = await Api.runTool(name, args, state.debugMode);
    state.debugLastResult = res.ok !== false ? { result: res.result } : { error: res.error || "Tool run failed." };
    debugStatusLine.textContent = res.ok !== false ? "done" : "tool run failed";
    debugStatusLine.classList.toggle("is-error", res.ok === false);
    debugStatusLine.classList.toggle("is-ok", res.ok !== false);
  }

  btnDebugRun.addEventListener("click", async () => {
    if (!state.debugSelected) return;
    let args;
    try {
      args = debugCollectArgs();
    } catch (e) {
      state.debugLastResult = { error: e.message };
      state.debugPendingConfirm = null;
      renderDebugResponse();
      return;
    }
    const tool = debugFindTool(state.debugSelected);
    btnDebugRun.disabled = true;
    debugStatusLine.classList.remove("is-error", "is-ok");
    try {
      if (tool && tool.confirm_required) {
        // Don't run anything yet — ask jarvis what this call would do (and,
        // if ai_review is on for this tool, a second provider's risk note),
        // then wait for Yes/No before ever calling /api/tools/run for real.
        debugStatusLine.textContent = `checking ${state.debugSelected}\u2026`;
        debugStatusLine.classList.add("is-busy");
        state.debugLastResult = null;
        const preview = await Api.previewTool(state.debugSelected, args, state.debugMode);
        state.debugPendingConfirm = {
          name: state.debugSelected,
          arguments: args,
          risk_note: (preview && preview.risk_note) || null,
        };
        debugStatusLine.textContent = "awaiting confirmation";
      } else {
        debugStatusLine.textContent = `running ${state.debugSelected}\u2026`;
        debugStatusLine.classList.add("is-busy");
        await debugRunNow(state.debugSelected, args);
      }
    } catch (e) {
      state.debugLastResult = { error: e.message };
      state.debugPendingConfirm = null;
      debugStatusLine.textContent = "request failed";
      debugStatusLine.classList.add("is-error");
    } finally {
      debugStatusLine.classList.remove("is-busy");
      btnDebugRun.disabled = false;
      renderDebugResponse();
    }
  });

  // ---- Open / close / load ----------------------------------------------------

  async function openDebug() {
    debugOverlay.hidden = false;
    if (!state.debugMode) {
      if (!modeOptions.length) await loadMode();
      // Seed from whatever the global switch currently shows, purely as a
      // starting point \u2014 from here the two are independent.
      state.debugMode = qs("#btn-mode-switch").dataset.mode || FALLBACK_MODE.mode;
    }
    renderDebugMode();
    renderDebugUsage();
    if (state.debugLoaded) return;
    debugStatusLine.textContent = "reading tool catalog\u2026";
    debugStatusLine.classList.add("is-busy");
    try {
      const tools = await Api.listTools();
      state.debugTools = Array.isArray(tools) ? tools : [];
      state.debugLoaded = true;
      debugStatusLine.textContent = `${state.debugTools.length} tools available`;
    } catch (e) {
      debugStatusLine.textContent = `couldn't load tools: ${e.message}`;
      debugStatusLine.classList.add("is-error");
    } finally {
      debugStatusLine.classList.remove("is-busy");
      renderDebugToolList();
    }
  }

  function closeDebug() {
    debugOverlay.hidden = true;
  }

  // ===========================================================================
  // Skills manager — see jarvis-cli/jarvis/skills.py.
  //
  // A skill is a folder of markdown Jarvis loads ONLY when a task matches it.
  // What's in the prompt at all times is one line per skill (name +
  // description); the instructions cost nothing until load_skill is called.
  // That's why the header shows a live token cost for the catalog: it's the
  // only number that grows with skills-you-have rather than skills-you-use,
  // so it's the one worth watching.
  //
  // Everything here goes through /api/skills/*, which proxies the dedicated
  // `jarvis skills-*` commands — not /api/tools/run. The manager is a person
  // editing their own files and shouldn't inherit the model-facing confirm
  // gate on remove_skill.
  // ===========================================================================
  const skillsOverlay = qs("#skills-overlay");
  const skillsList = qs("#skills-list");
  const skillsStatus = qs("#skills-status-line");
  const skillsCost = qs("#skills-cost");
  const skillEditor = qs("#skills-editor");
  const skillCreate = qs("#skills-create");
  const skillImport = qs("#skills-import");
  const skillEditorEmpty = qs("#skills-editor-empty");
  const skillEditorTitle = qs("#skill-editor-title");
  const skillContent = qs("#skill-content");
  const skillRefs = qs("#skill-refs");
  const btnSkillSave = qs("#btn-skill-save");
  const btnSkillDelete = qs("#btn-skill-delete");

  let skillsCache = [];
  let selectedSkill = null;

  function skillsPane(which) {
    // Exactly one of editor / create / import is ever visible; the empty
    // state shows only when none of them is.
    skillEditor.hidden = which !== "editor";
    skillCreate.hidden = which !== "create";
    skillImport.hidden = which !== "import";
    skillEditorEmpty.hidden = which !== "none";
    const editing = which === "editor";
    btnSkillSave.disabled = !editing;
    btnSkillDelete.disabled = !editing;
  }

  function renderSkillsList() {
    skillsList.innerHTML = "";
    if (!skillsCache.length) {
      const empty = document.createElement("div");
      empty.className = "skills-empty";
      empty.textContent = "No skills yet. \u201c+ New\u201d writes one, \u201cImport\u201d installs an existing SKILL.md.";
      skillsList.appendChild(empty);
      return;
    }
    for (const skill of skillsCache) {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "skill-row" + (selectedSkill === skill.name ? " is-active" : "")
        + (skill.valid ? "" : " is-invalid");
      const name = document.createElement("div");
      name.className = "skill-row__name";
      name.textContent = skill.name;
      const desc = document.createElement("div");
      desc.className = "skill-row__desc";
      // An invalid skill is shown WITH its reason rather than hidden: a
      // skill the user thinks they installed but that silently never loads
      // is the worst possible failure mode for this feature.
      desc.textContent = skill.valid
        ? (skill.description || "")
        : (skill.error || "Invalid skill.");
      row.appendChild(name);
      row.appendChild(desc);
      if (skill.references && skill.references.length) {
        const refs = document.createElement("div");
        refs.className = "skill-row__refs";
        refs.textContent = `${skill.references.length} reference file${skill.references.length === 1 ? "" : "s"}`;
        row.appendChild(refs);
      }
      row.addEventListener("click", () => selectSkill(skill.name));
      skillsList.appendChild(row);
    }
  }

  async function loadSkills(selectAfter) {
    try {
      const data = await api("GET", "/api/skills");
      skillsCache = data.skills || [];
      const stats = data.stats || {};
      skillsCost.textContent = `${stats.catalog_tokens || 0} tok in every prompt`;
      // Keep the slash-command autocomplete's name list in sync whenever
      // the manager refreshes (create/save/remove all call this) instead of
      // only ever fetching it lazily on first keystroke.
      skillNamesCache = skillsCache.map((s) => s.name);
      skillsStatus.textContent = skillsCache.length
        ? `${stats.valid || 0} of ${skillsCache.length} loadable \u00b7 instructions load on demand`
        : "no skills installed";
      renderSkillsList();
      if (selectAfter) await selectSkill(selectAfter);
    } catch (e) {
      skillsStatus.textContent = "couldn't read skills";
      toast(e.message || "Couldn't list skills.");
    }
  }

  async function selectSkill(name) {
    try {
      const data = await api("GET", `/api/skills/${encodeURIComponent(name)}`);
      selectedSkill = name;
      skillContent.value = data.content || "";
      skillEditorTitle.textContent = name;
      const skill = skillsCache.find((s) => s.name === name);
      const refs = (skill && skill.references) || [];
      if (refs.length) {
        skillRefs.hidden = false;
        skillRefs.textContent = `Reference files (loaded one at a time, only when needed): ${refs.join(", ")}`;
      } else {
        skillRefs.hidden = true;
      }
      skillsPane("editor");
      renderSkillsList();
    } catch (e) {
      toast(e.message || "Couldn't open that skill.");
    }
  }

  qs("#btn-skill-save").addEventListener("click", async () => {
    if (!selectedSkill) return;
    try {
      await api("PUT", `/api/skills/${encodeURIComponent(selectedSkill)}`, { content: skillContent.value });
      toast("Skill saved.", "info");
      await loadSkills(selectedSkill);
    } catch (e) {
      // The backend refuses a save whose frontmatter has no description,
      // because that would leave a skill installed but permanently
      // undiscoverable. Surface the reason instead of failing quietly.
      toast(e.message || "Couldn't save.");
    }
  });

  qs("#btn-skill-delete").addEventListener("click", async () => {
    if (!selectedSkill) return;
    if (!window.confirm(`Delete "${selectedSkill}" and everything in its folder? This can't be undone.`)) return;
    try {
      await api("DELETE", `/api/skills/${encodeURIComponent(selectedSkill)}`);
      toast("Skill removed.", "info");
      selectedSkill = null;
      skillsPane("none");
      await loadSkills();
    } catch (e) {
      toast(e.message || "Couldn't remove that skill.");
    }
  });

  qs("#btn-skill-new").addEventListener("click", () => {
    selectedSkill = null;
    qs("#skill-new-name").value = "";
    qs("#skill-new-desc").value = "";
    qs("#skill-new-body").value = "";
    skillEditorTitle.textContent = "New skill";
    skillsPane("create");
    renderSkillsList();
  });

  const skillImportZip = qs("#skill-import-zip");
  const skillImportZipName = qs("#skill-import-zip-name");

  qs("#btn-skill-import").addEventListener("click", () => {
    selectedSkill = null;
    qs("#skill-import-src").value = "";
    skillImportZip.value = "";
    skillImportZipName.textContent = "No file selected.";
    skillEditorTitle.textContent = "Import skill";
    skillsPane("import");
    renderSkillsList();
  });

  skillImportZip.addEventListener("change", () => {
    const file = skillImportZip.files && skillImportZip.files[0];
    skillImportZipName.textContent = file
      ? `${file.name} (${(file.size / 1024).toFixed(0)} KB)`
      : "No file selected.";
  });

  qs("#btn-skill-create").addEventListener("click", async () => {
    const name = qs("#skill-new-name").value.trim();
    const description = qs("#skill-new-desc").value.trim();
    const instructions = qs("#skill-new-body").value;
    if (!name || !description || !instructions.trim()) {
      toast("Name, description and instructions are all required.");
      return;
    }
    try {
      const created = await api("POST", "/api/skills", { mode: "create", name, description, instructions });
      toast(`Created "${created.name || name}".`, "info");
      await loadSkills(created.name || name);
    } catch (e) {
      toast(e.message || "Couldn't create that skill.");
    }
  });

  // A zip skill (real scripts + several reference docs) doesn't fit the
  // JSON path the paste/path form uses, so a selected file goes through the
  // raw-body /api/skills/upload endpoint instead — same "which shape did
  // the user give me" branch skills.add_skill() makes on the Python side,
  // just decided one layer up here because a File object and a pasted
  // string need genuinely different fetch() calls, not just different args.
  qs("#btn-skill-install").addEventListener("click", async () => {
    const file = skillImportZip.files && skillImportZip.files[0];
    if (file) {
      try {
        const res = await fetch("/api/skills/upload", {
          method: "POST",
          headers: { "Content-Type": "application/zip" },
          body: file,
        });
        let data = null;
        try { data = await res.json(); } catch { /* no body */ }
        if (!res.ok) throw new Error((data && data.error) || `Upload failed (${res.status})`);
        toast(`Installed "${data.name || data.slug}".`, "info");
        await loadSkills(data.name || data.slug);
      } catch (e) {
        toast(e.message || "Couldn't install that zip.");
      }
      return;
    }
    const source = qs("#skill-import-src").value;
    if (!source.trim()) { toast("Upload a .zip, paste a SKILL.md, or give a path."); return; }
    try {
      const added = await api("POST", "/api/skills", { mode: "add", source });
      toast(`Installed "${added.name || added.slug}".`, "info");
      await loadSkills(added.name || added.slug);
    } catch (e) {
      toast(e.message || "Couldn't install that skill.");
    }
  });

  function openSkills() {
    skillsOverlay.hidden = false;
    skillsPane("none");
    selectedSkill = null;
    loadSkills();
  }

  function closeSkills() {
    skillsOverlay.hidden = true;
  }

  // ===========================================================================
  // Manual skill loading — "/skillload <name>" / "/skillunload <name>" typed
  // directly into the chat box, plus "/skillmake" / "/skilladd" as shortcuts
  // that open the manager to the right pane. Recognized and handled locally,
  // same "never sent to the model" pattern as ORGANIZE_JSON_RE just above.
  // load/unload are the CLI's `jarvis skillload`/`skillunload` one layer up
  // (see skill_stickiness.py) — this forces the skill's full instructions
  // into every ask for this conversation until unloaded, rather than hoping
  // the model calls its own load_skill tool.
  // ===========================================================================
  const SKILL_SLASH_RE = /^\/skill(load|unload|make|add)\b\s*(.*)$/i;
  const SKILL_SUGGEST_RE = /^\/skill(load|unload)\s+(\S*)$/i;
  let skillNamesCache = null;

  async function ensureSkillNamesCache() {
    if (skillNamesCache) return skillNamesCache;
    try {
      const data = await api("GET", "/api/skills");
      skillNamesCache = (data.skills || []).map((s) => s.name);
    } catch {
      skillNamesCache = [];
    }
    return skillNamesCache;
  }

  function hideSkillSuggest() {
    const box = qs("#skill-slash-suggest");
    if (box) box.hidden = true;
  }

  async function updateSkillSuggest(text) {
    const match = SKILL_SUGGEST_RE.exec(text);
    const box = qs("#skill-slash-suggest");
    if (!match || !box) { hideSkillSuggest(); return; }
    const verb = match[1].toLowerCase();
    const partial = match[2].toLowerCase();
    const names = await ensureSkillNamesCache();
    const hits = names.filter((n) => n.toLowerCase().includes(partial)).slice(0, 8);
    if (!hits.length) { hideSkillSuggest(); return; }
    box.innerHTML = "";
    for (const name of hits) {
      const item = document.createElement("button");
      item.type = "button";
      item.className = "skill-slash-suggest__item";
      item.textContent = name;
      // mousedown, not click: fires before the textarea's blur handler, so
      // the suggestion lands in the input before hideSkillSuggest() (wired
      // to blur, below) would otherwise race it and close the dropdown
      // with nothing selected.
      item.addEventListener("mousedown", (e) => {
        e.preventDefault();
        const input = qs("#ask-input");
        input.value = `/skill${verb} ${name} `;
        askInputAutoGrow(input);
        input.focus();
        hideSkillSuggest();
      });
      box.appendChild(item);
    }
    box.hidden = false;
  }

  qs("#ask-input").addEventListener("input", (e) => updateSkillSuggest(e.target.value));
  qs("#ask-input").addEventListener("blur", () => setTimeout(hideSkillSuggest, 150));

  async function handleSkillSlashCommand(verb, arg) {
    // "make"/"add" don't take a meaningful single-line argument (a whole
    // skill, or a folder/zip path, doesn't fit one chat line) — they just
    // open the manager to the pane that handles them, same as clicking the
    // toolbar buttons directly.
    if (verb === "make") { openSkills(); qs("#btn-skill-new").click(); return; }
    if (verb === "add") { openSkills(); qs("#btn-skill-import").click(); return; }
    if (!arg) { toast(`Type a skill name: /skill${verb} <name>`); return; }
    try {
      if (verb === "load") {
        const data = await api("POST", `/api/skills/${encodeURIComponent(arg)}/load`,
          { conversationId: state.activeConversationId });
        toast(`"${data.name || arg}" loaded for this chat \u2014 Jarvis will use it from the next reply on.`, "info");
      } else {
        const data = await api("DELETE", `/api/skills/${encodeURIComponent(arg)}/load`,
          { conversationId: state.activeConversationId });
        toast(`"${data.name || arg}" unloaded.`, "info");
      }
      skillNamesCache = null; // stale after add/remove elsewhere; cheap to just refetch next time
    } catch (e) {
      toast(e.message || `Couldn't ${verb} that skill.`);
    }
  }

  // #btn-skills no longer exists (opening Skills now goes through the
  // panel-menu dropdown's #menu-item-skills, wired further down) — only the
  // close button and outside-click/Escape handling stay here.
  qs("#skills-close").addEventListener("click", closeSkills);
  skillsOverlay.addEventListener("click", (e) => { if (e.target === skillsOverlay) closeSkills(); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !skillsOverlay.hidden) closeSkills();
  });

  // Same story as #btn-skills above: #btn-debug is gone, opening Debug goes
  // through the panel-menu's #menu-item-debug instead.
  qs("#debug-close").addEventListener("click", closeDebug);
  debugOverlay.addEventListener("click", (e) => { if (e.target === debugOverlay) closeDebug(); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !debugOverlay.hidden) closeDebug();
  });

  // ===========================================================================
  // Logs overlay — conversation-scoped raw traffic between the model and
  // this backend (see logs.py). Left: every conversation that has at least
  // one logged entry. Middle: that conversation's entries (organized or raw
  // JSON), with refresh/clear. Right: settings, left empty for now.
  // ===========================================================================

  const logsOverlay = qs("#logs-overlay");
  const logsStatusLine = qs("#logs-status-line");
  const logsConvoList = qs("#logs-convo-list");
  const logsConvoCount = qs("#logs-convo-count");
  const logsEntriesEl = qs("#logs-entries");
  const logsEntriesTitle = qs("#logs-entries-title");
  const logsEntryCount = qs("#logs-entry-count");
  const btnLogsRefresh = qs("#btn-logs-refresh");
  const btnLogsClear = qs("#btn-logs-clear");

  function logsFilteredConvos() {
    const q = state.logsSearch.trim().toLowerCase();
    if (!q) return state.logsConvos;
    return state.logsConvos.filter((c) =>
      (c.title || "").toLowerCase().includes(q) || c.id.toLowerCase().includes(q)
    );
  }

  // Origin/source labels. Kept as one table so a new origin only has to be
  // named once — the conversation list, the entry list and the search
  // results all render through here.
  const ORIGIN_LABELS = {
    discord: { text: "Discord", cls: "is-discord" },
    instagram: { text: "Instagram", cls: "is-instagram" },
    scheduler: { text: "Scheduled", cls: "is-scheduler" },
  };

  function originBadge(origin) {
    const meta = ORIGIN_LABELS[origin];
    if (!meta) return null;   // "" = a normal, user-typed conversation
    return el("span", { class: "origin-badge " + meta.cls }, meta.text);
  }

  function renderLogsConvoList() {
    const items = logsFilteredConvos();
    logsConvoCount.textContent = `${state.logsConvos.length}`;
    logsConvoList.innerHTML = "";
    if (!items.length) {
      logsConvoList.appendChild(el("div", { class: "debug-empty" },
        state.logsConvos.length ? "No conversations match your search." : "No logs yet — logs are written as soon as you ask Jarvis something."));
      return;
    }
    for (const c of items) {
      const when = (c.updated_at || "").slice(0, 19).replace("T", " ");
      const titleRow = [
        el("span", { class: "logs-convo-card__name" + (c.exists ? "" : " is-deleted") },
           c.title || c.id),
      ];
      // A conversation the Discord bot or a scheduled job created looks
      // identical to one the user typed once it's just exchanges on disk.
      // The badge is the only thing that says otherwise, which is why it
      // sits on the title row rather than buried in the meta line.
      const badge = originBadge(c.origin);
      if (badge) titleRow.push(badge);
      const card = el("div", {
        class: "logs-convo-card" + (c.id === state.logsSelected ? " is-active" : ""),
        onclick: () => logsSelectConvo(c.id),
        title: c.origin_detail || "",
      }, [
        el("div", { class: "logs-convo-card__title" }, titleRow),
        el("div", { class: "logs-convo-card__meta" }, when ? `${when}  ·  ${c.id}` : c.id),
      ]);
      logsConvoList.appendChild(card);
    }
  }

  function logEntryDirClass(direction) {
    return `log-entry__dir--${(direction || "info").replace(/[^a-z_]/g, "")}`;
  }

  // Token count shown right next to the provider (key x/y) badge — see
  // logs.py (each entry's `data`). "usage" entries carry real,
  // provider-reported counts (token_usage.extract_usage); "tool_call" /
  // "tool_result" entries carry local ~estimates (token_usage.estimate_
  // tokens_for), since no provider reports per-tool-call token counts on
  // its own. Other directions (request/response/error/info) don't carry a
  // count of their own, so no badge is shown for them.
  function logEntryTokenLabel(entry) {
    const data = entry.data;
    if (!data || typeof data !== "object") return null;
    if (entry.direction === "usage") {
      const total = (data.input_tokens || 0) + (data.output_tokens || 0);
      return `${total} tok (in=${data.input_tokens || 0} out=${data.output_tokens || 0})`;
    }
    if (entry.direction === "tool_call") {
      return `~${data.input_tokens || 0} tok in`;
    }
    if (entry.direction === "tool_result") {
      return `~${data.output_tokens || 0} tok out`;
    }
    return null;
  }

  function renderLogsEntries() {
    renderLogsSettings();
    logsEntriesEl.innerHTML = "";
    logsEntryCount.textContent = state.logsSelected ? `${state.logsEntries.length}` : "";
    if (!state.logsSelected) {
      logsEntriesEl.appendChild(el("div", { class: "debug-empty" }, "Select a conversation on the left."));
      return;
    }
    if (!state.logsEntries.length) {
      logsEntriesEl.appendChild(el("div", { class: "debug-empty" }, "(empty)"));
      return;
    }
    if (state.logsViewMode === "raw") {
      logsEntriesEl.appendChild(el("pre", { class: "log-entry__body" }, JSON.stringify(state.logsEntries, null, 2)));
      return;
    }
    for (const entry of state.logsEntries) {
      const tokenLabel = logEntryTokenLabel(entry);
      const head = el("div", { class: "log-entry__head" }, [
        el("span", { class: "log-entry__ts" }, (entry.ts || "").replace("T", " ").replace("Z", "") || "?"),
        el("span", { class: `log-entry__dir ${logEntryDirClass(entry.direction)}` }, entry.direction || "?"),
        entry.provider ? el("span", { class: "log-entry__provider" }, entry.provider) : null,
        tokenLabel ? el("span", { class: "log-entry__tokens" }, tokenLabel) : null,
        entry.round != null ? el("span", { class: "log-entry__round" }, `round ${entry.round}`) : null,
      ]);
      const body = el("pre", { class: "log-entry__body" }, JSON.stringify(entry.data, null, 2));
      logsEntriesEl.appendChild(el("div", { class: "log-entry" }, [head, body]));
    }
  }

  // ---- Logs overlay's Settings pane: per-API-key breakdown -----------------
  // Groups the same entries renderLogsEntries() just rendered by their
  // `provider` field (ai_client.py's key_label, e.g. "Anthropic (key 1/2)"
  // or just "Anthropic" when a provider only has one key configured) and
  // pulls out, per key: the capacity mode it ran under (from the "info"
  // entry ai_client.py logs once per attempt), total tokens (summed from
  // "usage" entries — see logs.py), and any console dump lines (from the
  // "info" entry logged alongside a successful reply's split-out console
  // block). Purely a read of what's already logged — nothing here changes
  // what gets written.
  const KEY_LABEL_RE = /^(.*) \(key (\d+)\/(\d+)\)$/;

  function summarizeLogsByKey(entries) {
    const order = [];
    const byLabel = new Map();
    for (const entry of entries) {
      const label = entry.provider || "(unknown)";
      if (!byLabel.has(label)) {
        byLabel.set(label, {
          label, capacityMode: null, capacityLabel: null,
          inputTokens: 0, outputTokens: 0, consoleDump: [], toolTrace: [],
        });
        order.push(label);
      }
      const g = byLabel.get(label);
      const data = entry.data;
      if (!data || typeof data !== "object") continue;
      if (entry.direction === "info") {
        if (data.capacity_mode) {
          g.capacityMode = data.capacity_mode;
          g.capacityLabel = data.capacity_label || data.capacity_mode;
        }
        if (Array.isArray(data.console_dump) && data.console_dump.length) {
          g.consoleDump = g.consoleDump.concat(data.console_dump);
        }
      } else if (entry.direction === "usage") {
        g.inputTokens += data.input_tokens || 0;
        g.outputTokens += data.output_tokens || 0;
      } else if (entry.direction === "tool_call") {
        // Synthesized fallback for when nothing ever hit the real
        // console_dump split (see summarizeLogsByKey's caller) — a raw
        // "here's what this key actually did" trace built straight from
        // the tool_call/tool_result entries ai_providers.py already logs
        // for every tool run, same shape logEntryTokenLabel already reads
        // elsewhere in this file.
        const args = (() => { try { return JSON.stringify(data.arguments); } catch { return String(data.arguments); } })();
        g.toolTrace.push(`→ called ${data.name || "?"}(${args ?? ""}) — ~${data.input_tokens || 0} tok in`);
      } else if (entry.direction === "tool_result") {
        g.toolTrace.push(`← ${data.name || "?"} result — ~${data.output_tokens || 0} tok out`);
      }
    }
    return order.map((label) => byLabel.get(label));
  }

  function renderLogsSettings() {
    const container = qs("#logs-settings-body");
    if (!container) return;
    container.innerHTML = "";
    if (!state.logsSelected) {
      container.appendChild(el("div", { class: "debug-empty" }, "Select a conversation on the left."));
      return;
    }
    if (!state.logsEntries.length) {
      container.appendChild(el("div", { class: "debug-empty" }, "(empty)"));
      return;
    }
    const groups = summarizeLogsByKey(state.logsEntries);
    if (!groups.length) {
      container.appendChild(el("div", { class: "debug-empty" }, "Nothing to show for this conversation yet."));
      return;
    }
    groups.forEach((g, idx) => {
      const m = KEY_LABEL_RE.exec(g.label);
      const provider = m ? m[1] : g.label;
      const keyNum = m ? m[2] : "1";
      const keyTotal = m ? m[3] : null;
      const totalTokens = g.inputTokens + g.outputTokens;

      const modeChip = g.capacityMode
        ? el("span", { class: "mode-chip", "data-mode": g.capacityMode }, [
            el("span", { class: "mode-chip__dot" }),
            g.capacityLabel || g.capacityMode,
          ])
        : el("span", { class: "mode-chip mode-chip--unknown" }, "unknown");

      const summary = el("summary", { class: "logs-key-card__summary" }, [
        el("span", { class: "logs-key-card__caret" }),
        el("span", { class: "logs-key-card__title" },
          `API key ${keyNum}${keyTotal ? `/${keyTotal}` : ""} — ${provider}`),
        modeChip,
        el("span", { class: "logs-key-card__tokens" }, `${totalTokens} tok`),
      ]);

      const bodyRows = [
        el("div", { class: "logs-key-row" }, [
          el("span", { class: "logs-key-row__label" }, "Capacity"),
          modeChip.cloneNode(true),
        ]),
        el("div", { class: "logs-key-row" }, [
          el("span", { class: "logs-key-row__label" }, "API key"),
          el("span", { class: "logs-key-row__value" }, `${provider} — key ${keyNum}${keyTotal ? ` of ${keyTotal}` : ""}`),
        ]),
        el("div", { class: "logs-key-row" }, [
          el("span", { class: "logs-key-row__label" }, "Tokens"),
          el("span", { class: "logs-key-row__value" }, `${totalTokens} tok (in=${g.inputTokens} out=${g.outputTokens})`),
        ]),
      ];
      const consoleBlock = g.consoleDump.length
        ? el("pre", { class: "logs-key-console" }, g.consoleDump.join("\n"))
        : g.toolTrace.length
        ? el("pre", { class: "logs-key-console logs-key-console--synthesized" }, g.toolTrace.join("\n"))
        : el("div", { class: "debug-empty" }, "No console output or tool activity logged for this key.");
      bodyRows.push(el("div", { class: "logs-key-row" }, [
        el("span", { class: "logs-key-row__label" }, "Console"),
        !g.consoleDump.length && g.toolTrace.length
          ? el("span", { class: "logs-key-row__hint" }, "(synthesized from tool calls — no raw dump for this key)")
          : null,
      ]));
      bodyRows.push(consoleBlock);

      const details = el("details", { class: "logs-key-card" }, [summary, el("div", { class: "logs-key-card__body" }, bodyRows)]);
      if (idx === 0) details.open = true;
      container.appendChild(details);
    });
  }

  qs("#logs-view-toggle").addEventListener("click", (e) => {
    const btn = e.target.closest(".debug-toggle-btn");
    if (!btn) return;
    state.logsViewMode = btn.dataset.mode;
    qsa(".debug-toggle-btn", qs("#logs-view-toggle")).forEach((b) => b.classList.toggle("is-active", b === btn));
    renderLogsEntries();
  });

  // Logs overlay fetch cap. Was hardcoded to 50 — fine for the old raw
  // "skim recent traffic" view, but the per-API-key Settings pane (see
  // renderLogsSettings) needs the *whole* conversation's entries to
  // aggregate capacity mode / tokens / console dump per key, and a single
  // tool-heavy turn alone can easily emit 50+ entries (a request/response
  // pair plus a tool_call/tool_result pair per tool round-trip), pushing
  // that turn's own "info" entries out of a 50-entry window before the
  // Settings pane ever sees them. Bumped way up so a conversation's full
  // history is actually available; each line is separately capped (see
  // logs.py's MAX_LINE_CHARS) so this stays bounded.
  const LOGS_FETCH_LIMIT = 5000;

  async function logsLoadEntries(convId) {
    logsStatusLine.textContent = "loading entries…";
    logsStatusLine.classList.add("is-busy");
    logsStatusLine.classList.remove("is-error", "is-ok");
    try {
      const res = await Api.getLog(convId, LOGS_FETCH_LIMIT);
      state.logsEntries = (res && res.entries) || [];
      logsStatusLine.textContent = `${state.logsEntries.length} log line(s) for ${convId}`;
      logsStatusLine.classList.add("is-ok");
    } catch (e) {
      state.logsEntries = [];
      logsStatusLine.textContent = `couldn't load log: ${e.message}`;
      logsStatusLine.classList.add("is-error");
    } finally {
      logsStatusLine.classList.remove("is-busy");
      renderLogsEntries();
    }
  }

  function logsSelectConvo(id) {
    state.logsSelected = id;
    state.logsEntries = [];
    logsEntriesTitle.textContent = "Log";
    updateLogsConvoIdTag(id);
    const convo = state.logsConvos.find((c) => c.id === id);
    if (convo) logsEntriesTitle.textContent = convo.title || id;
    btnLogsRefresh.disabled = false;
    btnLogsClear.disabled = false;
    renderLogsConvoList();
    renderLogsEntries();
    logsLoadEntries(id);
  }

  // Deep search. The existing input filtered conversation TITLES only,
  // which is useless for "which ask produced that error" — the thing you
  // actually open this panel for. Enter runs a full-text search across the
  // log JSON via /api/logs-search; typing still does the instant local
  // title filter, so the cheap case stays instant.
  const logsSearchInput = qs("#logs-convo-search");

  async function runDeepLogSearch(query) {
    const list = qs("#logs-convo-list");
    list.innerHTML = "";
    list.appendChild(el("div", { class: "debug-empty" }, "Searching…"));
    let data;
    try {
      const params = new URLSearchParams({ q: query, mode: state.logsSearchMode || "words" });
      const direction = qs("#logs-filter-direction")?.value;
      if (direction) params.append("direction", direction);
      const origin = (qs("#logs-filter-origin")?.value || "").trim();
      if (origin) params.append("origin", origin);
      const source = (qs("#logs-filter-source")?.value || "").trim();
      if (source) params.append("source", source);
      const res = await fetch(`/api/logs-search?${params}`);
      data = await res.json();
      if (!res.ok || data.error) throw new Error(data.error || "search failed");
    } catch (err) {
      list.innerHTML = "";
      list.appendChild(el("div", { class: "debug-empty" }, `Search failed: ${err.message}`));
      return;
    }
    list.innerHTML = "";
    const results = data.results || [];
    if (!results.length) {
      list.appendChild(el("div", { class: "debug-empty" },
        `No log entries match "${query}".`));
      return;
    }
    list.appendChild(el("div", { class: "logs-search-summary" },
      `${results.length}${data.truncated ? "+" : ""} matches in ${data.scanned} entries`));
    for (const r of results) {
      const head = [el("span", { class: "logs-result__dir" }, r.direction || "?")];
      const badge = originBadge(r.source || r.origin);
      if (badge) head.push(badge);
      head.push(el("span", { class: "logs-result__title" }, r.title || r.conv_id));
      list.appendChild(el("div", {
        class: "logs-convo-card logs-result",
        onclick: () => { logsSelectConvo(r.conv_id); },
      }, [
        el("div", { class: "logs-convo-card__title" }, head),
        el("div", { class: "logs-result__snippet" }, r.snippet || ""),
        el("div", { class: "logs-convo-card__meta" },
           `${(r.ts || "").slice(0, 19).replace("T", " ")}  ·  ${r.provider || ""}`),
      ]));
    }
  }

  logsSearchInput.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    const query = e.target.value.trim();
    if (query) runDeepLogSearch(query);
    else renderLogsConvoList();
  });

  logsSearchInput.addEventListener("input", (e) => {
    state.logsSearch = e.target.value;
    renderLogsConvoList();
  });

  // Changing a filter re-runs the deep search immediately if one is active
  // (the search box holds a query, not just the instant local title
  // filter) — matching a change of "mode" doing the same elsewhere.
  for (const id of ["logs-filter-direction", "logs-filter-origin", "logs-filter-source"]) {
    qs(`#${id}`)?.addEventListener("change", () => {
      const query = logsSearchInput.value.trim();
      if (query) runDeepLogSearch(query);
    });
  }

  btnLogsRefresh.addEventListener("click", () => {
    if (state.logsSelected) logsLoadEntries(state.logsSelected);
  });

  btnLogsClear.addEventListener("click", async () => {
    if (!state.logsSelected) return;
    const id = state.logsSelected;
    if (!confirm(`Clear the log for "${logsEntriesTitle.textContent}"? This can't be undone.`)) return;
    btnLogsClear.disabled = true;
    try {
      await Api.clearLog(id);
      state.logsEntries = [];
      state.logsConvos = state.logsConvos.filter((c) => c.id !== id);
      state.logsSelected = null;
      logsEntriesTitle.textContent = "Log";
      btnLogsRefresh.disabled = true;
      renderLogsConvoList();
      renderLogsEntries();
      toast("Log cleared.", "info");
    } catch (e) {
      toast(`Couldn't clear log: ${e.message}`);
      btnLogsClear.disabled = false;
    }
  });

  async function openLogs() {
    logsOverlay.hidden = false;
    if (state.logsLoaded) { renderLogsConvoList(); renderLogsEntries(); return; }
    logsStatusLine.textContent = "reading conversations…";
    logsStatusLine.classList.add("is-busy");
    try {
      const items = await Api.listLogs();
      state.logsConvos = Array.isArray(items) ? items : [];
      state.logsLoaded = true;
      logsStatusLine.textContent = `${state.logsConvos.length} conversation(s) with logs`;
    } catch (e) {
      logsStatusLine.textContent = `couldn't load logs: ${e.message}`;
      logsStatusLine.classList.add("is-error");
    } finally {
      logsStatusLine.classList.remove("is-busy");
      renderLogsConvoList();
      renderLogsEntries();
    }
  }

  function closeLogs() {
    logsOverlay.hidden = true;
  }

  qs("#btn-logs-fab").addEventListener("click", openLogs);
  qs("#logs-close").addEventListener("click", closeLogs);
  logsOverlay.addEventListener("click", (e) => { if (e.target === logsOverlay) closeLogs(); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !logsOverlay.hidden) closeLogs();
  });


  // ===========================================================================
  // Conversations — every chat with at least one message is saved (see
  // jarvis-cli's conversations.py), switchable and searchable from the
  // sidebar inside the Ask panel. Opening the page always starts a fresh
  // one; switching to an older one restores it with full context. A
  // conversation that never gets a message — opened and abandoned, e.g.
  // by just loading the page or clicking "+ New" and never sending
  // anything — is never written into that history in the first place.
  // ===========================================================================

  const convoListEl = qs("#convo-list");

  // A plain, already-finished reply bubble — used to replay a saved
  // conversation's history, as opposed to addJarvisBubblePending() +
  // appendAskReplyLine()/finalizeAskBubble(), which animate a live one in.
  function addJarvisStaticBubble(text) {
    clearAskEmptyHint();
    const msg = el("div", { class: "ask-msg ask-msg--jarvis" }, [
      el("div", { class: "ask-msg__role" }, currentAssistantName()),
      el("div", { class: "ask-msg__bubble" }),
    ]);
    msg.dataset.raw = text || "";
    const bubbleEl = qs(".ask-msg__bubble", msg);
    bubbleEl.innerHTML = renderMarkdown(text || "");
    linkifyPaths(bubbleEl);
    renderMathIn(bubbleEl);
    addAskMsgActions(msg);
    askThread.appendChild(msg);
    return msg;
  }

  // The server now saves each turn's screenshots/downloads/organize_json
  // results/confirm decisions alongside it (see conversations.append_exchange
  // and ai_client._extras_from_runs) — this seeds state.threadExtrasByConv
  // from that saved data the first time a conversation is loaded in this
  // tab, so replay works after a genuine page reload and not just when
  // switching between conversations within the same session. Once seeded,
  // in-memory items pushed during a live ask (pushThreadExtra) take over
  // for the rest of the tab's lifetime.
  function seedThreadExtrasFromRecord(record, convId) {
    if (convId == null || state.threadExtrasByConv[convId]) return;
    const extras = [];
    (record.exchanges || []).forEach((ex, bucket) => {
      for (const e of ex.extras || []) {
        if (e && e.type) extras.push({ bucket, type: e.type, data: e.data || {} });
      }
    });
    state.threadExtrasByConv[convId] = extras;
  }

  function loadConversationIntoThread(record, convId) {
    seedThreadExtrasFromRecord(record, convId);
    askThread.innerHTML = "";
    const exchanges = (record && record.exchanges) || [];
    const extras = (convId != null && state.threadExtrasByConv[convId]) || [];
    const renderExtrasForBucket = (bucket) => {
      for (const item of extras) {
        if (item.bucket === bucket) renderThreadExtra(item);
      }
    };
    if (!exchanges.length) {
      renderExtrasForBucket(0);
      if (!askThread.children.length) {
        askThread.appendChild(el("div", { class: "ask-empty" }, "Ask about anything, or tell me what you need done, sir."));
      }
    } else {
      exchanges.forEach((ex, i) => {
        addUserBubble(ex.user || "");
        addJarvisStaticBubble(ex.jarvis || "");
        renderExtrasForBucket(i);
      });
      // Extras for the turn currently in flight (or one that failed after
      // producing media but before finishing) live past the last saved
      // exchange, at bucket === exchanges.length.
      renderExtrasForBucket(exchanges.length);
    }
    askThreadScrollToEnd();
    loadConsoleHistoryForConv(convId);
  }

  // Repopulates the "Live output" console panel (#console) from this
  // conversation's persisted "command_run" log entries (see cli.py's
  // logs-append-run and server.js's "run" websocket handler) — without
  // this, a directly-run command's console output only ever lived in this
  // tab's live websocket stream and vanished the instant the page reloaded
  // or another conversation was selected. Best-effort and silent: a brand
  // new conversation with no runs yet legitimately 404s (no log file at
  // all), which is not a failure worth surfacing.
  //
  // Skipped while something is actively running (state.running covers both
  // an in-flight ask and an in-flight run) so a switch mid-run can't wipe
  // out the live output the user is currently watching — that run's own
  // exit will persist it, and the next conversation load will pick it up.
  async function loadConsoleHistoryForConv(convId) {
    const consoleEl = qs("#console");
    if (!consoleEl || state.running) return;
    let log = null;
    if (convId != null) {
      try {
        log = await Api.getLog(convId, 500);
      } catch {
        log = null; // no log file yet for this conversation — that's fine
      }
    }
    // The conversation may have been switched again while this was in
    // flight; only paint if we're still looking at the conversation this
    // history belongs to.
    if (convId !== state.activeConversationId) return;
    const runs = ((log && log.entries) || []).filter((e) => e && e.direction === "command_run");
    consoleEl.innerHTML = "";
    if (!runs.length) {
      consoleEl.appendChild(el("div", { class: "console__idle" }, "Awaiting instructions."));
      return;
    }
    runs.forEach((entry, i) => {
      const d = entry.data || {};
      if (i > 0) consoleAppend("\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500", "sys");
      consoleAppend(d.cmdline || "(command)", "cmd");
      (Array.isArray(d.lines) ? d.lines : []).forEach((l) => {
        consoleAppend((l && l.text) || "", (l && l.stream === "err") ? "err" : "out");
      });
      if (d.signal) {
        consoleAppend(`\u25a0 stopped (${d.signal})`, "exit-bad");
      } else if (d.exit_code === 0) {
        consoleAppend("\u25a0 done \u2014 exit code 0", "exit-ok");
      } else if (d.exit_code != null) {
        consoleAppend(`\u25a0 exit code ${d.exit_code}`, "exit-bad");
      }
    });
  }

  function convoFiltered() {
    const q = state.convoSearch.trim().toLowerCase();
    if (!q) return state.conversations;
    return state.conversations.filter((c) =>
      (c.title || "").toLowerCase().includes(q) || (c.soft_context || "").toLowerCase().includes(q)
    );
  }

  function renderConvoList() {
    const items = convoFiltered();
    convoListEl.innerHTML = "";
    if (!items.length) {
      convoListEl.appendChild(el("div", { class: "ask-convos__empty" },
        state.conversations.length ? "No chats match your search." : "No conversations yet."));
      return;
    }
    for (const convo of items) {
      const gist = (convo.soft_context || "").trim();
      const card = el("div", {
        class: "convo-card" + (convo.id === state.activeConversationId ? " is-active" : ""),
        onclick: () => selectConversation(convo.id),
      }, [
        el("div", { class: "convo-card__title" }, convo.title || "New Conversation"),
        gist
          ? el("div", { class: "convo-card__gist" }, gist)
          : el("div", { class: "convo-card__empty-gist" }, convo.exchange_count ? "\u2026" : "No messages yet"),
        el("button", {
          type: "button", class: "convo-card__del", title: "Delete this conversation",
          onclick: (e) => { e.stopPropagation(); deleteConversationConfirm(convo.id); },
        }, "\u00d7"),
      ]);
      convoListEl.appendChild(card);
    }
  }

  async function refreshConvoList() {
    // A brand-new conversation isn't written into the server-side index
    // until it has a first real exchange (see conversations.py's
    // new_conversation/append_exchange) — specifically so an "opened but
    // never used" conversation doesn't clutter conversation history.
    // That means if we're currently sitting in one, the server plainly
    // won't include it in this fetch. Hang onto it locally so it doesn't
    // vanish out of the sidebar mid-session just because something else
    // triggered a list refresh — it only actually disappears (as
    // intended) once the session ends without ever sending a message.
    const activeIfStillEmpty = state.conversations.find((c) => c.id === state.activeConversationId);
    try {
      state.conversations = await Api.listConversations(state.convoSearch);
    } catch (e) {
      convoListEl.innerHTML = "";
      convoListEl.appendChild(el("div", { class: "ask-convos__empty" }, `Couldn't load chats: ${e.message}`));
      return;
    }
    if (
      activeIfStillEmpty &&
      !state.conversations.some((c) => c.id === state.activeConversationId)
    ) {
      state.conversations.unshift(activeIfStillEmpty);
    }
    renderConvoList();
  }

  async function selectConversation(id) {
    if (id === state.activeConversationId) return;
    // Switching is always allowed, even mid-reply: if an ask is still in
    // flight for the conversation we're leaving, it keeps running in the
    // background and saves normally — we just stop painting it into a
    // thread that isn't showing it anymore (see isViewingAskThread()).
    if (state.running && state.askConversationId && state.askConversationId === state.activeConversationId) {
      toast("Still replying in the other chat \u2014 it'll be saved there.", "info");
    }
    let record;
    try {
      record = await Api.getConversation(id);
    } catch (e) {
      toast(`Couldn't load that conversation: ${e.message}`);
      return;
    }
    state.activeConversationId = id;
    updateConvoIdTag(id);
    state.exchangeCountByConv[id] = (record.exchanges || []).length;
    loadConversationIntoThread(record, id);
    // If this conversation's ask is still running in the background, the
    // thread rebuild above just destroyed the old pending-bubble DOM node
    // (see insertIntoAskThread) — make a fresh one and repaint whatever's
    // accumulated so far, so live updates have somewhere valid to land.
    if (state.running && state.askConversationId === id) {
      state.askPendingBubble = addJarvisBubblePending();
      rerenderAskPendingBubble();
      setAskStatus("thinking\u2026", "busy");
    }
    renderAskTraceForConv(id);
    refreshAskBusyUI();
    const pending = state.pendingConfirmByConv[id];
    if (pending) {
      addAskConfirmBubble(pending.tool, pending.arguments, pending.risk_note, id, pending.extraItem);
      setAskStatus("waiting for your confirmation\u2026", "busy");
    }
    renderConvoList();
  }

  // Used both by the "+ New" button and automatically once on page load
  // (see initApp) — every fresh page load starts a brand-new conversation,
  // while older ones stay saved and reachable from the sidebar.
  async function startNewConversation({ select = true } = {}) {
    let record;
    try {
      record = await Api.createConversation();
    } catch (e) {
      toast(`Couldn't start a new conversation: ${e.message}`);
      return null;
    }
    state.conversations.unshift(record);
    if (select) {
      state.activeConversationId = record.id;
      updateConvoIdTag(record.id);
      state.exchangeCountByConv[record.id] = 0;
      loadConversationIntoThread({ exchanges: [] }, record.id);
      askPromptReset();
      refreshAskBusyUI();
    }
    renderConvoList();
    return record.id;
  }

  async function deleteConversationConfirm(id) {
    if (!confirm("Delete this conversation? This can't be undone.")) return;
    try {
      await Api.deleteConversation(id);
    } catch (e) {
      toast(`Couldn't delete: ${e.message}`);
      return;
    }
    delete state.askTraceByConv[id];
    delete state.pendingConfirmByConv[id];
    delete state.threadExtrasByConv[id];
    delete state.exchangeCountByConv[id];
    state.conversations = state.conversations.filter((c) => c.id !== id);
    if (id === state.activeConversationId) {
      // Land somewhere sane: the next most recent chat, or a brand-new one.
      if (state.conversations.length) {
        await selectConversation(state.conversations[0].id);
      } else {
        await startNewConversation();
      }
    } else {
      renderConvoList();
    }
    toast("Conversation deleted.", "info");
  }

  qs("#btn-convo-new").addEventListener("click", () => startNewConversation());
  qs("#convo-search").addEventListener("input", (e) => {
    state.convoSearch = e.target.value;
    refreshConvoList();
  });

  // ===========================================================================
  // Command builder modal
  // ===========================================================================

  const backdrop = qs("#modal-backdrop");

  function openBuilder(mode, name) {
    state.editingOriginalName = mode === "edit" ? name : null;
    qs("#modal-title").textContent = mode === "edit" ? `Edit \u201c${name}\u201d` : "New Command";
    qs("#modal-error").textContent = "";
    qs("#raw-json-error").textContent = "";
    setBuilderTab("builder");

    const spec = mode === "edit" ? state.commands[name] : { description: "", run: [""], vars: {} };
    qs("#f-name").value = mode === "edit" ? name : "";
    qs("#f-desc").value = spec.description || "";
    qs("#f-confirm-required").checked = !!spec.confirm_required;
    qs("#f-ai-review").checked = !!spec.ai_review;

    buildVarsEditor(spec.vars || {});
    buildStepsEditor(normalizeSteps(spec.run));
    refreshVarSync();
    syncBuilderToRaw();

    backdrop.hidden = false;
    qs("#f-name").focus();
  }

  function closeBuilder() { backdrop.hidden = true; }

  qs("#cmd-search").addEventListener("input", (e) => {
    state.cmdSearch = e.target.value;
    renderCommandList();
  });

  qs("#btn-new-command").addEventListener("click", () => openBuilder("new"));
  qs("#modal-close").addEventListener("click", closeBuilder);
  qs("#btn-cancel-modal").addEventListener("click", closeBuilder);
  backdrop.addEventListener("click", (e) => { if (e.target === backdrop) closeBuilder(); });

  // ===========================================================================
  // Settings modal — every *.json file in the jarvis config dir, auto-
  // discovered from GET /api/config/list (see server.js's KNOWN_CONFIGS /
  // generic config-file browser). No hardcoded tab list: drop a new *.json
  // file in that directory and it shows up here with no HTML/JS change.
  // Each tab gets an editable collapsible tree (buildJsonTree, editable:true)
  // plus a Raw JSON toggle for bulk/paste edits — mirroring the organize-json
  // chat bubble's Organized/Raw split, just editable here.
  // ===========================================================================

  const settingsBackdrop = qs("#settings-backdrop");
  const settingsTabsEl = qs("#settings-tabs");
  const settingsBodyEl = qs("#settings-body");

  let settingsFiles = [];        // [{name, label, hint, path}] from /api/config/list
  let settingsActiveTab = null;  // active file name, e.g. "commands.json"
  const settingsFileState = {};  // name -> {path, text, data, parseError, mode, dirty}

  function settingsFileMeta(name) {
    return settingsFiles.find((f) => f.name === name);
  }

  function settingsPaneEl(name) {
    return qs(`.settings-pane[data-settings-pane="${name}"]`, settingsBodyEl);
  }

  function clearSettingsErrors() {
    qs("#settings-error-global").textContent = "";
    qsa(".settings-pane__error", settingsBodyEl).forEach((e) => { e.textContent = ""; });
  }

  function buildSettingsTabs() {
    settingsTabsEl.innerHTML = "";
    settingsFiles.forEach((f) => {
      settingsTabsEl.appendChild(el("button", {
        type: "button",
        class: "tab-btn settings-tab-btn",
        "data-settings-tab": f.name,
        title: f.hint || "",
        onclick: () => selectSettingsTab(f.name),
      }, f.label || f.name));
    });
    updateSettingsTabsFade();
  }

  // Keeps the scrollable tab strip's edge fades honest: only fades the
  // side(s) that actually have more tabs hidden off-screen, so a short
  // list (nothing to scroll) shows no fade at all, and a strip scrolled
  // all the way to one end doesn't fade the end it's already at.
  const SETTINGS_TABS_FADE_PX = 18;
  function updateSettingsTabsFade() {
    const maxScroll = settingsTabsEl.scrollWidth - settingsTabsEl.clientWidth;
    if (maxScroll <= 1) {
      settingsTabsEl.style.setProperty("--settings-tabs-fade-l", "0px");
      settingsTabsEl.style.setProperty("--settings-tabs-fade-r", "0px");
      return;
    }
    const atStart = settingsTabsEl.scrollLeft <= 1;
    const atEnd = settingsTabsEl.scrollLeft >= maxScroll - 1;
    settingsTabsEl.style.setProperty("--settings-tabs-fade-l", atStart ? "0px" : `${SETTINGS_TABS_FADE_PX}px`);
    settingsTabsEl.style.setProperty("--settings-tabs-fade-r", atEnd ? "0px" : `${SETTINGS_TABS_FADE_PX}px`);
  }
  settingsTabsEl.addEventListener("scroll", updateSettingsTabsFade, { passive: true });
  window.addEventListener("resize", () => {
    if (!settingsBackdrop.hidden) updateSettingsTabsFade();
  });
  // The strip only scrolls horizontally, so a plain vertical mouse-wheel
  // (no shift held) would otherwise do nothing over it — translate it to
  // horizontal scroll, same convention as most horizontal tab/chip rows.
  settingsTabsEl.addEventListener("wheel", (e) => {
    if (Math.abs(e.deltaY) <= Math.abs(e.deltaX)) return;
    if (settingsTabsEl.scrollWidth <= settingsTabsEl.clientWidth) return;
    e.preventDefault();
    settingsTabsEl.scrollLeft += e.deltaY;
  }, { passive: false });

  function buildSettingsPanes() {
    settingsBodyEl.innerHTML = "";
    settingsFiles.forEach((f) => {
      const btnOrganized = el("button", {
        type: "button", class: "debug-toggle-btn is-active", "data-view": "organized",
        onclick: () => setSettingsPaneMode(f.name, "organized"),
      }, "Organized");
      const btnRaw = el("button", {
        type: "button", class: "debug-toggle-btn", "data-view": "raw",
        onclick: () => setSettingsPaneMode(f.name, "raw"),
      }, "Raw JSON");

      settingsBodyEl.appendChild(el("div", { class: "settings-pane", "data-settings-pane": f.name }, [
        el("div", { class: "settings-pane__hint" }, f.hint || ""),
        el("div", { class: "raw-config-path settings-pane__path" }, ""),
        el("div", { class: "settings-view-toggle" }, [btnOrganized, btnRaw]),
        el("div", { class: "settings-pane__view" }, [
          el("div", { class: "settings-empty" }, "Loading\u2026"),
        ]),
        el("div", { class: "modal__error settings-pane__error" }),
      ]));
    });
  }

  function setActiveSettingsTabUi(name) {
    qsa(".settings-tab-btn", settingsTabsEl).forEach((b) => {
      const isActive = b.dataset.settingsTab === name;
      b.classList.toggle("is-active", isActive);
      // "nearest" (not "center"/"start") so this never fights the user's
      // own scroll position when the tab is already fully visible — it
      // only moves the strip the minimum needed to bring a newly
      // selected, currently-clipped tab (e.g. picked via openSettings()
      // with an initialName, or a keyboard/programmatic switch) on screen.
      if (isActive) b.scrollIntoView({ block: "nearest", inline: "nearest" });
    });
    qsa(".settings-pane", settingsBodyEl).forEach((p) => {
      p.classList.toggle("is-active", p.dataset.settingsPane === name);
    });
    updateSettingsTabsFade();
  }

  // Renders whichever view (tree or raw textarea) matches the file's
  // current mode. Called after loading a file, after Save/Reload, and
  // whenever the Organized/Raw toggle is flipped.
  function renderSettingsPaneContent(name) {
    const st = settingsFileState[name];
    const pane = settingsPaneEl(name);
    if (!st || !pane) return;
    qsa(".debug-toggle-btn", pane).forEach((b) => b.classList.toggle("is-active", b.dataset.view === st.mode));
    const viewWrap = qs(".settings-pane__view", pane);
    viewWrap.innerHTML = "";

    if (st.mode === "raw") {
      const ta = el("textarea", { class: "settings-json", spellcheck: "false" });
      ta.value = st.text;
      ta.addEventListener("input", () => { st.text = ta.value; st.dirty = true; });
      viewWrap.appendChild(ta);
      return;
    }

    if (st.parseError) {
      viewWrap.appendChild(el("div", { class: "json-org-error" },
        `Can't show a tree \u2014 invalid JSON: ${st.parseError}`));
      return;
    }
    const treeMount = el("div", { class: "json-tree settings-tree" });
    viewWrap.appendChild(treeMount);
    buildJsonTree(treeMount, st.data, {
      editable: true,
      onChange: () => {
        st.text = JSON.stringify(st.data, null, 2) + "\n";
        st.dirty = true;
      },
    });
  }

  function setSettingsPaneMode(name, mode) {
    const st = settingsFileState[name];
    const pane = settingsPaneEl(name);
    if (!st || !pane) return;
    if (mode === "organized" && st.mode !== "organized") {
      // Coming from Raw — re-parse whatever text is sitting there now,
      // since the person may have hand-edited it directly.
      try {
        st.data = JSON.parse(st.text);
        st.parseError = null;
      } catch (e) {
        qs(".settings-pane__error", pane).textContent = `Can't switch to Organized \u2014 invalid JSON: ${e.message}`;
        return;
      }
    }
    qs(".settings-pane__error", pane).textContent = "";
    st.mode = mode;
    renderSettingsPaneContent(name);
  }

  async function selectSettingsTab(name, { force = false } = {}) {
    settingsActiveTab = name;
    setActiveSettingsTabUi(name);
    const pane = settingsPaneEl(name);
    if (!pane) return;
    qs(".settings-pane__error", pane).textContent = "";

    if (!settingsFileState[name] || force) {
      try {
        const data = await Api.getConfigFile(name);
        const st = { path: data.path, text: data.text, mode: "organized", dirty: false };
        try {
          st.data = JSON.parse(st.text);
          st.parseError = null;
        } catch (e) {
          st.data = null;
          st.parseError = e.message;
          st.mode = "raw"; // can't build a tree out of invalid JSON — show the text instead
        }
        settingsFileState[name] = st;
      } catch (e) {
        qs(".settings-pane__error", pane).textContent = e.message;
        return;
      }
    }
    qs(".settings-pane__path", pane).textContent = settingsFileState[name].path;
    renderSettingsPaneContent(name);
  }

  async function openSettings(initialName) {
    clearSettingsErrors();
    settingsBackdrop.hidden = false;
    Object.keys(settingsFileState).forEach((k) => delete settingsFileState[k]);
    try {
      const list = await Api.configList();
      settingsFiles = list.files || [];
    } catch (e) {
      settingsFiles = [];
      toast(e.message);
    }
    buildSettingsTabs();
    buildSettingsPanes();
    const target = (initialName && settingsFiles.some((f) => f.name === initialName))
      ? initialName
      : (settingsFiles[0] && settingsFiles[0].name);
    if (target) {
      await selectSettingsTab(target, { force: true });
    } else {
      settingsBodyEl.appendChild(el("div", { class: "settings-empty" }, "No config files found."));
    }
  }

  function closeSettings() {
    settingsBackdrop.hidden = true;
  }

  // Called from applyCommandsToUi whenever commands.json changes on the
  // server (another tab's edit, the CLI, a live broadcast) — if that tab is
  // open in Settings right now, refresh it in place instead of letting a
  // later Save silently clobber the newer version with stale in-memory state.
  function syncCommandsIntoSettingsTab(commands) {
    const st = settingsFileState["commands.json"];
    if (!st || settingsBackdrop.hidden) return;
    const text = JSON.stringify({ commands }, null, 2) + "\n";
    st.text = text;
    try {
      st.data = JSON.parse(text);
      st.parseError = null;
    } catch { /* JSON.stringify output is always valid JSON */ }
    if (settingsActiveTab === "commands.json") renderSettingsPaneContent("commands.json");
  }

  qs("#btn-settings").addEventListener("click", () => openSettings("commands.json"));
  qs("#settings-close").addEventListener("click", closeSettings);
  qs("#btn-settings-cancel").addEventListener("click", closeSettings);
  settingsBackdrop.addEventListener("click", (e) => { if (e.target === settingsBackdrop) closeSettings(); });

  qs("#btn-settings-reload").addEventListener("click", async () => {
    clearSettingsErrors();
    if (!settingsActiveTab) return;
    try {
      await selectSettingsTab(settingsActiveTab, { force: true });
      toast("Reloaded from disk.", "info");
    } catch (e) {
      toast(e.message);
    }
  });

  qs("#btn-settings-save").addEventListener("click", async () => {
    clearSettingsErrors();
    const name = settingsActiveTab;
    if (!name) return;
    const st = settingsFileState[name];
    const pane = settingsPaneEl(name);
    if (!st || !pane) return;
    const errEl = qs(".settings-pane__error", pane);

    // Organized mode keeps st.text in sync on every tree edit (onChange
    // above); Raw mode's textarea does the same on input — either way
    // st.text is the thing to validate and send.
    try {
      JSON.parse(st.text);
    } catch (e) {
      errEl.textContent = `Invalid JSON: ${e.message}`;
      return;
    }
    try {
      await Api.putConfigFile(name, st.text);
      st.dirty = false;
      if (name === "commands.json") await loadCommands({ silent: true });
      closeSettings();
      const meta = settingsFileMeta(name);
      toast(`${(meta && meta.label) || name} saved.`, "info");
    } catch (e) {
      errEl.textContent = e.message;
    }
  });

  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (!settingsBackdrop.hidden) closeSettings();
    else if (!backdrop.hidden) closeBuilder();
  });

  function setBuilderTab(tab) {
    qsa("#modal-backdrop .tab-btn").forEach((b) => b.classList.toggle("is-active", b.dataset.tab === tab));
    qsa("#modal-backdrop .tab-pane").forEach((p) => p.classList.toggle("is-active", p.dataset.pane === tab));
  }

  qsa("#modal-backdrop .tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const target = btn.dataset.tab;
      if (target === "raw" && !qs('#modal-backdrop .tab-btn[data-tab="builder"]').disabled) {
        syncBuilderToRaw();
      } else if (target === "builder") {
        if (!applyRawToBuilder()) return;
      }
      setBuilderTab(target);
    });
  });

  // --- Vars editor -----------------------------------------------------------

  function buildVarsEditor(varsObj) {
    const holder = qs("#vars-editor");
    holder.innerHTML = "";
    const entries = Object.entries(varsObj || {});
    if (entries.length === 0) addVarRow();
    else entries.forEach(([name, v]) => addVarRow(name, v.default, v.description));
  }

  function addVarRow(name = "", def, desc = "") {
    const tmpl = qs("#tmpl-var").content.cloneNode(true);
    const row = tmpl.querySelector(".var-row");
    row.querySelector(".var-name").value = name;
    row.querySelector(".var-default").value = def == null ? "" : String(def);
    row.querySelector(".var-desc").value = desc || "";
    row.querySelector(".var-remove").addEventListener("click", () => { row.remove(); refreshVarSync(); });
    row.querySelector(".var-name").addEventListener("input", refreshVarSync);
    qs("#vars-editor").appendChild(row);
    refreshVarSync();
  }

  qs("#btn-add-var").addEventListener("click", () => addVarRow());

  function currentVarNames() {
    return qsa("#vars-editor .var-name").map((i) => i.value.trim()).filter(Boolean);
  }

  function refreshVarSync() {
    const names = currentVarNames();
    // condition-row <select> options
    qsa(".cond-var").forEach((sel) => {
      const prev = sel.value;
      sel.innerHTML = "";
      names.forEach((n) => sel.appendChild(el("option", { value: n }, n)));
      if (names.includes(prev)) sel.value = prev;
    });
    // insert-var chips per step
    qsa(".step-card").forEach((card) => {
      let chipRow = qs(".insert-chips", card);
      if (!chipRow) {
        chipRow = el("div", { class: "insert-chips" });
        chipRow.style.cssText = "display:flex;gap:6px;flex-wrap:wrap;margin:-4px 0 10px;";
        card.querySelector(".step-run").closest(".field").after(chipRow);
      }
      chipRow.innerHTML = "";
      names.forEach((n) => {
        const chip = el("button", { type: "button", class: "btn btn--ghost btn--sm" }, `{${n}}`);
        chip.style.padding = "2px 8px";
        chip.addEventListener("click", () => insertAtCursor(card.querySelector(".step-run"), `{${n}}`));
        chipRow.appendChild(chip);
      });
    });
  }

  function insertAtCursor(input, text) {
    const start = input.selectionStart ?? input.value.length;
    const end = input.selectionEnd ?? input.value.length;
    input.value = input.value.slice(0, start) + text + input.value.slice(end);
    input.focus();
    input.selectionStart = input.selectionEnd = start + text.length;
  }

  // --- Steps editor ------------------------------------------------------------

  function normalizeSteps(run) {
    const list = Array.isArray(run) ? run : [run];
    return list.map((s) => (typeof s === "string" ? { run: s } : { ...s }));
  }

  // Shared by both the Builder and Raw JSON tabs so neither path can save a
  // command with a blank step — jarvis would accept it and then fail
  // confusingly (or no-op) the moment someone actually ran it.
  function findSpecError(spec) {
    if (typeof spec !== "object" || spec === null || Array.isArray(spec)) {
      return "Command spec must be an object.";
    }
    if (spec.run === undefined || spec.run === null) {
      return "Command must have a 'run' (string or list of steps).";
    }
    const steps = normalizeSteps(spec.run);
    if (steps.length === 0) return "Add at least one step.";
    const blankIdx = steps.findIndex((s) => typeof s.run !== "string" || s.run.trim() === "");
    if (blankIdx !== -1) return `Step ${blankIdx + 1} needs a command to run.`;
    return null;
  }

  function buildStepsEditor(steps) {
    const holder = qs("#steps-editor");
    holder.innerHTML = "";
    if (steps.length === 0) steps = [{ run: "" }];
    steps.forEach((s) => addStepCard(s));
  }

  // The "parallel with previous" checkbox only makes sense from the second
  // step onward \u2014 there's nothing before the first step to run alongside.
  // Called after anything that can change step order (add, remove, move,
  // drag-drop) so the right card's checkbox is the one hidden.
  function refreshStepParallelVisibility() {
    qsa("#steps-editor .step-card").forEach((card, i) => {
      const wrap = qs(".step-parallel-wrap", card);
      if (!wrap) return;
      wrap.hidden = i === 0;
      if (i === 0) card.querySelector(".step-parallel").checked = false;
    });
  }

  function addStepCard(step = {}) {
    const tmpl = qs("#tmpl-step").content.cloneNode(true);
    const card = tmpl.querySelector(".step-card");

    card.querySelector(".step-name").value = step.name || "";
    card.querySelector(".step-run").value = step.run || "";
    card.querySelector(".step-coe").checked = !!step.continueOnError;
    card.querySelector(".step-parallel").checked = !!step.parallel;
    card.querySelector(".step-show-cmd").checked = step.showCommand !== false;

    card.querySelector(".step-remove").addEventListener("click", () => {
      if (qsa("#steps-editor .step-card").length <= 1) { toast("A command needs at least one step."); return; }
      card.remove();
      refreshStepParallelVisibility();
    });
    card.querySelector(".step-up").addEventListener("click", () => {
      const prev = card.previousElementSibling;
      if (prev) card.parentNode.insertBefore(card, prev);
      refreshStepParallelVisibility();
    });
    card.querySelector(".step-down").addEventListener("click", () => {
      const next = card.nextElementSibling;
      if (next) card.parentNode.insertBefore(next, card);
      refreshStepParallelVisibility();
    });

    setupCondBlock(card, "if", step.if);
    setupCondBlock(card, "unless", step.unless);

    // drag reorder
    card.addEventListener("dragstart", () => card.classList.add("is-dragging"));
    card.addEventListener("dragend", () => { card.classList.remove("is-dragging"); refreshStepParallelVisibility(); });
    card.addEventListener("dragover", (e) => {
      e.preventDefault();
      const dragging = qs(".is-dragging", holderOf(card));
      if (!dragging || dragging === card) return;
      const rect = card.getBoundingClientRect();
      const after = e.clientY - rect.top > rect.height / 2;
      card.parentNode.insertBefore(dragging, after ? card.nextSibling : card);
    });

    qs("#steps-editor").appendChild(card);
    refreshVarSync();
    refreshStepParallelVisibility();
  }

  function holderOf(node) { return node.parentNode; }

  qs("#btn-add-step").addEventListener("click", () => addStepCard());

  function setupCondBlock(card, kind, initialValue) {
    const checkbox = card.querySelector(kind === "if" ? ".step-has-if" : ".step-has-unless");
    const rowsHolder = card.querySelector(kind === "if" ? ".step-card__if-rows" : ".step-card__unless-rows");
    let mode = "simple"; // or "advanced"

    function render() {
      rowsHolder.innerHTML = "";
      const modeBar = el("div", { style: "display:flex;justify-content:space-between;align-items:center;" }, [
        el("span", { class: "cond-group-label" }, kind.toUpperCase()),
        el("button", {
          type: "button", class: "btn btn--ghost btn--sm",
          onclick: () => { mode = mode === "simple" ? "advanced" : "simple"; render(); },
        }, mode === "simple" ? "use expression instead" : "use simple rows instead"),
      ]);
      rowsHolder.appendChild(modeBar);

      if (mode === "simple") {
        rowsHolder.dataset.rows = "";
        const addRow = (varName = "", val = "") => {
          const t = qs("#tmpl-cond-row").content.cloneNode(true);
          const row = t.querySelector(".cond-row");
          const names = currentVarNames();
          names.forEach((n) => row.querySelector(".cond-var").appendChild(el("option", { value: n }, n)));
          if (names.includes(varName)) row.querySelector(".cond-var").value = varName;
          row.querySelector(".cond-val").value = val;
          row.querySelector(".cond-remove").addEventListener("click", () => row.remove());
          rowsHolder.appendChild(row);
        };
        rowsHolder._addRow = addRow;
        rowsHolder._getValue = () => {
          const out = {};
          qsa(".cond-row", rowsHolder).forEach((row) => {
            const k = row.querySelector(".cond-var").value;
            const v = row.querySelector(".cond-val").value;
            if (k) out[k] = v;
          });
          return out;
        };
        const initial = initialValue && typeof initialValue === "object" ? initialValue : {};
        const entries = Object.entries(initial);
        if (entries.length === 0) addRow();
        else entries.forEach(([k, v]) => addRow(k, Array.isArray(v) ? v.join(",") : v));
        rowsHolder.appendChild(el("button", {
          type: "button", class: "btn btn--ghost btn--sm cond-add",
          onclick: () => addRow(),
        }, "+ AND condition"));
      } else {
        const ta = el("input", {
          type: "text", class: "cond-expr",
          placeholder: "env == 'prod' and (region == 'us' or region == 'eu')",
          value: typeof initialValue === "string" ? initialValue : "",
        });
        rowsHolder._getValue = () => ta.value.trim();
        rowsHolder.appendChild(ta);
      }
    }

    checkbox.checked = initialValue != null;
    rowsHolder.hidden = !checkbox.checked;
    if (typeof initialValue === "string") mode = "advanced";
    render();

    checkbox.addEventListener("change", () => { rowsHolder.hidden = !checkbox.checked; });

    card[`_get_${kind}`] = () => {
      if (!checkbox.checked) return undefined;
      const v = rowsHolder._getValue();
      if (mode === "simple" && Object.keys(v).length === 0) return undefined;
      if (mode === "advanced" && !v) return undefined;
      return v;
    };
  }

  // --- Builder <-> spec object -------------------------------------------------

  function collectVarsFromEditor() {
    const vars = {};
    qsa("#vars-editor .var-row").forEach((row) => {
      const name = row.querySelector(".var-name").value.trim();
      if (!name) return;
      const def = row.querySelector(".var-default").value;
      const desc = row.querySelector(".var-desc").value.trim();
      const entry = {};
      if (def !== "") entry.default = def;
      if (desc) entry.description = desc;
      vars[name] = entry;
    });
    return vars;
  }

  function collectStepsFromEditor() {
    return qsa("#steps-editor .step-card").map((card, i) => {
      const step = { run: card.querySelector(".step-run").value };
      const name = card.querySelector(".step-name").value.trim();
      if (name) step.name = name;
      const ifVal = card._get_if && card._get_if();
      const unlessVal = card._get_unless && card._get_unless();
      if (ifVal !== undefined) step.if = ifVal;
      if (unlessVal !== undefined) step.unless = unlessVal;
      if (card.querySelector(".step-coe").checked) step.continueOnError = true;
      // Meaningless (and never shown) on the first step \u2014 nothing precedes
      // it to run alongside \u2014 so it's never written even if the checkbox
      // somehow ended up checked before this card became the first one.
      if (i > 0 && card.querySelector(".step-parallel").checked) step.parallel = true;
      // Default is "shown"; only write the field when it's turned off, so
      // a plain spec with nothing toggled stays exactly as compact as before.
      if (!card.querySelector(".step-show-cmd").checked) step.showCommand = false;
      return step;
    });
  }

  function collectSpecFromBuilder() {
    const description = qs("#f-desc").value.trim();
    const vars = collectVarsFromEditor();
    const steps = collectStepsFromEditor();
    let run;
    if (steps.length === 1 && !steps[0].name && steps[0].if === undefined && steps[0].unless === undefined && !steps[0].continueOnError) {
      run = steps[0].run;
    } else {
      run = steps;
    }
    const spec = { description, run, vars };
    // Only write these when turned on, so a plain command with neither
    // toggled stays exactly as compact as before (mirrors the step-level
    // showCommand pattern above).
    if (qs("#f-confirm-required").checked) spec.confirm_required = true;
    if (qs("#f-ai-review").checked) spec.ai_review = true;
    return spec;
  }

  function syncBuilderToRaw() {
    const spec = collectSpecFromBuilder();
    qs("#raw-json").value = JSON.stringify(spec, null, 2);
    qs("#raw-json-error").textContent = "";
  }

  function applyRawToBuilder() {
    let parsed;
    try {
      parsed = JSON.parse(qs("#raw-json").value);
    } catch (e) {
      qs("#raw-json-error").textContent = `Invalid JSON: ${e.message}`;
      return false;
    }
    if (typeof parsed !== "object" || parsed === null || parsed.run === undefined) {
      qs("#raw-json-error").textContent = "Needs at least a 'run' field.";
      return false;
    }
    qs("#f-desc").value = parsed.description || "";
    qs("#f-confirm-required").checked = !!parsed.confirm_required;
    qs("#f-ai-review").checked = !!parsed.ai_review;
    buildVarsEditor(parsed.vars || {});
    buildStepsEditor(normalizeSteps(parsed.run));
    refreshVarSync();
    qs("#raw-json-error").textContent = "";
    return true;
  }

  qs("#btn-save-command").addEventListener("click", async () => {
    qs("#modal-error").textContent = "";
    let spec;
    if (qs('.tab-pane[data-pane="raw"]').classList.contains("is-active")) {
      try {
        spec = JSON.parse(qs("#raw-json").value);
      } catch (e) {
        qs("#modal-error").textContent = `Invalid JSON: ${e.message}`;
        return;
      }
    } else {
      spec = collectSpecFromBuilder();
    }
    const name = qs("#f-name").value.trim();
    if (!name) { qs("#modal-error").textContent = "Command name is required."; return; }
    const specErr = findSpecError(spec);
    if (specErr) { qs("#modal-error").textContent = specErr; return; }

    try {
      const originalName = state.editingOriginalName;
      if (originalName) {
        await Api.updateCommand(originalName, name, spec);
        if (originalName !== name) {
          renameInSequence(originalName, name);
          if (favoriteCommands.delete(originalName)) {
            favoriteCommands.add(name);
            saveFavoriteCommands(favoriteCommands);
          }
        }
      } else {
        await Api.createCommand(name, spec);
      }
      closeBuilder();
      await loadCommands();
      state.selected = name;
      renderCommandList();
      renderDetail();
      toast(`Saved "${name}".`, "info");
    } catch (e) {
      qs("#modal-error").textContent = e.message;
    }
  });

  // ===========================================================================
  // Init
  // ===========================================================================

  async function initApp(status) {
    renderStatus(status);
    // Favorites live on the server, not the jarvis CLI, so this loads
    // regardless of status.online — then re-renders the (already loaded,
    // or about-to-load) command list once the real favorite set is in.
    favoriteCommands = await loadFavoriteCommands();
    renderCommandList();
    // Silent: the boot sequence + status pill already explain an offline
    // CLI on first load, so a third toast on top would just be noise.
    await loadCommands({ silent: !status.online });
    if (status.online) await loadMode();
    if (status.online) await loadAiProviders();
    // Load every saved conversation into the sidebar first (they live on
    // disk under ~/.jarvis/conversations and are shared across every
    // browser/session that hits this server — this was previously never
    // called, so old conversations looked "lost" on a fresh page load even
    // though they were still on disk). Then start today's fresh thread on
    // top of that list, per the "opening the page always starts a new
    // conversation" design.
    if (status.online) {
      await refreshConvoList();
      await startNewConversation();
    }
    connectWs();
  }

  wireSkinModal();
  // Fire-and-forget — see loadRegisteredPersonas()'s own comment for why
  // this is never awaited here.
  loadRegisteredPersonas();

  tickClock();
  setInterval(tickClock, 1000);
  runBoot();

  // -------------------------------------------------------------------------
  // Scheduled panel — jobs and quick-add. MCP server status now lives in its
  // own panel (see refreshMcpPanel/openMcp below) — the two used to share one
  // overlay, but scheduling and MCP are unrelated concerns and each earns its
  // own menu entry.
  //
  // Read-mostly on purpose. Creating anything that RUNS (a command, a tool, a
  // full ask) is deliberately not offered here: those go through the approval
  // gate (scheduler._needs_approval) and are far easier to express by asking
  // Jarvis than by filling in a form. What this panel is for is seeing what's
  // set, approving what's parked, and killing what you no longer want.
  // -------------------------------------------------------------------------
  const schedOverlay = qs("#sched-overlay");

  async function refreshScheduledPanel() {
    if (!schedOverlay || schedOverlay.hidden) return;
    const list = qs("#sched-list");
    const statusLine = qs("#sched-status-line");
    try {
      const data = await Api.get("/api/scheduled");
      const jobs = data.jobs || [];
      const counts = data.counts || {};
      statusLine.textContent = jobs.length
        ? `${jobs.length} active \u2014 ${counts.reminders || 0} reminder(s), ${counts.tasks || 0} task(s)` +
          (counts.needs_approval ? `, ${counts.needs_approval} awaiting approval` : "")
        : "nothing scheduled";
      list.innerHTML = "";
      if (!jobs.length) {
        list.appendChild(el("div", { class: "skills-empty" }, "Nothing scheduled yet."));
        return;
      }
      for (const job of jobs) list.appendChild(renderSchedJob(job));
    } catch (err) {
      statusLine.textContent = "couldn't read the schedule";
      list.innerHTML = "";
      list.appendChild(el("div", { class: "skills-empty" }, err.message || "Failed."));
    }
  }

  function renderSchedJob(job) {
    const meta = [job.kind, job.when, job.in ? `in ${job.in}` : null]
      .filter(Boolean).join(" \u00b7 ");
    const actions = [];
    const act = (label, action, body) => {
      const b = el("button", { class: "btn btn--ghost btn--sm", type: "button" }, label);
      b.addEventListener("click", async () => {
        b.disabled = true;
        try {
          await Api.post(`/api/scheduled/${job.id}/${action}`, body || {});
          await refreshScheduledPanel();
        } catch (err) {
          toast(err.message || "That didn't work.");
          b.disabled = false;
        }
      });
      actions.push(b);
    };
    // Approve is only offered for a job actually waiting on it — showing it
    // on everything would suggest every job needs approving, which would
    // make the ones that genuinely do stop standing out.
    if (job.needs_approval) act("Approve", "approve");
    if (job.status === "paused") act("Resume", "resume");
    else if (!job.needs_approval) act("Pause", "pause");
    act("Snooze 10m", "snooze", { delay: "10 minutes" });
    act("Cancel", "cancel");

    return el("div", { class: "skills-item" + (job.needs_approval ? " is-warn" : "") }, [
      el("div", { class: "skills-item__name" }, job.title || "(untitled)"),
      el("div", { class: "skills-item__desc" }, meta),
      job.last_error ? el("div", { class: "skills-item__desc" }, `last error: ${job.last_error}`) : null,
      el("div", { class: "skills-item__actions" }, actions),
    ].filter(Boolean));
  }

  function openScheduled() {
    if (!schedOverlay) return;
    schedOverlay.hidden = false;
    ensureNotifPermission();
    refreshScheduledPanel();
  }

  function closeScheduled() {
    if (schedOverlay) schedOverlay.hidden = true;
  }

  qs("#sched-close")?.addEventListener("click", closeScheduled);
  schedOverlay?.addEventListener("click", (e) => { if (e.target === schedOverlay) closeScheduled(); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && schedOverlay && !schedOverlay.hidden) closeScheduled();
  });

  qs("#btn-sched-add")?.addEventListener("click", async () => {
    const when = qs("#sched-when").value.trim();
    const message = qs("#sched-message").value.trim();
    if (!when || !message) return toast("Both a time and a message are needed.");
    try {
      await Api.post("/api/scheduled", { when, message, kind: "reminder" });
      qs("#sched-when").value = "";
      qs("#sched-message").value = "";
      toast("Scheduled.", "info");
      refreshScheduledPanel();
    } catch (err) {
      // The server passes scheduler/timespec's own error text straight
      // through, which is written to be read by a person ("couldn't read
      // 'nexr tuesday' as a time — try ..."), so it's shown verbatim.
      toast(err.data?.error || err.message || "Couldn't schedule that.");
    }
  });

  qs("#btn-sched-tick")?.addEventListener("click", async () => {
    try {
      const result = await Api.post("/api/scheduled/tick", {});
      const n = (result.ran || []).length;
      toast(n ? `Ran ${n} job(s).` : "Nothing was due.", "info");
      refreshScheduledPanel();
    } catch (err) {
      toast(err.message || "Tick failed.");
    }
  });

  // -------------------------------------------------------------------------
  // MCP Servers panel — its own menu entry, its own overlay. Read-mostly:
  // servers are added by editing mcp_config.json by hand (see mcp_client.py's
  // docstring on why that's a deliberate restriction), so the only action
  // this panel offers is Refresh.
  // -------------------------------------------------------------------------
  const mcpOverlay = qs("#mcp-overlay");

  async function refreshMcpPanel() {
    if (!mcpOverlay || mcpOverlay.hidden) return;
    const list = qs("#mcp-list");
    const statusLine = qs("#mcp-status-line");
    try {
      const data = await Api.get("/api/mcp");
      const servers = data.servers || [];
      statusLine.textContent = servers.length
        ? `${data.enabled_count || 0} enabled \u00b7 ${data.total_tools || 0} tool(s)` +
          (data.needs_refresh ? " \u00b7 refresh recommended" : "")
        : "no servers configured";
      list.innerHTML = "";
      if (!servers.length) {
        list.appendChild(el("div", { class: "skills-empty" },
          "No MCP servers configured. Add them in mcp_config.json, then Refresh."));
        return;
      }
      for (const s of servers) {
        const bits = [
          s.enabled ? "enabled" : "disabled",
          s.transport,
          `${s.tool_count} tool(s)`,
          s.trusted ? "trusted" : "confirm-gated",
          s.stale ? "stale" : null,
        ].filter(Boolean).join(" \u00b7 ");
        list.appendChild(el("div", { class: "skills-item" + (s.error ? " is-warn" : "") }, [
          el("div", { class: "skills-item__name" }, s.name),
          el("div", { class: "skills-item__desc" }, bits),
          s.error ? el("div", { class: "skills-item__desc" }, s.error) : null,
        ].filter(Boolean)));
      }
    } catch (err) {
      statusLine.textContent = "couldn't read MCP status";
      list.innerHTML = "";
      list.appendChild(el("div", { class: "skills-empty" }, err.message || "Failed."));
    }
  }

  function openMcp() {
    if (!mcpOverlay) return;
    mcpOverlay.hidden = false;
    refreshMcpPanel();
  }

  function closeMcp() {
    if (mcpOverlay) mcpOverlay.hidden = true;
  }

  qs("#mcp-close")?.addEventListener("click", closeMcp);
  mcpOverlay?.addEventListener("click", (e) => { if (e.target === mcpOverlay) closeMcp(); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && mcpOverlay && !mcpOverlay.hidden) closeMcp();
  });

  // ===========================================================================
  // Channels panel — Discord/Instagram allowlists. The backend API this
  // drives (GET /api/channels, POST /api/channels/:platform/:set — see
  // server.js) already existed; this is the missing frontend for it, so
  // "who gets a reply" no longer requires a terminal.
  //
  // Token/enabled/owner setup is deliberately left out (and still pointed
  // at the Guides panel) — see server.js's own comment on /api/channels:
  // the UI shows posture and edits allowlists, but a bot token must never
  // pass through this browser.
  // ===========================================================================
  const channelsOverlay = qs("#channels-overlay");
  const CHANNELS_META = {
    discord: { label: "Discord" },
    instagram: { label: "Instagram" },
  };
  const CHANNELS_SETS = [
    { key: "dm_allowlist", short: "dm", label: "DM allowlist", hint: "May open a DM conversation with the bot at all." },
    { key: "reply_allowlist", short: "reply", label: "Reply allowlist", hint: "Actually gets an answer back." },
    { key: "tool_allowlist", short: "tool", label: "Tool allowlist", hint: "May cause a tool to run on this PC." },
  ];

  function channelsChip(entry, onRemove) {
    const isWildcard = entry === "*";
    return el("span", { class: "channels-chip" + (isWildcard ? " channels-chip--wildcard" : "") }, [
      isWildcard ? "everyone (*)" : entry,
      el("button", {
        type: "button", class: "channels-chip__remove", title: `Remove ${entry}`,
        onclick: onRemove,
      }, "\u00d7"),
    ]);
  }

  async function channelsMutate(platform, short, entry, remove, statusEl) {
    if (statusEl) statusEl.textContent = "";
    try {
      const result = await Api.setChannelEntry(platform, short, entry, remove);
      if (!result || result.ok === false) {
        throw new Error((result && result.error) || "Request failed.");
      }
      await refreshChannelsPanel();
    } catch (err) {
      if (statusEl) statusEl.textContent = err.message || "Couldn't save that.";
    }
  }

  function renderChannelsPlatform(platform, block) {
    const meta = CHANNELS_META[platform];
    const enabled = !!block.enabled;
    const tokenSet = block.bot_token === "set" || block.access_token === "set";
    const header = el("div", { class: "skills-item__name" }, [
      `${meta.label} — `,
      el("span", { style: enabled ? "color: var(--accent);" : "color: var(--text-dimmer);" },
        enabled ? "enabled" : "disabled"),
      " \u00b7 token ",
      tokenSet ? "set" : "not set",
      block.owner ? ` \u00b7 owner: ${block.owner}` : " \u00b7 owner: (unset)",
    ]);

    const setBlocks = CHANNELS_SETS.map((set) => {
      const entries = Array.isArray(block[set.key]) ? block[set.key] : [];
      const chips = el("div", { class: "channels-chips" });
      if (!entries.length) {
        chips.appendChild(el("div", { class: "channels-empty-hint" },
          set.short === "reply" ? "nobody — the bot won't reply to anyone yet" : "nobody"));
      } else {
        for (const entry of entries) {
          chips.appendChild(channelsChip(entry, () =>
            channelsMutate(platform, set.short, entry, true, statusLine)));
        }
      }
      const input = el("input", {
        type: "text", placeholder: "id, handle, or * for everyone", autocomplete: "off",
      });
      const addBtn = el("button", { type: "button", class: "btn btn--ghost btn--sm" }, "Add");
      const statusLine = el("div", { class: "channels-empty-hint" });
      const doAdd = () => {
        const value = input.value.trim();
        if (!value) return;
        input.value = "";
        channelsMutate(platform, set.short, value, false, statusLine);
      };
      addBtn.addEventListener("click", doAdd);
      input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); doAdd(); } });
      return el("div", { class: "channels-set" }, [
        el("div", { class: "channels-set__label" }, `${set.label} \u2014 ${set.hint}`),
        chips,
        el("div", { class: "channels-add" }, [input, addBtn]),
        statusLine,
      ]);
    });

    return el("div", { class: "skills-item" }, [header, ...setBlocks]);
  }

  async function refreshChannelsPanel() {
    if (!channelsOverlay || channelsOverlay.hidden) return;
    const list = qs("#channels-list");
    const statusLine = qs("#channels-status-line");
    try {
      const data = await Api.getChannels();
      const cfg = (data && data.config) || {};
      statusLine.textContent = Object.entries(data.summary || {})
        .map(([p, s]) => `${CHANNELS_META[p]?.label || p}: ${(s || "").split("\n")[0]}`)
        .join("  \u00b7  ") || "";
      list.innerHTML = "";
      for (const platform of Object.keys(CHANNELS_META)) {
        list.appendChild(renderChannelsPlatform(platform, cfg[platform] || {}));
      }
    } catch (err) {
      statusLine.textContent = "couldn't read channel status";
      list.innerHTML = "";
      list.appendChild(el("div", { class: "skills-empty" }, err.message || "Failed."));
    }
  }

  function openChannels() {
    if (!channelsOverlay) return;
    channelsOverlay.hidden = false;
    refreshChannelsPanel();
  }

  function closeChannels() {
    if (channelsOverlay) channelsOverlay.hidden = true;
  }

  qs("#channels-close")?.addEventListener("click", closeChannels);
  channelsOverlay?.addEventListener("click", (e) => { if (e.target === channelsOverlay) closeChannels(); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && channelsOverlay && !channelsOverlay.hidden) closeChannels();
  });

  qs("#btn-mcp-refresh")?.addEventListener("click", async () => {
    const btn = qs("#btn-mcp-refresh");
    btn.disabled = true;
    btn.textContent = "Refreshing\u2026";
    try {
      await Api.post("/api/mcp/refresh", {});
      toast("MCP servers refreshed. Restart Jarvis for new tools to appear.", "info");
      refreshMcpPanel();
    } catch (err) {
      toast(err.data?.error || err.message || "Refresh failed.");
    } finally {
      btn.disabled = false;
      btn.textContent = "Refresh";
    }
  });


  // -------------------------------------------------------------------------
  // Guides panel — setup walkthroughs for every integration.
  //
  // Content lives here as data rather than in index.html so a guide is one
  // object to add, and so the search below can match against step text
  // without walking the DOM. Each entry: id, name, blurb, tags (searched),
  // and sections of {heading, steps[], notes[]}.
  //
  // The gotchas matter more than the happy paths — every "notes" entry here
  // is a thing that silently fails rather than erroring, which is exactly
  // the class of problem a setup guide should pre-empt.
  // -------------------------------------------------------------------------
  const GUIDES = [
    {
      id: "build-a-tool", name: "Build a Tool (walkthrough)", blurb: "Start to finish, one real example",
      tags: "build make write custom tool python tutorial walkthrough example handler schema keywords group confirm gate",
      sections: [
        { heading: "Before you write anything: tool or skill?", steps: [
          "If what you're adding is INSTRUCTIONS \u2014 'here's how I want the weekly report written' \u2014 you want a SKILL, not a tool. Skills are plain SKILL.md files, need no Python, and cost nothing in the prompt until loaded.",
          "If it has to RUN something \u2014 call an API, read a file, poke a device \u2014 it's a tool.",
          "Rule of thumb: if you'd write it as instructions, make it a skill; if you'd write it as a function, make it a tool.",
        ]},
        { heading: "Step 1 \u2014 open the editor and pick a template", steps: [
          "Menu > Custom Tools. Choose a template from the dropdown, then New.",
          "'Minimal' is one tool with one argument. 'Shows a popup' demonstrates toasts and in-chat cards. 'Asks the user something' shows a blocking confirm with a safe default. 'Calls an API' shows HTTP plus result shaping.",
          "Give it a filename in the Name box: lower_snake_case, starts with a letter. This is the FILE name \u2014 one file can define several tools.",
        ]},
        { heading: "Step 2 \u2014 the handler", steps: [
          "def tool_disk_report(args): \u2014 takes one dict, returns one dict. That's the whole contract.",
          "args is never None, but treat every key as optional and untrusted. The model fills these in; it gets them wrong sometimes.",
          "NEVER raise. Catch your own exceptions and return {\"error\": \"...\"}. An uncaught exception is caught one layer up, but your message is far more useful to the model than a bare repr.",
          "Return {\"needs_clarification\": True, \"message\": \"Which folder?\"} when you need more from the user \u2014 the model knows to ask rather than guess.",
          "Keep the returned dict SMALL. Every character is sent to the model and billed. Return what's needed to answer, not everything you happen to have.",
        ]},
        { heading: "Step 3 \u2014 the schema (this is what actually matters)", steps: [
          "TOOL_SCHEMAS = [{\"name\", \"description\", \"parameters\"}]. The description is the single highest-leverage thing in the file.",
          "Write it like briefing a coworker on WHEN to reach for this \u2014 not what it does. 'Show a disk usage report as a card in the chat. Use when the user asks how much space is left.' beats 'Reports disk usage.'",
          "Every parameter gets its own description too. 'Drive or mount point, e.g. C: or /' is worth ten words of prose elsewhere.",
          "Mark only genuinely-required things in \"required\". Anything with a sensible default should be optional.",
          "Name it something no built-in uses. The editor checks and tells you before it saves.",
        ]},
        { heading: "Step 4 \u2014 TOOLS and TOOL_GROUP", steps: [
          "TOOLS = {\"disk_report\": tool_disk_report} \u2014 one entry per schema name, no extras. Mismatches are rejected.",
          "TOOL_GROUP = \"custom\" \u2014 the router group. Join an EXISTING group (files, web, desktop, system_control...) and your tool is offered whenever that group is, alongside its siblings.",
          "Or start a new group. That's cleaner, but see the next section \u2014 a new group without keywords is nearly invisible.",
        ]},
        { heading: "Step 5 \u2014 TOOL_KEYWORDS (skip this and your tool is nearly unreachable)", steps: [
          "Jarvis does NOT send every tool to the model. A local keyword router picks a group first \u2014 that's the whole token-optimisation design.",
          "TOOL_KEYWORDS = {\"disk_report\": {\"disk space\": 10, \"how full\": 8, \"free space\": 9}}",
          "A phrase needs weight >= 5 to count as real signal. Weights are additive across phrases.",
          "Phrases are matched on WORD BOUNDARIES, not substrings \u2014 'ping' will not match 'pinging'. Write the phrases people actually type.",
          "Without keywords, a brand-new group is only reachable through search_tools \u2014 which works, but costs an extra round trip every time.",
          "Need a phrase to NOT match when another is present? Use {\"weight\": 8, \"not_with\": [\"screen recording\"]}.",
        ]},
        { heading: "Step 6 \u2014 gate it if it's dangerous", steps: [
          "TOOL_CONFIRM_REQUIRED = {\"cleanup_temp\"} \u2014 the user is shown the call and must approve before it runs.",
          "TOOL_AI_REVIEW = {\"cleanup_temp\"} \u2014 a SECOND AI provider assesses the risk first and its note is shown alongside.",
          "This is enforced out of band, from a config file the model cannot reach. It is NOT a \"confirm\": true parameter on your own schema \u2014 that shape lets the model approve itself, which protects nothing.",
          "The policy engine (jarvis/policy.py) also scores every call by tool, arguments and context, and can escalate on its own. Name your arguments plainly \u2014 a tool with a `path` argument is scored far more accurately than one hiding a path inside an opaque string.",
        ]},
        { heading: "Step 7 \u2014 talk to the user", steps: [
          "A return value goes to the MODEL, not the person. To reach the person: from jarvis import ui_bridge as ui",
          "ui.toast(\"Backup finished\", level=\"success\") \u2014 transient corner message.",
          "ui.bubble(\"Disk report\", body=table) \u2014 a card in the chat that persists across reloads.",
          "ui.dialog(\"Disk almost full\", body, level=\"error\") \u2014 a modal that interrupts.",
          "if ui.confirm(\"Delete 12 files?\", body=listing, default=False): \u2014 blocking, and the default is what an UNATTENDED run gets.",
          "Always point the default at the safe outcome. Your tool will eventually be run by a scheduled job at 3am, because the user can schedule any tool.",
        ]},
        { heading: "Step 8 \u2014 check, save, run", steps: [
          "Check validates without saving. Errors are reported by stage: syntax (a typo), import (usually a circular import \u2014 see the pitfalls below), or contract (a missing description, a handler with no schema).",
          "Save validates first and refuses to write a file that would be silently rejected at startup. It keeps a .bak of the previous version.",
          "Run executes the real handler with arguments you supply. Real side effects really happen. There is no sandbox.",
          "Ctrl+S saves. Tab indents four spaces.",
        ]},
        { heading: "Step 9 \u2014 make it live", steps: [
          "Tools are discovered once, at process start, so a new tool is live on your very next command.",
          "A long-running daemon (sched-daemon, discord-daemon) has to be restarted to see it.",
          "jarvis doctor tools \u2014 lists any file that was rejected, and why.",
          "Then just ask for it in plain language. If Jarvis doesn't reach for it, your keywords are the thing to fix, not the handler.",
        ]},
        { heading: "The four pitfalls that cost real time", steps: [
          "CIRCULAR IMPORT: never import ai_client, tool_router or tool_registry at module level. At discovery time jarvis.tools is only half-initialised. Import them INSIDE your handler. This fails with a confusing partially-initialised-module error and once silently dropped a built-in tool for weeks.",
          "SILENT REJECTION: a file that fails validation is logged once at startup and then invisible \u2014 no tool, no error in the UI. That's exactly why the editor validates before saving. If a tool 'disappeared', run jarvis doctor tools.",
          "NO KEYWORDS: the handler is perfect, the schema is perfect, and Jarvis never calls it. Almost always this.",
          "WRONG DIRECTORY: tools in ~/.jarvis/tools survive a rebuild. Tools written into the install directory (jarvis/actions/) are DESTROYED by the next script.bat run.",
        ]},
        { heading: "Multiple tools in one file", steps: [
          "Perfectly normal, and usually better \u2014 related tools that share helpers belong together.",
          "Add an entry to both TOOL_SCHEMAS and TOOLS for each, all in the same TOOL_GROUP.",
          "A file starting with _ is skipped by the loader, so _shared_helpers.py can sit next to your tools without being mistaken for one.",
        ]},
      ],
      notes: [
        "A custom tool is arbitrary Python running as you. Same trust level as commands.json, which runs arbitrary shell.",
        "The MODEL cannot author these. That's deliberate: a model that could write its own tools could route around every confirm gate by writing an unflagged tool that does the same thing.",
        "Everything here matches jarvis/actions/_template.py, which is the same contract with more detail \u2014 read it if you're writing something unusual.",
      ],
    },
    {
      id: "custom-tools", name: "Custom Tools", blurb: "Write your own Python tools",
      tags: "custom tools python actions write code extend plugin handler schema popup ui",
      sections: [
        { heading: "Where they live", steps: [
          "Menu > Custom Tools, or the folder directly: ~/.jarvis/tools/",
          "NOT jarvis/actions/ — script.bat overwrites the install directory on every rebuild, so a tool written there is destroyed by the next build. ~/.jarvis never moves.",
          "One .py file per tool set. A file starting with _ is ignored, so _helpers.py is safe to keep alongside.",
          "x.py.disabled is switched off but kept — that's what the Enabled checkbox toggles.",
        ]},
        { heading: "The contract — three names, all required", steps: [
          "TOOL_SCHEMAS = [{\"name\", \"description\", \"parameters\"}]  — the description is what tells the model WHEN to use it, so write it like briefing a coworker.",
          "TOOLS = {\"name\": handler}  — handler(args: dict) -> dict. One entry per schema, no extras.",
          "TOOL_GROUP = \"custom\"  — the router group it joins.",
          "Optional: TOOL_KEYWORDS, TOOL_PACK_INSTRUCTION, TOOL_CONFIRM_REQUIRED, TOOL_AI_REVIEW.",
        ]},
        { heading: "Four rules that bite", steps: [
          "A handler must NEVER raise. Catch your own exceptions and return {\"error\": \"...\"}.",
          "Import ai_client / tool_router / tool_registry INSIDE the handler, never at module level — at discovery time jarvis.tools is only half-initialised and a module-level import fails with a circular-import error.",
          "Without TOOL_KEYWORDS, a brand-new group is only reachable through search_tools, never through normal routing. Add at least one phrase.",
          "Name it something no built-in already uses. The editor checks and tells you.",
        ]},
        { heading: "Buttons in the editor", steps: [
          "Check — validates without saving. Reports syntax, import and contract errors separately, because they need different fixes.",
          "Save — validates FIRST and refuses to write a file that would be silently rejected at startup. Keeps a .bak of the previous version.",
          "Run — actually executes the handler with arguments you supply. Real side effects really happen; there is no sandbox.",
          "Ctrl+S saves. Tab indents four spaces instead of leaving the box.",
        ]},
        { heading: "Picking it up", steps: [
          "Tools are discovered once, at process start. A new tool is live on the next command you run.",
          "A long-running daemon (sched-daemon, discord-daemon) has to be restarted to see it.",
          "jarvis doctor tools — lists any file that was rejected and why.",
        ]},
      ],
      notes: [
        "A custom tool is arbitrary Python running as you. That's the same trust level as commands.json (arbitrary shell) — the person writing it is the person running it.",
        "The MODEL cannot write these. Authoring is human-only on purpose: a model that could write its own tools could route around every confirm gate by writing an unflagged tool that does the same thing.",
      ],
    },
    {
      id: "tool-popups", name: "Tool Popups & Dialogs", blurb: "Show things and ask things from a tool",
      tags: "popup dialog toast confirm prompt modal notify error ui bridge bubble progress form choose",
      sections: [
        { heading: "The idea", steps: [
          "Any tool can put something on screen and get an answer back: from jarvis import ui_bridge as ui",
          "The same call works in the web UI (a real popup), a plain terminal (styled text + input()), and headless (returns your default immediately).",
          "That last one matters most — see 'The headless rule' below.",
        ]},
        { heading: "Show something (returns immediately)", steps: [
          "ui.toast(\"Backup finished\", level=\"success\")  — transient corner message. Errors stay until dismissed; everything else fades.",
          "ui.bubble(\"Disk report\", body=table, level=\"warn\")  — a card IN the chat thread. Persists, survives a reload, scroll back to it.",
          "ui.dialog(\"Careful\", \"This overwrites 12 files.\", level=\"error\")  — a real modal. Interrupts. Reserve it for things that should.",
          "ui.progress(\"Indexing\", 3, 10, job_id=pid)  — an updatable row, bottom-left.",
          "level is one of: info, success, warn, error.",
        ]},
        { heading: "Ask something (blocks for an answer)", steps: [
          "ui.confirm(\"Delete 12 files?\", body=listing, default=False) -> bool",
          "ui.choose(\"Which environment?\", [\"staging\", \"production\"]) -> str",
          "ui.prompt(\"Name the backup?\", default=\"backup-1\") -> str",
          "ui.form(\"Setup\", [{\"name\": \"host\", \"default\": \"127.0.0.1\"}]) -> dict",
          "Escape / clicking away always picks the SAFE answer — a dismissed confirm is never a yes.",
        ]},
        { heading: "The headless rule", steps: [
          "EVERY blocking call needs a default, and it is returned instantly when nobody is watching — a scheduled job at 3am, a daemon, a test.",
          "Without that, a tool blocking on input() inside a scheduler tick hangs the whole tick, silently, forever.",
          "So set the default to the safe outcome: default=False on a delete means an unattended run deletes nothing.",
          "There is also a timeout (120s default) for a web dialog nobody answers.",
        ]},
        { heading: "What it is NOT", steps: [
          "ui.confirm() is a courtesy question, NOT a security gate.",
          "The real gate is TOOL_CONFIRM_REQUIRED, checked out of band against ~/.jarvis/tool_safety.json — a file the model cannot reach.",
          "A destructive tool should have both: the flag for protection, the ui.confirm for a readable question.",
          "Every field you pass is rendered as plain text, never as HTML. A filename containing <script> is shown, not run.",
        ]},
      ],
      notes: [
        "Start from Menu > Custom Tools > template 'Shows a popup' or 'Asks the user something' — both are working examples of everything above.",
      ],
    },
    {
      id: "themes", name: "Themes & Appearance", blurb: "Skins, colours and interface tuning",
      tags: "theme skin colour color appearance dark light accent font glow scanlines customise",
      sections: [
        { heading: "Picking a theme", steps: [
          "Skin > Theme. Six built in: Jarvis, Mark I, Terminal, Mono, Daylight (a real light theme) and Nebula.",
          "Applies instantly, no reload. Remembered per browser.",
        ]},
        { heading: "Tuning on top", steps: [
          "Skin > Interface adjusts any theme without editing it: corner rounding (0 = fully square), glow/shadow intensity, text size, animations, grid overlay.",
          "Useful combinations: Mono + rounding 0 + glow 0 for a flat, plain look; any theme + animations off for a low-distraction setup.",
          "Animations also switch off automatically if your OS asks for reduced motion.",
        ]},
        { heading: "Making your own", steps: [
          "Pick the closest theme, adjust the accent colour, then Save as new — it snapshots what's currently on screen.",
          "Export current gives you JSON you can save as a file and share.",
          "Import... takes that JSON back. Only recognised CSS variables are accepted, so an imported skin can't restyle arbitrary parts of the app.",
        ]},
      ],
      notes: [
        "A theme is just a set of CSS variables, so a new one is data rather than code.",
        "Themes are per-browser (localStorage). Persona name and attitude are different — those are server-side in ai_config.json and apply everywhere.",
      ],
    },
    {
      id: "doctor", name: "Doctor / Self-check", blurb: "One command that checks everything",
      tags: "doctor diagnose diagnostic broken health check fix troubleshoot",
      sections: [
        { heading: "Running it", steps: [
          "jarvis doctor            — everything, offline, only shows problems.",
          "jarvis doctor --verbose  — shows passing checks too.",
          "jarvis doctor --deep     — also tests every API key with a real request, pings Playnite, handshakes MCP servers.",
          "jarvis doctor channels   — just one area (runtime, binaries, packages, ai, tools, memory, scheduler, notify, digest, daemons, channels, playnite, mcp).",
          "jarvis doctor --json     — machine readable.",
        ]},
        { heading: "What it catches", steps: [
          "ffmpeg / tesseract missing from PATH — the reason youtube_download and click_on_text 'just fail'.",
          "An API key that's present but expired, rate-limited or out of credit (--deep only).",
          "A Discord bot enabled with a valid token and an EMPTY allowlist — it connects, looks online, and ignores everyone including you, with no error anywhere.",
          "A stale daemon pid file, which makes a new daemon refuse to start while nothing is actually running.",
          "Scheduled jobs overdue by more than an hour, i.e. nothing is ticking.",
          "A custom tool or action file that was rejected at startup and is silently missing.",
        ]},
        { heading: "Reading it", steps: [
          "FAIL = broken now. warn = works, will bite later. -- = not applicable, which is a normal result for anything you don't use.",
          "Every FAIL and warn carries a literal fix — a command to run or a file to edit.",
          "Exit code: 0 healthy, 1 warnings, 2 something broken. Usable in a script.",
        ]},
      ],
      notes: [
        "doctor never changes anything. You run it when something is already broken; it must not be able to make it worse.",
      ],
    },
    {
      id: "discord", name: "Discord", blurb: "Bot that answers @mentions and DMs",
      tags: "discord bot token gateway intents mention dm server guild",
      sections: [
        { heading: "Create the bot", steps: [
          "Go to discord.com/developers/applications and click New Application.",
          "Open the Bot tab, then Reset Token and copy the token.",
          "Leave every Privileged Gateway Intent OFF. You do not need them — see the note below.",
          "In OAuth2 > URL Generator tick scope 'bot', then permissions: Send Messages, Read Message History. Open the generated URL to invite it to your server.",
        ]},
        { heading: "Connect it to Jarvis", steps: [
          "jarvis channels-config           (prints the config file path)",
          "jarvis channels-set discord bot_token YOUR_TOKEN",
          "jarvis channels-set discord enabled true",
          "In Discord: Settings > Advanced > Developer Mode ON, then right-click your name > Copy User ID.",
          "jarvis channels-set discord owner YOUR_USER_ID",
          "jarvis channels-allow discord reply YOUR_USER_ID",
          "jarvis channels-allow discord dm YOUR_USER_ID",
          "jarvis channels-allow discord tool YOUR_USER_ID",
          "jarvis channels-set discord allow_tools true",
          "jarvis discord-daemon            (runs in the foreground)",
        ]},
        { heading: "Permissions", steps: [
          "dm_allowlist — who may DM the bot at all.",
          "reply_allowlist — who gets an answer back.",
          "tool_allowlist — whose requests may run tools on this PC.",
          "The three are independent. A friend on reply_allowlist but not tool_allowlist gets a conversation with tools switched off.",
          "Empty means NOBODY. Put \"*\" in a list to mean everyone — that has to be typed on purpose.",
        ]},
        { heading: "Servers and per-server rules", steps: [
          "The bot works in any server it's invited to, using the same allowlists.",
          "In a server it only answers when @mentioned. In a DM it answers without one.",
          "To loosen or tighten one server, add a scope in channels.json:",
          "\"scopes\": { \"guild:123456\": { \"reply_allowlist\": [\"*\"], \"allow_tools\": false } }",
          "Only the keys you write are overridden; everything else inherits. Specificity: thread > channel > guild.",
        ]},
      ],
      notes: [
        "You do NOT need the Message Content Intent. Discord exempts DMs to your bot and messages that @mention it — which is exactly what Jarvis reads. Leaving it off means nothing to justify at review and no breakage at 100 servers.",
        "A bot can only DM someone who shares a server with it and has 'allow DMs from server members' on. That is the usual cause of a failed owner notification.",
        "Install the library with: pip install -U discord.py  — the PyPI package named 'discord' is a different, unmaintained project.",
      ],
    },
    {
      id: "instagram", name: "Instagram", blurb: "Graph API DMs — read the limits first",
      tags: "instagram meta graph api webhook igsid professional business creator dm",
      sections: [
        { heading: "Before you start — hard platform limits", steps: [
          "A PERSONAL account cannot be automated at all. The Basic Display API is dead and has no successor. You need a Professional (Business or Creator) account.",
          "The bot CANNOT start a conversation. It may only reply within 24 hours of the other person's last message. That clock resets each time they write.",
          "So 'DM me when the task finishes' only works if you messaged the bot in the last 24h. Jarvis falls back to Discord automatically when the window is shut.",
          "The HUMAN_AGENT tag extends the window to 7 days but Meta restricts it to messages typed by a real human. Jarvis never sends it — automated use gets API access revoked.",
        ]},
        { heading: "Set up the Meta app", steps: [
          "Switch the Instagram account to Professional (Settings > Account type).",
          "At developers.facebook.com create an app and add the Instagram product.",
          "Request the instagram_manage_messages permission.",
          "In the Instagram app: Settings > Messages > allow access to messages. This fails silently if it's off.",
          "Generate an access token and note your Instagram user id.",
        ]},
        { heading: "Connect it to Jarvis", steps: [
          "jarvis channels-set instagram access_token YOUR_TOKEN",
          "jarvis channels-set instagram ig_user_id YOUR_IG_USER_ID",
          "jarvis channels-set instagram app_secret YOUR_APP_SECRET",
          "jarvis channels-set instagram verify_token ANY_STRING_YOU_INVENT",
          "jarvis channels-set instagram bot_handle your_account_handle",
          "jarvis channels-set instagram enabled true",
          "jarvis instagram-serve           (listens on 127.0.0.1:19824)",
          "Expose it over HTTPS with a tunnel (Cloudflare Tunnel, ngrok) — Meta requires a public HTTPS callback with a valid certificate.",
          "In the Meta dashboard set the callback URL to https://your-tunnel/webhook and the verify token to the one you invented above, then subscribe to the 'messages' field.",
        ]},
        { heading: "Group threads", steps: [
          "Group threads work the same way as Discord servers: the bot only answers when @mentioned.",
          "Set bot_handle so it can recognise its own @name — Instagram sends mentions as plain text, not as structured data, so this is a text match.",
          "The same three allowlists apply, keyed on the sender's IGSID.",
        ]},
      ],
      notes: [
        "Rate limits: 100 text sends/sec, but only 2 conversation reads/sec — the tightest limit. Jarvis derives state from webhooks rather than re-reading threads.",
        "Your IGSID is assigned per-app and only exists after you've messaged the bot. Run `jarvis instagram-serve` and DM the bot; the trace prints the sender id.",
        "The webhook is signature-verified (X-Hub-Signature-256). Without app_secret set, Jarvis rejects every webhook — an unverified endpoint is an open door.",
      ],
    },
    {
      id: "spotify", name: "Spotify", blurb: "Playback control via Web API + PKCE",
      tags: "spotify music playback oauth pkce client id premium",
      sections: [
        { heading: "Basic (no API needed)", steps: [
          "spotify_open just launches the desktop app. Nothing to configure.",
        ]},
        { heading: "Full playback control", steps: [
          "Create an app at developer.spotify.com/dashboard.",
          "jarvis spotify-config     (prints the path to spotify.json)",
          "Copy the redirect URI from that file into the Spotify dashboard exactly as written.",
          "Paste your client_id into spotify.json.",
          "jarvis spotify-login",
          "Log in with the SAME account the desktop app is signed into.",
        ]},
      ],
      notes: [
        "Playback control (play/pause/skip/volume) requires Spotify Premium — the API returns 403 on free accounts.",
        "Spotify needs an active device. If nothing is playing anywhere, start playback once in the app first.",
      ],
    },
    {
      id: "playnite", name: "Playnite", blurb: "Game library browse and launch",
      tags: "playnite games library launch bridge plugin emulator",
      sections: [
        { heading: "Install the bridge", steps: [
          "Install the Playnite Bridge plugin from the playnitebridge/ folder in this repo.",
          "Start Playnite — the bridge serves an HTTP API on localhost:19821.",
          "jarvis playnite-config    (prints the path to playnite.json)",
          "Paste the token the plugin shows into playnite.json.",
        ]},
      ],
      notes: [
        "Playnite must be RUNNING. The bridge is a plugin inside it, not a standalone service.",
        "Launching by action id supports mods, emulators and custom URLs — not just the default Play action.",
        "LibraryPlugin actions are virtual and never persisted, so they're resolved fresh rather than cached.",
      ],
    },
    {
      id: "ytdl", name: "yt-dlp", blurb: "Video info, formats and downloads",
      tags: "ytdl yt-dlp youtube download video audio ffmpeg format",
      sections: [
        { heading: "Install", steps: [
          "pip install -U yt-dlp",
          "Install ffmpeg and put it on PATH — without it, merging separate video and audio streams fails.",
          "Check it works: yt-dlp --version",
        ]},
      ],
      notes: [
        "yt-dlp breaks whenever a site changes. If downloads suddenly fail, update it first: pip install -U yt-dlp.",
        "The highest-quality streams are usually video-only and audio-only, merged afterwards. That merge is ffmpeg's job — no ffmpeg means no 1080p+.",
        "See DOCUMENTATION/yt-dlp-exhaustive-reference.md for the full option surface.",
      ],
    },
    {
      id: "pyautogui", name: "PyAutoGUI", blurb: "Mouse, keyboard and window control",
      tags: "pyautogui desktop mouse keyboard click type hotkey window automation screen",
      sections: [
        { heading: "Install", steps: [
          "pip install pyautogui",
          "Windows: works out of the box.",
          "macOS: grant Accessibility AND Screen Recording permission to your terminal in System Settings > Privacy & Security.",
          "Linux (X11): pip install python-xlib. On Wayland it mostly does not work — most compositors block synthetic input by design.",
        ]},
        { heading: "Use it well", steps: [
          "Prefer click_on_text: it screenshots, OCRs, and clicks a visible label in one call.",
          "Avoid get_window_size then guessing coordinates — that's the fragile path, and it costs extra rounds.",
        ]},
      ],
      notes: [
        "PyAutoGUI has a failsafe: slam the mouse into a screen corner to abort a runaway script.",
        "Coordinates are wrong on scaled displays unless DPI awareness is set. If clicks land offset, that's usually why.",
      ],
    },
    {
      id: "pytesseract", name: "pytesseract / OCR", blurb: "Reading text off the screen",
      tags: "pytesseract tesseract ocr read screen text click_on_text image",
      sections: [
        { heading: "Install", steps: [
          "pip install pytesseract pillow",
          "Install the Tesseract BINARY separately — the pip package is only a wrapper around it.",
          "Windows: install from the UB Mannheim build, then add the install folder to PATH.",
          "macOS: brew install tesseract.   Linux: apt install tesseract-ocr",
          "Check it works: tesseract --version",
        ]},
      ],
      notes: [
        "'pytesseract is installed but nothing works' almost always means the binary is missing or not on PATH. The wrapper cannot do OCR by itself.",
        "OCR accuracy drops badly on small or low-contrast text. If click_on_text keeps missing, increase the app's font size or zoom before blaming the matcher.",
        "Screenshots are saved under ~/.jarvis/screenshots and pruned automatically. They are never sent to the model as pixels.",
      ],
    },
    {
      id: "everything", name: "Everything (file search)", blurb: "Instant filename search on Windows",
      tags: "everything voidtools file search index windows dll",
      sections: [
        { heading: "Install", steps: [
          "Install Everything from voidtools.com and let it finish building its index.",
          "Everything must be running for search_files to work.",
          "jarvis everything-config  to override the DLL/exe path if it isn't found automatically.",
        ]},
      ],
      notes: [
        "Windows only. On other platforms the file tools fall back to slower methods.",
        "reveal_in_explorer had a long-standing bug with paths containing spaces — fixed, but if a path opens the wrong folder, that's the shape of the old failure.",
      ],
    },
    {
      id: "mcp", name: "MCP servers", blurb: "External tools over Model Context Protocol",
      tags: "mcp model context protocol server external tools",
      sections: [
        { heading: "Connect", steps: [
          "jarvis mcp-config     (prints the config path)",
          "Add a server entry with its command and args.",
          "jarvis mcp-refresh    to pull its tool list.",
          "jarvis mcp-status     to check what's connected.",
          "The MCP Servers panel in this menu shows the same thing visually.",
        ]},
      ],
      notes: [
        "MCP tools join the normal router, so they're only sent to the model when relevant — they don't inflate every prompt.",
      ],
    },
    {
      id: "voice", name: "Voice (STT / TTS)", blurb: "Speaking and listening",
      tags: "voice tts stt speech whisper microphone speak listen audio",
      sections: [
        { heading: "Set up", steps: [
          "jarvis voice-config   (prints the config path)",
          "jarvis speak \"hello\"  to test output.",
          "jarvis listen         to test input.",
        ]},
      ],
      notes: [
        "Microphone access needs OS permission on macOS and Windows; it fails silently as an empty transcript otherwise.",
      ],
    },
  ];

  const guidesOverlay = qs("#guides-overlay");
  const guidesList = qs("#guides-list");
  const guideBody = qs("#guide-body");
  const guideTitle = qs("#guide-title");
  let guidesFilter = "";
  let guideSelected = null;

  function renderGuidesList() {
    const needle = guidesFilter.toLowerCase();
    // Match against name, blurb and tags — tags exist so "ocr" finds
    // pytesseract and "intents" finds Discord, neither of which appear in
    // the visible name.
    const items = GUIDES.filter((g) => !needle ||
      (g.name + " " + g.blurb + " " + g.tags).toLowerCase().includes(needle));
    guidesList.innerHTML = "";
    if (!items.length) {
      guidesList.appendChild(el("div", { class: "skills-empty" }, "No guides match."));
      return;
    }
    for (const g of items) {
      guidesList.appendChild(el("div", {
        class: "logs-convo-card" + (g.id === guideSelected ? " is-active" : ""),
        onclick: () => selectGuide(g.id),
      }, [
        el("div", { class: "logs-convo-card__title" }, g.name),
        el("div", { class: "logs-convo-card__meta" }, g.blurb),
      ]));
    }
  }

  function selectGuide(id) {
    guideSelected = id;
    const guide = GUIDES.find((g) => g.id === id);
    renderGuidesList();
    if (!guide) return;
    guideTitle.textContent = guide.name;
    guideBody.innerHTML = "";
    for (const section of guide.sections || []) {
      guideBody.appendChild(el("h4", { class: "guide-heading" }, section.heading));
      const list = el("ol", { class: "guide-steps" });
      for (const step of section.steps || []) {
        // A step that looks like a command is rendered as one so it can be
        // copied without picking prose out of it.
        const isCommand = /^(jarvis |pip |npm |brew |apt |yt-dlp |tesseract |")/.test(step)
          || step.startsWith("\"scopes\"");
        list.appendChild(el("li", {}, isCommand
          ? [el("code", { class: "guide-cmd" }, step)]
          : step));
      }
      guideBody.appendChild(list);
    }
    if (guide.notes && guide.notes.length) {
      guideBody.appendChild(el("h4", { class: "guide-heading" }, "Gotchas"));
      const notes = el("ul", { class: "guide-notes" });
      for (const note of guide.notes) notes.appendChild(el("li", {}, note));
      guideBody.appendChild(notes);
    }
  }

  function openGuides() {
    guidesOverlay.hidden = false;
    if (!guideSelected) renderGuidesList();
  }

  qs("#guides-close")?.addEventListener("click", () => { guidesOverlay.hidden = true; });
  // Same click-outside-to-close pattern every other overlay uses (Debug,
  // Skills, Logs, etc.) — this one was just missing it.
  guidesOverlay?.addEventListener("click", (e) => {
    if (e.target === guidesOverlay) guidesOverlay.hidden = true;
  });
  qs("#guides-search")?.addEventListener("input", (e) => {
    guidesFilter = e.target.value || "";
    renderGuidesList();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !guidesOverlay.hidden) guidesOverlay.hidden = true;
  });

  // -------------------------------------------------------------------------
  // Panel-menu dropdown — one trigger for Debug / Skills / Scheduled / MCP
  // -------------------------------------------------------------------------
  // Servers, replacing four buttons that used to sit side by side in the
  // console header. Same open/close/outside-click/Escape pattern as the Ask
  // panel's .provider-picker (see loadAiProviders et al. above).
  //
  // Two triggers share this one dropdown: #btn-panel-menu (Live Feed header,
  // next to Clear) and #btn-panel-menu-focus (Ask panel header, Focus-mode
  // only — see .panel-menu--focus-only in style.css). Rather than duplicate
  // #panel-menu-list's eleven items and every click handler wired to them
  // below, opening the menu reparents that one list to <body> (a portal —
  // see the long comment on openPanelMenuFrom for why) and points it at
  // whichever trigger was clicked.
  // -------------------------------------------------------------------------
  const panelMenuEl = qs("#panel-menu");
  const panelMenuBtn = qs("#btn-panel-menu");
  const panelMenuFocusEl = qs("#panel-menu-focus");
  const panelMenuFocusBtn = qs("#btn-panel-menu-focus");
  const panelMenuList = qs("#panel-menu-list");

  // BUGFIX: this used to just be `wrapperEl.appendChild(panelMenuList)`,
  // relying on .panel-menu__list's `position: absolute` to anchor it under
  // whichever trigger's wrapper it was dropped into (.panel-menu{ position:
  // relative }). That works fine for the Focus-mode trigger, which sits in
  // the Ask panel header, but the console trigger (#panel-menu) lives inside
  // .panel--console — and every .panel, this one included, sets `overflow:
  // hidden` so its rounded corners clip cleanly. An absolutely-positioned
  // child is still clipped by an `overflow: hidden` ancestor in the DOM
  // regardless of its own containing block, so once the list (eleven items)
  // grew taller than the console panel itself, everything from "Log search"
  // down (Setup, Channels) rendered outside the panel's box and was simply
  // invisible/unclickable — "the dropdown gets cut off under Backlog".
  // Portaling the list to <body> and positioning it with `fixed` coordinates
  // computed from the trigger's own bounding rect escapes that clipping
  // entirely, no matter how tall the list grows or which trigger opened it.
  function openPanelMenuFrom(wrapperEl, btnEl) {
    if (!wrapperEl || !btnEl || !panelMenuList) return;
    document.body.appendChild(panelMenuList);
    panelMenuList.hidden = false;
    panelMenuList.style.position = "fixed";
    const rect = btnEl.getBoundingClientRect();
    const listWidth = panelMenuList.offsetWidth || 230;
    const left = Math.max(8, Math.min(rect.right - listWidth, window.innerWidth - listWidth - 8));
    const top = Math.min(rect.bottom + 6, window.innerHeight - 60);
    panelMenuList.style.left = `${left}px`;
    panelMenuList.style.top = `${top}px`;
    panelMenuList.style.right = "auto";
    panelMenuList.style.maxHeight = `${Math.max(160, window.innerHeight - top - 16)}px`;
    panelMenuList.style.overflowY = "auto";
    btnEl.setAttribute("aria-expanded", "true");
  }

  function closePanelMenu() {
    if (!panelMenuList) return;
    panelMenuList.hidden = true;
    panelMenuBtn?.setAttribute("aria-expanded", "false");
    panelMenuFocusBtn?.setAttribute("aria-expanded", "false");
  }

  panelMenuBtn?.addEventListener("click", () => {
    if (!panelMenuList.hidden && panelMenuList.dataset.openFrom === "console") return closePanelMenu();
    panelMenuList.dataset.openFrom = "console";
    openPanelMenuFrom(panelMenuEl, panelMenuBtn);
  });
  panelMenuFocusBtn?.addEventListener("click", () => {
    if (!panelMenuList.hidden && panelMenuList.dataset.openFrom === "focus") return closePanelMenu();
    panelMenuList.dataset.openFrom = "focus";
    openPanelMenuFrom(panelMenuFocusEl, panelMenuFocusBtn);
  });

  document.addEventListener("click", (e) => {
    if (panelMenuList.hidden) return;
    const inList = panelMenuList.contains(e.target);
    const inConsole = panelMenuEl?.contains(e.target);
    const inFocus = panelMenuFocusEl?.contains(e.target);
    if (!inList && !inConsole && !inFocus) closePanelMenu();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !panelMenuList.hidden) closePanelMenu();
  });
  // The list is fixed-positioned from a rect computed at open time; if the
  // viewport resizes while it's open (rotating a tablet, resizing the
  // window) that rect goes stale, so just close it rather than leave it
  // floating over the wrong spot.
  window.addEventListener("resize", () => {
    if (!panelMenuList.hidden) closePanelMenu();
  });

  // Each item opens its destination, then closes the dropdown — the menu
  // itself is never the thing left on screen after a choice is made.
  qs("#menu-item-guides")?.addEventListener("click", () => { closePanelMenu(); openGuides(); });
  qs("#menu-item-debug")?.addEventListener("click", () => { closePanelMenu(); openDebug(); });
  qs("#menu-item-skills")?.addEventListener("click", () => { closePanelMenu(); openSkills(); });
  qs("#menu-item-scheduled")?.addEventListener("click", () => { closePanelMenu(); openScheduled(); });
  qs("#menu-item-mcp")?.addEventListener("click", () => { closePanelMenu(); openMcp(); });
  qs("#menu-item-channels")?.addEventListener("click", () => { closePanelMenu(); openChannels(); });
  qs("#menu-item-ctools")?.addEventListener("click", () => {
    closePanelMenu();
    if (window.JarvisCustomTools) window.JarvisCustomTools.open();
  });

  // ===========================================================================
  // DAEMONS / BACKLOG / LOG SEARCH / SETUP / LAYOUT
  //
  // Daemons, Backlog and Log search are built on the bigger .debug-overlay
  // chrome (same as Debug/Logs); Setup stays on the plainer .menu-overlay
  // chrome the Scheduled and MCP panels use — see the CSS comment above the
  // daemons/backlog/logsearch rules for why. Every one of them talks to a
  // REST route that shells out to the matching `jarvis` subcommand, so the
  // browser never re-implements a rule that lives in Python — see server.js's
  // comment above the routes for why that split is load-bearing rather than
  // tidy.
  // ===========================================================================

  // --- layout -----------------------------------------------------------
  // "classic" keeps the Commands / Detail / Console grid on screen at all
  // times, which is what this UI has always done. "focus" hides the grid and
  // opens the Ask panel as the whole surface, with everything else reachable
  // from the same Menu it was already in.
  //
  // The important property, and the reason this is safe: NO FEATURE EXISTS IN
  // ONE LAYOUT AND NOT THE OTHER. Focus only changes what is on screen by
  // default. Anything that would only work in classic would turn a
  // presentation preference into a trap.
  let currentLayout = "classic";

  function applyLayout(mode) {
    currentLayout = mode === "focus" ? "focus" : "classic";
    document.body.classList.toggle("layout--focus", currentLayout === "focus");
    const btn = qs("#btn-layout-switch");
    const label = qs("#layout-switch-label");
    if (btn) btn.dataset.layout = currentLayout;
    if (label) label.textContent = currentLayout === "focus" ? "Focus" : "Classic";
    // Focus leads with the conversation, so the Ask panel is the surface
    // rather than an overlay you open. Opening it here (idempotently) is what
    // makes the switch feel like a layout change instead of a blank screen.
    //
    // BUGFIX: the Setup wizard's own "ui_mode" step lets you pick Focus or
    // Classic right there in the wizard (see renderSetupStep's `ui_mode`
    // branch below), which calls applyLayout() while the wizard is still
    // open — this used to open the Ask panel unconditionally regardless,
    // so it appeared layered with the still-open wizard. There's nothing to
    // gain by opening it while another panel already has the whole screen —
    // whatever's underneath isn't visible either way — so this only opens
    // the Ask panel when nothing else is currently covering it. closeSetup()
    // below picks up the deferred case, opening the Ask panel once the
    // wizard actually closes if Focus was chosen from inside it.
    //
    // BUGFIX: switching back to Classic used to leave the Ask panel's
    // `hidden` attribute exactly as openAsk() left it above — false — since
    // nothing here ever closed it again. In Classic, .ask-overlay is
    // `position: fixed; inset: 0`, so a still-open-but-invisible-because-
    // inline panel from Focus mode would suddenly render as a full-screen
    // overlay on top of the grid the instant Classic came back. Closing it
    // on the way OUT of Focus is what keeps Classic's Ask panel an
    // explicit, click-to-open modal rather than something that reopens
    // itself as a side effect of the layout switch.
    const anotherOverlayOpen = setupOverlay && !setupOverlay.hidden;
    if (currentLayout === "focus" && !anotherOverlayOpen && typeof openAsk === "function") {
      try { openAsk(); } catch { /* the panel may not be built yet on first paint */ }
    } else if (currentLayout === "classic" && typeof closeAsk === "function") {
      try { closeAsk(); } catch { /* the panel may not be built yet on first paint */ }
    }
  }

  async function loadLayout() {
    try {
      const data = await Api.get("/api/ui-mode");
      applyLayout(data && data.ui_mode);
    } catch {
      // Server-side preference unreadable (jarvis CLI down, first run) — the
      // classic layout is the safe default because it is what every existing
      // user already has.
      applyLayout("classic");
    }
  }

  async function toggleLayout() {
    const next = currentLayout === "focus" ? "classic" : "focus";
    applyLayout(next);          // optimistic: the switch must feel instant
    try {
      await Api.post("/api/ui-mode", { mode: next });
    } catch (err) {
      applyLayout(next === "focus" ? "classic" : "focus");   // roll back
      toast(err.message || "Couldn't save that layout.");
    }
  }

  qs("#btn-layout-switch")?.addEventListener("click", toggleLayout);

  // --- subagents ----------------------------------------------------------
  // Active vs finished split mirrors tasks.py's own ACTIVE_STATUSES /
  // TERMINAL_STATUSES exactly (see tasks.py) — "blocked" is neither: it's
  // not running, but it's a subagent waiting on a human decision, which
  // belongs with the things worth looking at, not the finished pile.
  const SUBAGENT_ACTIVE_STATUSES = new Set(["pending", "running", "blocked"]);

  const subagentsOverlay = qs("#subagents-overlay");
  let selectedSubagent = null;
  let subagentActiveList = [];
  let subagentPollTimer = null;

  function subagentStatusClass(status) {
    if (status === "running") return "is-busy";
    if (status === "done") return "is-ok";
    if (status === "failed") return "is-error";
    if (status === "blocked") return "is-warn";
    if (status === "cancelled") return "is-muted";
    return "";
  }

  function renderSubagentRow(t) {
    const goalLine = (t.goal || "").length > 64 ? t.goal.slice(0, 64) + "\u2026" : (t.goal || "");
    return el("button", {
      class: "skills-item subagent-row" + (t.id === selectedSubagent ? " is-active" : "")
        + (t.status === "blocked" ? " is-warn" : ""),
      type: "button",
      onclick: () => selectSubagent(t.id),
    }, [
      el("div", { class: "skills-item__name" }, [
        el("span", { class: `subagent-dot ${subagentStatusClass(t.status)}` }),
        `[${t.role || "?"}] ${goalLine || "(no goal)"}`,
      ]),
      el("div", { class: "skills-item__desc" },
        `${t.status}\u2002\u00b7\u2002${t.progress || ""}\u2002\u00b7\u2002${t.steps_used || 0} step${t.steps_used === 1 ? "" : "s"}`),
    ]);
  }

  async function refreshSubagents() {
    let data;
    try {
      data = await Api.get("/api/subagents");
    } catch (e) {
      qs("#subagents-status-line").textContent = e.message || "Couldn't read subagents.";
      return;
    }
    const tasks = data.tasks || [];
    subagentActiveList = tasks.filter((t) => SUBAGENT_ACTIVE_STATUSES.has(t.status));
    const done = tasks.filter((t) => !SUBAGENT_ACTIVE_STATUSES.has(t.status)).slice(0, 10);

    qs("#subagents-status-line").textContent =
      `${data.running_now || 0} running \u00b7 max_concurrent ${data.max_concurrent ?? "\u2014"}`;

    const badge = qs("#subagents-badge");
    if (badge) {
      const n = subagentActiveList.length;
      badge.hidden = n === 0;
      badge.textContent = String(n);
    }

    const listEl = qs("#subagents-list");
    listEl.innerHTML = "";
    if (!subagentActiveList.length) {
      listEl.appendChild(el("div", { class: "skills-empty" }, "No active subagents right now."));
    } else {
      subagentActiveList.forEach((t) => listEl.appendChild(renderSubagentRow(t)));
    }

    const doneEl = qs("#subagents-list-done");
    doneEl.innerHTML = "";
    if (!done.length) {
      doneEl.appendChild(el("div", { class: "skills-empty" }, "Nothing finished yet."));
    } else {
      done.forEach((t) => doneEl.appendChild(renderSubagentRow(t)));
    }

    // Keep a selection alive across a poll tick if it's still around; drop
    // it (rather than silently pointing at stale data) if the task is gone.
    if (selectedSubagent && !tasks.some((t) => t.id === selectedSubagent)) {
      selectedSubagent = null;
      renderSubagentDetail(null);
    } else if (selectedSubagent) {
      selectSubagent(selectedSubagent, /* fromPoll */ true);
    }
  }

  function planLine(step) {
    const mark = { done: "\u2713", failed: "\u2717", skipped: "\u2013" }[step.status] || "\u25cb";
    return el("div", { class: `subagent-plan-step subagent-plan-step--${step.status || "pending"}` },
      `${mark}  ${step.text || ""}`);
  }

  function historyLine(entry) {
    const cls = entry.ok === false ? "fail" : "sys";
    const when = entry.at ? new Date(entry.at).toLocaleTimeString() : "";
    return el("div", { class: `daemon-console-line ${cls}` },
      `${when}  step ${entry.step ?? "?"}: ${entry.summary || ""}${entry.error ? "  \u2014 " + entry.error : ""}`);
  }

  function renderSubagentDetail(detail) {
    const body = qs("#subagent-detail");
    const title = qs("#subagent-detail-title");
    const statusTag = qs("#subagent-detail-status");
    const actions = qs("#subagent-detail-actions");
    if (!detail) {
      title.textContent = "Pick a subagent";
      statusTag.hidden = true;
      actions.hidden = true;
      body.innerHTML = "";
      body.appendChild(el("div", { class: "skills-empty" },
        "Nothing selected \u2014 pick one on the left, or spawn one by asking Jarvis to consult a role."));
      return;
    }
    title.textContent = `[${detail.role || "?"}] ${detail.goal || ""}`;
    statusTag.hidden = false;
    statusTag.textContent = detail.status;
    statusTag.className = `subagent-detail-status ${subagentStatusClass(detail.status)}`;
    actions.hidden = false;
    qs("#btn-subagent-cancel").disabled = !SUBAGENT_ACTIVE_STATUSES.has(detail.status);
    qs("#btn-subagent-transcript").disabled = !detail.conv_id;

    body.innerHTML = "";
    body.appendChild(el("div", { class: "subagent-detail__row" }, [
      el("span", {}, `Progress: ${detail.progress || "\u2014"}`),
      el("span", {}, `Steps: ${detail.steps_used ?? 0}${detail.max_steps ? ` / ${detail.max_steps}` : ""}`),
      detail.parent_id ? el("span", {}, `Parent: ${detail.parent_id}`) : null,
    ]));

    if (detail.error) {
      body.appendChild(el("div", { class: "jui-card jui-card--error" }, [
        el("div", { class: "jui-card__head" }, "Last error"),
        el("div", { class: "jui-card__body" }, detail.error),
      ]));
    }

    const plan = detail.plan || [];
    if (plan.length) {
      body.appendChild(el("div", { class: "skills-pane__head skills-pane__head--sub" }, [el("h3", {}, "Plan")]));
      plan.forEach((step) => body.appendChild(planLine(step)));
    }

    body.appendChild(el("div", { class: "skills-pane__head skills-pane__head--sub" }, [el("h3", {}, "Step history")]));
    const history = detail.history || [];
    if (!history.length) {
      body.appendChild(el("div", { class: "skills-empty" }, "No steps have run yet."));
    } else {
      const log = el("pre", { class: "daemon-console subagent-history" });
      history.forEach((entry) => log.appendChild(historyLine(entry)));
      body.appendChild(log);
    }

    if (detail.result) {
      body.appendChild(el("div", { class: "skills-pane__head skills-pane__head--sub" }, [el("h3", {}, "Result")]));
      body.appendChild(el("pre", { class: "daemon-console" }, detail.result));
    }
  }

  async function selectSubagent(id, fromPoll) {
    selectedSubagent = id;
    if (!fromPoll) {
      // Reflect the selection in the list immediately; don't wait on the
      // network round-trip below just to show which row is active.
      qsa(".skills-item", qs("#subagents-list")).forEach((n) => n.classList.remove("is-active"));
      qsa(".skills-item", qs("#subagents-list-done")).forEach((n) => n.classList.remove("is-active"));
    }
    let detail;
    try {
      detail = await Api.get(`/api/subagents/${encodeURIComponent(id)}`);
    } catch (e) {
      toast(e.message || "Couldn't load that subagent.");
      return;
    }
    if (selectedSubagent !== id) return; // superseded by a newer selection
    renderSubagentDetail(detail);
    qsa(".skills-item", qs("#subagents-list")).forEach((n, i) => {
      n.classList.toggle("is-active", subagentActiveList[i] && subagentActiveList[i].id === id);
    });
  }

  function cycleSubagent(delta) {
    if (!subagentActiveList.length) return;
    const i = subagentActiveList.findIndex((t) => t.id === selectedSubagent);
    const next = i === -1
      ? (delta > 0 ? 0 : subagentActiveList.length - 1)
      : (i + delta + subagentActiveList.length) % subagentActiveList.length;
    selectSubagent(subagentActiveList[next].id);
  }

  function openSubagents() {
    subagentsOverlay.hidden = false;
    refreshSubagents();
    if (subagentPollTimer) clearInterval(subagentPollTimer);
    subagentPollTimer = setInterval(refreshSubagents, 4000);
  }

  function closeSubagents() {
    subagentsOverlay.hidden = true;
    if (subagentPollTimer) { clearInterval(subagentPollTimer); subagentPollTimer = null; }
  }

  qs("#btn-subagents")?.addEventListener("click", openSubagents);
  qs("#subagents-close")?.addEventListener("click", closeSubagents);
  qs("#btn-subagents-refresh")?.addEventListener("click", refreshSubagents);
  qs("#btn-subagents-prev")?.addEventListener("click", () => cycleSubagent(-1));
  qs("#btn-subagents-next")?.addEventListener("click", () => cycleSubagent(1));
  subagentsOverlay?.addEventListener("click", (e) => {
    if (e.target === subagentsOverlay) closeSubagents();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && subagentsOverlay && !subagentsOverlay.hidden) closeSubagents();
  });

  qs("#btn-subagent-cancel")?.addEventListener("click", async () => {
    if (!selectedSubagent) return;
    try {
      await Api.post(`/api/subagents/${encodeURIComponent(selectedSubagent)}/cancel`, {});
      toast("Subagent cancelled.");
      refreshSubagents();
    } catch (e) {
      toast(e.message || "Couldn't cancel that subagent.");
    }
  });

  qs("#btn-subagent-transcript")?.addEventListener("click", async () => {
    if (!selectedSubagent) return;
    let detail;
    try {
      detail = await Api.get(`/api/subagents/${encodeURIComponent(selectedSubagent)}`);
    } catch (e) {
      toast(e.message || "Couldn't load that subagent.");
      return;
    }
    if (!detail.conv_id) {
      toast("This subagent has no transcript yet.");
      return;
    }
    closeSubagents();
    // Reuses the exact same conversation-switch the sidebar's own
    // conversation list uses (see selectConversation) — a subagent's steps
    // are ordinary exchanges in an ordinary conversation (see
    // subagents.spawn()'s docstring), so nothing new had to be built to
    // show its real tool calls, console output and thinking, only to make
    // that conversation exist in the first place. Switching here makes it
    // the active conversation; the sidebar's own conversation list is how
    // the person gets back to whatever they were working on.
    await selectConversation(detail.conv_id);
    openAsk();
  });

  // --- daemons ----------------------------------------------------------
  const daemonsOverlay = qs("#daemons-overlay");
  let selectedDaemon = null;
  let daemonPollTimer = null;

  function daemonStatusClass(status) {
    if (status === "running") return "daemon-dot daemon-dot--up";
    if (status === "crashed") return "daemon-dot daemon-dot--bad";
    if (status === "scheduled") return "daemon-dot daemon-dot--wait";
    return "daemon-dot";
  }

  function renderDaemonRow(entry) {
    const running = Boolean(entry.running);
    const actions = el("div", { class: "daemon-actions" }, [
      el("button", {
        class: "btn btn--ghost btn--sm",
        onclick: () => daemonAction(entry.id, running ? "stop" : "start"),
      }, running ? "Stop" : "Start"),
      el("button", {
        class: "btn btn--ghost btn--sm",
        onclick: () => daemonAction(entry.id, "restart"),
      }, "Restart"),
      el("button", {
        class: "btn btn--ghost btn--sm",
        onclick: () => selectDaemon(entry.id),
      }, "Console"),
    ]);
    // A built-in can be disabled but never deleted — daemons.remove refuses,
    // so offering the button would only produce an error.
    if (!entry.builtin) {
      actions.appendChild(el("button", {
        class: "btn btn--ghost btn--sm",
        onclick: () => removeDaemon(entry.id),
      }, "Remove"));
    }

    const meta = [];
    if (entry.pid) meta.push(`pid ${entry.pid}`);
    if (entry.adopted) meta.push("started outside Jarvis");
    if (entry.next_start) meta.push(`starts ${entry.next_start}`);
    if (!entry.enabled) meta.push("disabled");
    if (entry.last_error) meta.push(entry.last_error);

    return el("div", {
      class: "skill-row daemon-row" + (selectedDaemon === entry.id ? " is-selected" : ""),
    }, [
      el("div", { class: "daemon-row__main" }, [
        el("span", { class: daemonStatusClass(entry.status) }),
        el("div", { class: "daemon-row__text" }, [
          el("div", { class: "skill-row__name" }, entry.name || entry.id),
          el("div", { class: "skill-row__desc" },
            `${entry.status}${meta.length ? " \u2014 " + meta.join(", ") : ""}`),
          el("div", { class: "daemon-row__cmd" }, entry.command || ""),
        ]),
      ]),
      actions,
    ]);
  }

  async function refreshDaemons() {
    if (!daemonsOverlay || daemonsOverlay.hidden) return;
    const list = qs("#daemons-list");
    const statusLine = qs("#daemons-status-line");
    try {
      const data = await Api.get("/api/daemons");
      const entries = data.daemons || [];
      const up = entries.filter((d) => d.running).length;
      const broken = entries.filter((d) => d.status === "crashed").length;
      statusLine.textContent =
        `${up} of ${entries.length} running` + (broken ? `, ${broken} crashed` : "");
      list.innerHTML = "";
      for (const entry of entries) list.appendChild(renderDaemonRow(entry));
      if (!entries.length) {
        list.appendChild(el("div", { class: "skills-empty" }, "No services registered."));
      }
    } catch (err) {
      statusLine.textContent = "couldn't read services";
      list.innerHTML = "";
      list.appendChild(el("div", { class: "skills-empty" }, err.message || "Failed."));
    }
  }

  async function daemonAction(id, action) {
    try {
      const data = await Api.post(`/api/daemons/${encodeURIComponent(id)}/${action}`, {});
      toast(data.message || `${action}ed ${id}`, "info");
    } catch (err) {
      toast(err.message || `Couldn't ${action} ${id}.`);
    }
    // Starting is asynchronous by design (daemons.start returns as soon as the
    // supervisor is spawned, without waiting for a WebSocket handshake), so
    // one refresh now and one shortly after is what makes the row settle on
    // the real state rather than on "starting".
    refreshDaemons();
    setTimeout(refreshDaemons, 1500);
    if (selectedDaemon === id) setTimeout(refreshDaemonConsole, 1500);
  }

  async function removeDaemon(id) {
    if (!window.confirm(`Remove the '${id}' service? Its console logs stay on disk.`)) return;
    try {
      await api("DELETE", `/api/daemons/${encodeURIComponent(id)}`);
      if (selectedDaemon === id) selectedDaemon = null;
      refreshDaemons();
    } catch (err) {
      toast(err.message || "Couldn't remove that service.");
    }
  }

  async function selectDaemon(id) {
    selectedDaemon = id;
    const title = qs("#daemon-console-title");
    if (title) title.textContent = `Console \u2014 ${id}`;
    await refreshDaemonConsole();
    refreshDaemons();
  }

  async function refreshDaemonConsole() {
    const pane = qs("#daemon-console");
    if (!pane || !selectedDaemon) return;
    const picker = qs("#daemon-console-file");
    const file = picker && picker.value ? `&file=${encodeURIComponent(picker.value)}` : "";
    try {
      const data = await Api.get(
        `/api/daemons/${encodeURIComponent(selectedDaemon)}/console?lines=300${file}`);
      const lines = data.lines || [];
      pane.textContent = lines.length ? lines.join("\n") : "(no output yet)";
      pane.scrollTop = pane.scrollHeight;

      if (picker && !file) {
        const backups = data.backups || [];
        const want = ["", ...backups].join("|");
        if (picker.dataset.loaded !== want) {
          picker.dataset.loaded = want;
          picker.innerHTML = "";
          picker.appendChild(el("option", { value: "" }, "current"));
          for (const path of backups) {
            picker.appendChild(el("option", { value: path },
              path.split(/[\\/]/).pop()));
          }
        }
      }

      // stdin is only offered where it can actually work: a running daemon
      // whose definition says its process reads stdin. daemons.send_input
      // refuses otherwise, and a control that always errors is worse than no
      // control.
      const daemons = (await Api.get("/api/daemons")).daemons || [];
      const entry = daemons.find((d) => d.id === selectedDaemon);
      const row = qs("#daemon-input-row");
      if (row) row.hidden = !(entry && entry.running && entry.supports_stdin);
    } catch (err) {
      pane.textContent = err.message || "Couldn't read that console.";
    }
  }

  qs("#btn-daemon-refresh")?.addEventListener("click", () => {
    refreshDaemons();
    refreshDaemonConsole();
  });

  qs("#daemon-console-file")?.addEventListener("change", refreshDaemonConsole);

  qs("#btn-daemon-add")?.addEventListener("click", async () => {
    const id = (qs("#daemon-new-id")?.value || "").trim();
    const command = (qs("#daemon-new-command")?.value || "").trim();
    if (!id || !command) return toast("An id and a command are both required.");
    try {
      await Api.post("/api/daemons", {
        id,
        command,
        cwd: (qs("#daemon-new-cwd")?.value || "").trim(),
        stdin: Boolean(qs("#daemon-new-stdin")?.checked),
      });
      qs("#daemon-new-id").value = "";
      qs("#daemon-new-command").value = "";
      qs("#daemon-new-cwd").value = "";
      refreshDaemons();
    } catch (err) {
      toast(err.message || "Couldn't register that service.");
    }
  });

  qs("#btn-daemon-send")?.addEventListener("click", async () => {
    const input = qs("#daemon-input-text");
    const text = (input?.value || "").trim();
    if (!selectedDaemon || !text) return;
    try {
      await Api.post(`/api/daemons/${encodeURIComponent(selectedDaemon)}/input`, { text });
      input.value = "";
      setTimeout(refreshDaemonConsole, 400);
    } catch (err) {
      toast(err.message || "Couldn't send that.");
    }
  });

  qs("#daemon-input-text")?.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") qs("#btn-daemon-send")?.click();
  });

  qs("#btn-daemon-schedule")?.addEventListener("click", async () => {
    const when = (qs("#daemon-schedule-when")?.value || "").trim();
    if (!selectedDaemon) return toast("Pick a service first.");
    try {
      const data = await Api.post(
        `/api/daemons/${encodeURIComponent(selectedDaemon)}/schedule`, { when });
      toast(when ? `Will start at ${data.next_start || when}` : "Schedule cleared", "info");
      refreshDaemons();
    } catch (err) {
      toast(err.message || "Couldn't schedule that.");
    }
  });

  function openDaemons() {
    if (!daemonsOverlay) return;
    daemonsOverlay.hidden = false;
    refreshDaemons();
    if (selectedDaemon) refreshDaemonConsole();
    // A console is only useful live. Polling stops the moment the panel
    // closes — an interval left running against a closed overlay is a slow
    // leak and a pile of pointless subprocess spawns on the server.
    if (daemonPollTimer) clearInterval(daemonPollTimer);
    daemonPollTimer = setInterval(() => {
      refreshDaemons();
      refreshDaemonConsole();
    }, 4000);
  }

  function closeDaemons() {
    if (daemonsOverlay) daemonsOverlay.hidden = true;
    if (daemonPollTimer) { clearInterval(daemonPollTimer); daemonPollTimer = null; }
  }

  qs("#daemons-close")?.addEventListener("click", closeDaemons);
  daemonsOverlay?.addEventListener("click", (ev) => {
    if (ev.target === daemonsOverlay) closeDaemons();
  });

  // --- backlog ----------------------------------------------------------
  const backlogOverlay = qs("#backlog-overlay");
  const BACKLOG_COLUMNS = [
    ["idea", "Ideas"], ["todo", "To do"], ["doing", "Doing"],
    ["blocked", "Blocked"], ["done", "Done"],
  ];

  function renderBacklogCard(item) {
    const next = { idea: "todo", todo: "doing", doing: "done", blocked: "doing", done: "todo" };
    return el("div", { class: `kanban__card kanban__card--${item.priority || "normal"}` }, [
      el("div", { class: "kanban__title" }, item.title),
      item.project ? el("div", { class: "kanban__project" }, item.project) : null,
      item.blocked_on ? el("div", { class: "kanban__blocked" }, `waiting on ${item.blocked_on}`) : null,
      el("div", { class: "kanban__actions" }, [
        el("button", {
          class: "btn btn--ghost btn--sm",
          onclick: () => updateBacklog(item.id, { state: next[item.state] || "todo" }),
        }, item.state === "done" ? "Reopen" : `\u2192 ${next[item.state] || "todo"}`),
        el("button", {
          class: "btn btn--ghost btn--sm",
          onclick: () => {
            const reason = window.prompt("Blocked on what?", item.blocked_on || "");
            if (reason === null) return;
            // BUGFIX: this used to send only { blocked_on: reason }. The
            // server sets state to "blocked" for you when blocked_on is
            // non-empty (backlog.py's update()), but clearing it back to ""
            // has no matching rule — nothing ever moved the card OUT of the
            // Blocked column, so clearing the reason here left it stranded
            // there with no visible note and no obvious way back except the
            // unrelated advance button. Mirror that server rule on the way
            // out: clearing the reason on an already-blocked item unblocks
            // it too.
            const fields = { blocked_on: reason };
            if (!reason && item.state === "blocked") fields.state = "doing";
            updateBacklog(item.id, fields);
          },
        }, "Block"),
        el("button", {
          class: "btn btn--ghost btn--sm",
          onclick: () => removeBacklog(item.id),
        }, "\u00d7"),
      ]),
    ]);
  }

  async function refreshBacklog() {
    if (!backlogOverlay || backlogOverlay.hidden) return;
    const board = qs("#backlog-board");
    const statusLine = qs("#backlog-status-line");
    try {
      const data = await Api.get("/api/backlog");
      const grouped = data.board || {};
      const summary = data.summary || {};
      const blocked = (summary.blocked || []).length;
      statusLine.textContent =
        `${summary.open || 0} open` + (blocked ? `, ${blocked} blocked` : "") +
        ((summary.stale_doing || []).length ? `, ${summary.stale_doing.length} stale` : "");
      board.innerHTML = "";
      for (const [state, label] of BACKLOG_COLUMNS) {
        const items = grouped[state] || [];
        const column = el("div", { class: "kanban__col" }, [
          el("div", { class: "kanban__colhead" }, `${label} (${items.length})`),
        ]);
        for (const item of items) column.appendChild(renderBacklogCard(item));
        if (!items.length) column.appendChild(el("div", { class: "kanban__empty" }, "\u2014"));
        board.appendChild(column);
      }
    } catch (err) {
      statusLine.textContent = "couldn't read the backlog";
      board.innerHTML = "";
      board.appendChild(el("div", { class: "skills-empty" }, err.message || "Failed."));
    }
  }

  async function updateBacklog(id, fields) {
    try {
      await api("PATCH", `/api/backlog/${encodeURIComponent(id)}`, fields);
      refreshBacklog();
    } catch (err) {
      toast(err.message || "Couldn't update that item.");
    }
  }

  async function removeBacklog(id) {
    try {
      await api("DELETE", `/api/backlog/${encodeURIComponent(id)}`);
      refreshBacklog();
    } catch (err) {
      toast(err.message || "Couldn't remove that item.");
    }
  }

  qs("#btn-backlog-add")?.addEventListener("click", async () => {
    const title = (qs("#backlog-new-title")?.value || "").trim();
    if (!title) return;
    try {
      await Api.post("/api/backlog", {
        title,
        project: (qs("#backlog-new-project")?.value || "").trim(),
        state: qs("#backlog-new-state")?.value || "todo",
      });
      qs("#backlog-new-title").value = "";
      refreshBacklog();
    } catch (err) {
      toast(err.message || "Couldn't add that.");
    }
  });

  qs("#backlog-new-title")?.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") qs("#btn-backlog-add")?.click();
  });

  function openBacklog() {
    if (!backlogOverlay) return;
    backlogOverlay.hidden = false;
    refreshBacklog();
  }

  function closeBacklog() {
    if (backlogOverlay) backlogOverlay.hidden = true;
  }

  qs("#backlog-close")?.addEventListener("click", closeBacklog);
  backlogOverlay?.addEventListener("click", (ev) => {
    if (ev.target === backlogOverlay) closeBacklog();
  });

  // --- log search -------------------------------------------------------
  const logsearchOverlay = qs("#logsearch-overlay");

  async function runLogSearch() {
    const query = (qs("#logsearch-query")?.value || "").trim();
    const results = qs("#logsearch-results");
    const statusLine = qs("#logsearch-status-line");
    if (!query) return;
    results.innerHTML = "";
    results.appendChild(el("div", { class: "skills-empty" }, "Searching\u2026"));
    const params = new URLSearchParams({ q: query, context: "1", limit: "120" });
    const mode = qs("#logsearch-mode")?.value;
    if (mode) params.set("mode", mode);
    const set = qs("#logsearch-set")?.value;
    if (set) params.set("set", set);
    const path = (qs("#logsearch-path")?.value || "").trim();
    if (path) params.set("path", path);
    try {
      const data = await Api.get(`/api/log-files/search?${params.toString()}`);
      const hits = data.results || [];
      statusLine.textContent =
        `${hits.length} match(es) across ${data.files_scanned || 0} file(s)` +
        (data.truncated ? " (truncated)" : "");
      results.innerHTML = "";
      if (!hits.length) {
        results.appendChild(el("div", { class: "skills-empty" }, "Nothing matched."));
        return;
      }
      for (const hit of hits) {
        results.appendChild(el("div", { class: "logsearch-hit" }, [
          el("div", { class: "logsearch-hit__where" }, `${hit.file}:${hit.line}`),
          el("pre", { class: "logsearch-hit__text" }, hit.text),
        ]));
      }
    } catch (err) {
      statusLine.textContent = "search failed";
      results.innerHTML = "";
      results.appendChild(el("div", { class: "skills-empty" }, err.message || "Failed."));
    }
  }

  qs("#btn-logsearch")?.addEventListener("click", runLogSearch);
  qs("#logsearch-query")?.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") runLogSearch();
  });

  function openLogSearch() {
    if (!logsearchOverlay) return;
    logsearchOverlay.hidden = false;
    qs("#logsearch-query")?.focus();
  }

  function closeLogSearch() {
    if (logsearchOverlay) logsearchOverlay.hidden = true;
  }

  qs("#logsearch-close")?.addEventListener("click", closeLogSearch);
  logsearchOverlay?.addEventListener("click", (ev) => {
    if (ev.target === logsearchOverlay) closeLogSearch();
  });

  // --- notifications -------------------------------------------------------
  // Every notification Jarvis has ever sent (reminders, task-done, plain
  // notify-send), newest first, backed by the durable inbox on disk (see
  // /api/notifications/history and notifier.history() server-side) rather
  // than anything kept in the browser — so it survives a tab close, a
  // browser restart, or the machine rebooting, which "persist restarts"
  // means here. The panel never acknowledges anything it reads (that stays
  // /api/notifications's job, driven by the tick loop's toasts/OS
  // notifications), so opening it can't make an unread item vanish before
  // the person has actually seen it delivered live.
  const notificationsOverlay = qs("#notifications-overlay");
  const notificationsFab = qs("#btn-notifications-fab");
  const notificationsBadge = qs("#notifications-fab-badge");
  let notifUnseenCount = 0;

  function notifKindClass(note) {
    if (note.failed) return "notif-card notif-card--failed";
    if (note.kind === "reminder") return "notif-card notif-card--reminder";
    if (note.kind === "task") return "notif-card notif-card--task";
    return "notif-card";
  }

  function notifTimestamp(note) {
    const raw = note.created_at || note.ts || "";
    return raw ? raw.replace("T", " ").slice(0, 19) : "";
  }

  function renderNotifCard(note) {
    return el("div", { class: notifKindClass(note) }, [
      el("div", { class: "notif-card__head" }, [
        el("div", { class: "notif-card__title" }, note.title || "Jarvis"),
        el("div", { class: "notif-card__meta" }, notifTimestamp(note)),
      ]),
      note.message ? el("div", { class: "notif-card__body" }, note.message) : null,
      el("div", { class: "notif-card__kind" },
        `${note.kind || "notify"}${note.failed ? " \u2014 failed" : ""}`),
    ]);
  }

  async function refreshNotifications() {
    if (!notificationsOverlay || notificationsOverlay.hidden) return;
    const list = qs("#notifications-list");
    const statusLine = qs("#notifications-status-line");
    try {
      const data = await Api.get("/api/notifications/history?limit=200");
      const items = data.notifications || [];
      statusLine.textContent = items.length
        ? `${items.length} notification${items.length === 1 ? "" : "s"}, newest first`
        : "everything ever sent, persisted on disk";
      list.innerHTML = "";
      if (!items.length) {
        list.appendChild(el("div", { class: "skills-empty" }, "Nothing sent yet."));
        return;
      }
      for (const note of items) list.appendChild(renderNotifCard(note));
    } catch (err) {
      statusLine.textContent = "couldn't read notifications";
      list.innerHTML = "";
      list.appendChild(el("div", { class: "skills-empty" }, err.message || "Failed."));
    }
  }

  function updateNotifBadge() {
    if (!notificationsBadge) return;
    notificationsBadge.hidden = notifUnseenCount <= 0;
    notificationsBadge.textContent = notifUnseenCount > 99 ? "99+" : String(notifUnseenCount);
  }

  // Called for every notification pushed live over the websocket (see
  // handleWsMessage's "notifications" case) so the badge and, if the panel
  // happens to be open, the list itself stay current without waiting for
  // the next manual refresh.
  function recordLiveNotifications(items) {
    if (!items.length) return;
    if (notificationsOverlay && !notificationsOverlay.hidden) {
      refreshNotifications();
    } else {
      notifUnseenCount += items.length;
      updateNotifBadge();
    }
  }

  function openNotifications() {
    if (!notificationsOverlay) return;
    notificationsOverlay.hidden = false;
    notifUnseenCount = 0;
    updateNotifBadge();
    refreshNotifications();
  }

  function closeNotifications() {
    if (notificationsOverlay) notificationsOverlay.hidden = true;
  }

  notificationsFab?.addEventListener("click", openNotifications);
  qs("#btn-notifications-refresh")?.addEventListener("click", refreshNotifications);
  qs("#notifications-close")?.addEventListener("click", closeNotifications);
  notificationsOverlay?.addEventListener("click", (ev) => {
    if (ev.target === notificationsOverlay) closeNotifications();
  });

  // --- setup / onboarding -----------------------------------------------
  const setupOverlay = qs("#setup-overlay");

  function renderSetupStep(step) {
    const icons = { ok: "\u2713", todo: "!", optional: "\u00b7" };
    const body = [
      el("div", { class: "setup-step__head" }, [
        el("span", { class: `setup-step__icon setup-step__icon--${step.status}` },
          icons[step.status] || "?"),
        el("div", { class: "setup-step__title" }, step.title),
      ]),
      el("div", { class: "setup-step__detail" }, step.detail || ""),
    ];
    if (step.status !== "ok") {
      body.push(el("div", { class: "setup-step__why" }, step.why || ""));
      if (step.action) {
        body.push(el("code", { class: "setup-step__action" }, step.action));
      }
    }
    // The layout step is the one thing the wizard can actually DO from here,
    // so it gets real buttons rather than a command to copy.
    if (step.id === "ui_mode" && Array.isArray(step.choices)) {
      const row = el("div", { class: "setup-step__choices" });
      for (const choice of step.choices) {
        row.appendChild(el("button", {
          class: "btn " + (currentLayout === choice.value ? "btn--primary" : "btn--ghost") + " btn--sm",
          title: choice.hint || "",
          onclick: async () => {
            try {
              await Api.post("/api/ui-mode", { mode: choice.value });
              applyLayout(choice.value);
              refreshSetup();
            } catch (err) {
              toast(err.message || "Couldn't set that layout.");
            }
          },
        }, choice.label));
      }
      body.push(row);
    }
    return el("div", { class: `setup-step setup-step--${step.status}` }, body);
  }

  async function refreshSetup() {
    if (!setupOverlay || setupOverlay.hidden) return;
    const container = qs("#setup-steps");
    const statusLine = qs("#setup-status-line");
    try {
      const data = await Api.get("/api/onboarding");
      statusLine.textContent = data.ready
        ? "everything required is in place"
        : `still needed: ${(data.blocking || []).join(", ")}`;
      container.innerHTML = "";
      for (const step of data.steps || []) container.appendChild(renderSetupStep(step));
      if (data.ready && !data.completed) {
        // Marking it complete is what stops it opening by itself next time.
        Api.post("/api/onboarding/complete", {}).catch(() => {});
      }
    } catch (err) {
      statusLine.textContent = "couldn't read setup state";
      container.innerHTML = "";
      container.appendChild(el("div", { class: "skills-empty" }, err.message || "Failed."));
    }
  }

  function openSetup() {
    if (!setupOverlay) return;
    setupOverlay.hidden = false;
    refreshSetup();
  }

  function closeSetup() {
    if (setupOverlay) setupOverlay.hidden = true;
    // Picks up the deferral from applyLayout() above: if Focus was chosen
    // from inside the wizard, the Ask panel was held back rather than
    // opened underneath the still-open wizard. Now that the wizard is
    // actually gone, open it — same "don't leave Focus on a blank screen"
    // reasoning applyLayout() itself follows.
    if (currentLayout === "focus" && askOverlay?.hidden && typeof openAsk === "function") {
      try { openAsk(); } catch { /* the panel may not be built yet on first paint */ }
    }
  }

  qs("#setup-close")?.addEventListener("click", closeSetup);
  qs("#btn-setup-skip")?.addEventListener("click", async () => {
    try { await Api.post("/api/onboarding/skip", {}); } catch { /* best effort */ }
    closeSetup();
  });
  setupOverlay?.addEventListener("click", (ev) => {
    if (ev.target === setupOverlay) closeSetup();
  });

  // Opens by itself exactly once: on a machine that has never been set up and
  // has never skipped. `should_prompt` is server-side state (see
  // onboarding.should_prompt), not a cookie, so it is the same answer in
  // every browser.
  async function maybeOpenOnboarding() {
    try {
      const data = await Api.get("/api/onboarding");
      if (data && data.should_prompt) openSetup();
    } catch { /* first run with the CLI unreachable — doctor covers that */ }
  }

  // --- menu wiring ------------------------------------------------------
  qs("#menu-item-daemons")?.addEventListener("click", () => { closePanelMenu(); openDaemons(); });
  qs("#menu-item-backlog")?.addEventListener("click", () => { closePanelMenu(); openBacklog(); });
  qs("#menu-item-logsearch")?.addEventListener("click", () => { closePanelMenu(); openLogSearch(); });
  qs("#menu-item-setup")?.addEventListener("click", () => { closePanelMenu(); openSetup(); });

  // Escape closes whichever of these is on top, matching the existing panels.
  document.addEventListener("keydown", (ev) => {
    if (ev.key !== "Escape") return;
    if (setupOverlay && !setupOverlay.hidden) return closeSetup();
    if (logsearchOverlay && !logsearchOverlay.hidden) return closeLogSearch();
    if (backlogOverlay && !backlogOverlay.hidden) return closeBacklog();
    if (daemonsOverlay && !daemonsOverlay.hidden) return closeDaemons();
  });

  loadLayout();
  maybeOpenOnboarding();


})();