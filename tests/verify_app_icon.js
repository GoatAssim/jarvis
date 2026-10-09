// web/public/app-icon.js (L.48): the browser-tab icon that follows the skin's
// logo and colour and switches to a "working" variant while a task runs.
//
// Two halves, both without a browser and without npm install:
//   1. the pure helpers (JarvisIcon._pure) loaded into a bare `window` with vm,
//      the way verify_daemons_panel.js does it;
//   2. the DOM half, against a tiny fake document -- enough of one to prove the
//      <link> is replaced, that busy swaps the picture, that a skin change is
//      picked up, and that the frame ticker starts and stops.
//
//   node tests/verify_app_icon.js
const fs = require("fs"), path = require("path"), vm = require("vm");
const SRC = fs.readFileSync(path.join(__dirname, "..", "web", "public", "app-icon.js"), "utf8");
let passed = 0; const failed = [];
function check(name, cond, detail) { if (cond) passed++; else failed.push(name + (detail ? " - " + detail : "")); }

// ---------------------------------------------------------------- 1. pure
const win = {}; win.window = win;
vm.runInNewContext(SRC, win, { filename: "app-icon.js" });
const I = win.JarvisIcon, P = I && I._pure;
check("module exposes _pure and loads without a document", !!P && typeof P.buildSvg === "function");

check("cleanColor: hex passes (lower-cased)", P.cleanColor("#4FD8FF", "#000000") === "#4fd8ff");
check("cleanColor: short hex expands", P.cleanColor("#0af", "#000000") === "#00aaff");
check("cleanColor: rgb() becomes hex", P.cleanColor("rgb(79, 216, 255)", "#000000") === "#4fd8ff");
check("cleanColor: rgba() works, channels are clamped", P.cleanColor("rgba(300, 0, 5, .3)", "#000000") === "#ff0005");
check("cleanColor: whitespace is trimmed", P.cleanColor("  #123456 ", "#000000") === "#123456");
check("cleanColor: garbage / empty / null fall back", P.cleanColor("banana", "#111111") === "#111111"
  && P.cleanColor("", "#111111") === "#111111" && P.cleanColor(null, "#111111") === "#111111");

const pal = P.resolvePalette((n) => ({ "--accent": "#ff00aa", "--bg": "rgb(1, 2, 3)" }[n] || ""));
check("resolvePalette: reads the accent", pal["--accent"] === "#ff00aa");
check("resolvePalette: converts rgb()", pal["--bg"] === "#010203");
check("resolvePalette: anything missing gets the stock default", pal["--accent-secondary"] === P.DEFAULTS["--accent-secondary"]);
check("resolvePalette: no reader at all still returns six valid colours",
  Object.values(P.resolvePalette(null)).length === 6 && Object.values(P.resolvePalette(null)).every((c) => /^#[0-9a-f]{6}$/.test(c)));

check("resolveVars: replaces var(--x)", P.resolveVars('<c stroke="var(--accent)"/>', pal) === '<c stroke="#ff00aa"/>');
check("resolveVars: replaces var(--x, fallback) and several in one string",
  P.resolveVars("var(--accent, red) var(--bg)", pal) === "#ff00aa #010203");
check("resolveVars: an unknown variable becomes the accent, never an unresolved var()",
  P.resolveVars("var(--nope)", pal) === "#ff00aa");
check("resolveVars: tolerates null", P.resolveVars(null, pal) === "");

check("stripUnsafe: scripts are removed", !/script/i.test(P.stripUnsafe('<g><script>alert(1)</script><circle r="1"/></g>')));
check("stripUnsafe: inline handlers are removed", !/onload|onclick/i.test(P.stripUnsafe('<circle onclick="x()" r="1"/>')));
check("stripUnsafe: foreignObject is removed", !/foreignObject/i.test(P.stripUnsafe("<foreignObject><div/></foreignObject>")));
check("stripUnsafe: a remote href is dropped", !/https?:/.test(P.stripUnsafe('<image href="https://evil.example/x.png"/>')));
check("stripUnsafe: an embedded PNG data URI stays", /data:image\/png/.test(P.stripUnsafe('<image href="data:image/png;base64,AAAA"/>')));
check("stripUnsafe: a same-document #ref stays", /#a/.test(P.stripUnsafe('<use href="#a"/>')));

const DEFAULT_BRAND = '<circle cx="20" cy="20" r="18" class="brand-mark__ring"/>'
  + '<circle cx="20" cy="20" r="11" class="brand-mark__ring brand-mark__ring--in"/>'
  + '<circle cx="20" cy="20" r="3" class="brand-mark__core"/>';
const VERITY_BRAND = '<circle cx="20" cy="20" r="18" class="brand-mark__ring"/>'
  + '<circle cx="14" cy="17" r="2.2" class="brand-mark__core"/>'
  + '<path d="M 12 24 Q 20 30 28 24" fill="none" stroke="var(--accent)" stroke-width="1.8"/>';

const idle = P.buildSvg({ brand: DEFAULT_BRAND, palette: pal, busy: false });
check("buildSvg: a complete standalone svg", idle.startsWith("<svg ") && idle.endsWith("</svg>") && idle.includes('xmlns="http://www.w3.org/2000/svg"'));
check("buildSvg: carries the skin colour into the class rules", idle.includes("stroke:#ff00aa") && idle.includes("fill:#ff00aa"));
check("buildSvg: no var() survives (an isolated image cannot resolve it)", !/var\(/.test(P.buildSvg({ brand: VERITY_BRAND, palette: pal })));
check("buildSvg: the logo markup is the skin's own", P.buildSvg({ brand: VERITY_BRAND, palette: pal }).includes('cx="14" cy="17"'));
check("buildSvg: idle has no working decoration", !idle.includes("stroke-dasharray=\"26 94\""));
const busy0 = P.buildSvg({ brand: DEFAULT_BRAND, palette: pal, busy: true, frame: 0 });
const busy3 = P.buildSvg({ brand: DEFAULT_BRAND, palette: pal, busy: true, frame: 3 });
check("buildSvg: busy is a different picture from idle", busy0 !== idle && busy0.includes("stroke-dasharray=\"26 94\""));
check("buildSvg: busy still carries the skin's own mark", busy0.includes('r="11"') && busy0.includes("brand-mark__core"));
check("buildSvg: busy follows the skin accent, not a fixed secondary (a blue one on Verity)",
  busy0.includes('stroke="' + pal["--accent"] + '"') && !busy0.includes(pal["--accent-secondary"]));
check("buildSvg: no background rectangle -- the tab icon is transparent (it was a black tile)",
  !/<rect/i.test(idle) && !/<rect/i.test(busy0));
check("buildSvg: the busy badge has no dark disc behind it", !busy0.includes(pal["--bg"]) && busy0.includes('fill-rule="evenodd"'));
check("buildSvg: each frame turns the arc", busy0 !== busy3 && busy3.includes("rotate(135 20 20)"), busy3.slice(0, 0));
check("buildSvg: frames wrap around, negative or huge frames never throw",
  P.buildSvg({ brand: "", palette: pal, busy: true, frame: P.FRAME_COUNT }) === P.buildSvg({ brand: "", palette: pal, busy: true, frame: 0 })
  && typeof P.buildSvg({ brand: "", palette: pal, busy: true, frame: -3 }) === "string");
check("buildSvg: works with nothing at all", P.buildSvg().startsWith("<svg "));
check("buildSvg: a different accent gives a different picture",
  idle !== P.buildSvg({ brand: DEFAULT_BRAND, palette: P.resolvePalette((n) => (n === "--accent" ? "#00ff00" : "")) }));

const uri = P.toDataUri(idle);
check("toDataUri: an svg data URI that round-trips", uri.startsWith("data:image/svg+xml,") && decodeURIComponent(uri.slice("data:image/svg+xml,".length)) === idle);
check("toDataUri: nothing in it can break out of an html attribute", !/["<>]/.test(uri));

const s1 = P.signature({ brand: DEFAULT_BRAND, palette: pal, busy: false });
check("signature: stable for equal input", s1 === P.signature({ brand: DEFAULT_BRAND, palette: pal, busy: false }));
check("signature: changes with the logo", s1 !== P.signature({ brand: VERITY_BRAND, palette: pal, busy: false }));
check("signature: changes with the colour", s1 !== P.signature({ brand: DEFAULT_BRAND, palette: P.resolvePalette(() => "#010101"), busy: false }));
check("signature: changes with busy and with the frame",
  s1 !== P.signature({ brand: DEFAULT_BRAND, palette: pal, busy: true, frame: 0 })
  && P.signature({ brand: DEFAULT_BRAND, palette: pal, busy: true, frame: 0 }) !== P.signature({ brand: DEFAULT_BRAND, palette: pal, busy: true, frame: 1 }));
check("signature: an idle icon ignores the frame (no needless rewrites)",
  P.signature({ brand: "x", palette: pal, busy: false, frame: 0 }) === P.signature({ brand: "x", palette: pal, busy: false, frame: 5 }));

// ---------------------------------------------------------------- 2. DOM half
function makeDom(opts) {
  const o = opts || {};
  const intervals = new Map(); let nextId = 1;
  const timeouts = new Map();
  const head = { children: [], contains(n) { return this.children.includes(n); }, appendChild(n) { n.parentNode = this; this.children.push(n); return n; },
    replaceChild(n, old) { const i = this.children.indexOf(old); this.children[i] = n; n.parentNode = this; old.parentNode = null; return n; } };
  const classes = new Set(o.classes || []);
  const style = { "--accent": "#4fd8ff", "--accent-soft": "#2ea9d6", "--accent-secondary": "#f2b544", "--bg": "#04070d" };
  const mark = { innerHTML: DEFAULT_BRAND };
  const observers = [];
  const mkLink = () => ({ tagName: "LINK", rel: "", id: "", type: "", href: "", parentNode: null });
  const first = Object.assign(mkLink(), { rel: "icon", id: "app-icon", href: "data:image/svg+xml,stock" });
  head.appendChild(first);
  const documentElement = { classList: { contains: (c) => classes.has(c), add: (c) => classes.add(c), remove: (c) => classes.delete(c) } };
  const doc = {
    readyState: "complete", head, documentElement,
    getElementById: (id) => head.children.find((n) => n.id === id) || null,
    querySelector: (sel) => (sel === ".brand-mark" ? mark : sel.startsWith("link") ? head.children[0] || null : null),
    querySelectorAll: (sel) => (sel === ".brand-mark" ? [mark] : []),
    createElement: () => mkLink(),
    addEventListener() {},
  };
  const w = {
    document: doc,
    getComputedStyle: () => ({ getPropertyValue: (n) => style[n] || "" }),
    matchMedia: () => ({ matches: !!o.reducedMotion }),
    MutationObserver: function (cb) { this.cb = cb; this.observe = () => {}; observers.push(this); },
    setInterval: (fn, ms) => { const id = nextId++; intervals.set(id, { fn, ms }); return id; },
    clearInterval: (id) => { intervals.delete(id); },
    setTimeout: (fn) => { const id = nextId++; timeouts.set(id, fn); return id; },
  };
  w.window = w;
  return { w, head, style, mark, classes, intervals, timeouts, observers,
    flush() { for (const [id, fn] of [...timeouts]) { timeouts.delete(id); fn(); } },
    href() { return head.children[0].href; },
    tick() { for (const t of intervals.values()) t.fn(); } };
}
const dec = (href) => decodeURIComponent(href.replace("data:image/svg+xml,", ""));

{
  const d = makeDom();
  vm.runInNewContext(SRC, d.w, { filename: "app-icon.js" });
  check("dom: on load the stock <link> is replaced by the skin's icon", d.href().startsWith("data:image/svg+xml,") && d.href() !== "data:image/svg+xml,stock");
  check("dom: there is exactly one icon <link> and it keeps the id", d.head.children.length === 1 && d.head.children[0].id === "app-icon" && d.head.children[0].type === "image/svg+xml");
  check("dom: the icon uses the live accent", dec(d.href()).includes("#4fd8ff"));
  check("dom: not busy at the start", d.w.JarvisIcon.isBusy() === false && d.intervals.size === 0);

  const idleHref = d.href();
  d.w.JarvisIcon.setBusy(true, "run");
  check("dom: setBusy(true) swaps to a different picture at once", d.href() !== idleHref && dec(d.href()).includes("stroke-dasharray=\"26 94\""));
  check("dom: ...and starts the frame ticker", d.intervals.size === 1);
  const f0 = d.href(); d.tick();
  check("dom: each tick turns the arc", d.href() !== f0);
  for (let i = 0; i < 7; i++) d.tick();
  check("dom: after a full turn it is back on frame 0", d.href() === f0);
  d.w.JarvisIcon.setBusy(true, "backup");
  d.w.JarvisIcon.setBusy(false, "run");
  check("dom: busy lasts while ANY reason is active", d.w.JarvisIcon.isBusy() === true && d.intervals.size === 1);
  d.w.JarvisIcon.setBusy(false, "backup");
  check("dom: when the last reason ends the idle icon returns and the ticker stops",
    d.w.JarvisIcon.isBusy() === false && d.intervals.size === 0 && d.href() === idleHref);
  d.w.JarvisIcon.setBusy(false, "never-set");
  check("dom: clearing a reason that was never set is harmless", d.href() === idleHref);

  const before = d.head.children[0];
  d.observers[0].cb([{ type: "attributes" }]); d.flush();
  check("dom: a change event that changes nothing does not rewrite the <link>", d.head.children[0] === before);
  d.w.JarvisIcon.refresh();
  check("dom: refresh() forces a rebuild", d.head.children[0] !== before);

  // a skin change: the accent moves and the logo is swapped
  d.style["--accent"] = "#ff3b7a"; d.mark.innerHTML = VERITY_BRAND;
  d.observers[0].cb([{ type: "attributes" }]); d.flush();
  const after = dec(d.href());
  check("dom: a skin change is picked up (new colour)", after.includes("#ff3b7a") && !after.includes("#4fd8ff"));
  check("dom: ...and the new logo", after.includes('cx="14" cy="17"'));
  check("dom: ...with no unresolved var()", !/var\(/.test(after));
}

{
  const d = makeDom({ classes: ["jui-no-motion"] });
  vm.runInNewContext(SRC, d.w, { filename: "app-icon.js" });
  d.w.JarvisIcon.setBusy(true, "run");
  check("dom: with Animations switched off busy still changes the picture but never ticks",
    dec(d.href()).includes("stroke-dasharray=\"26 94\"") && d.intervals.size === 0);
}
{
  const d = makeDom({ reducedMotion: true });
  vm.runInNewContext(SRC, d.w, { filename: "app-icon.js" });
  d.w.JarvisIcon.setBusy(true, "run");
  check("dom: prefers-reduced-motion also holds the icon still", d.intervals.size === 0 && dec(d.href()).includes("stroke-dasharray=\"26 94\""));
  d.w.JarvisIcon.setBusy(false, "run");
  check("dom: and it still returns to idle", !dec(d.href()).includes("stroke-dasharray=\"26 94\""));
}
{
  // The skin's motion switch flips WHILE busy: the ticker follows it.
  const d = makeDom();
  vm.runInNewContext(SRC, d.w, { filename: "app-icon.js" });
  d.w.JarvisIcon.setBusy(true, "run");
  check("dom: ticker running", d.intervals.size === 1);
  d.classes.add("jui-no-motion"); d.observers[0].cb([{ type: "attributes" }]);
  check("dom: switching Animations off mid-run stops the ticker", d.intervals.size === 0);
  d.classes.delete("jui-no-motion"); d.observers[0].cb([{ type: "attributes" }]);
  check("dom: switching them back on restarts it", d.intervals.size === 1);
}

// ---------------------------------------------------------------- 3. wiring (static)
const root = path.join(__dirname, "..", "web", "public");
const html = fs.readFileSync(path.join(root, "index.html"), "utf8");
const app = fs.readFileSync(path.join(root, "app.js"), "utf8");
check("wiring: index.html loads app-icon.js before app.js", html.indexOf('src="app-icon.js"') > 0 && html.indexOf('src="app-icon.js"') < html.indexOf('src="app.js"'));
check("wiring: the stock <link rel=icon> is no longer the empty data: URI", !html.includes('<link rel="icon" href="data:,">') && /<link rel="icon" id="app-icon"/.test(html));
check("wiring: setRunning tells the icon", /function setRunning\(running\)\s*\{[\s\S]{0,700}JarvisIcon\.setBusy\(!!running, "run"\)/.test(app));
check("wiring: the call is guarded (the icon is cosmetic)", /window\.JarvisIcon && typeof window\.JarvisIcon\.setBusy === "function"/.test(app));

console.log(`${passed} passed, ${failed.length} failed`);
for (const f of failed) console.log("FAILED:", f);
process.exit(failed.length ? 1 : 0);
