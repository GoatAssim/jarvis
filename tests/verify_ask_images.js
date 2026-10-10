// Pictures in the Ask Jarvis panel: the browser helpers, the server's checking and staging, and the wiring.
//
//   node tests/verify_ask_images.js      (no npm install, no browser)
//
// web/public/ask-images.js is loaded into a bare `window` with vm (the pure helpers: list limits,
// picture URLs, what a saved exchange draws). web/ask-images.js (the server's own check and temp-folder
// staging) is imported as the module server.js imports. app.js / index.html / style.css / server.js are
// checked as text for the wiring that cannot be run without a browser.

const fs = require("fs");
const os = require("os");
const path = require("path");
const vm = require("vm");

const ROOT = path.resolve(__dirname, "..");
let passed = 0;
const failed = [];
function check(name, cond, detail) {
  if (cond) passed += 1;
  else failed.push(name + (detail ? " - " + detail : ""));
}

// ---- 1. browser helpers ---------------------------------------------------------------
const win = {};
vm.runInNewContext(fs.readFileSync(path.join(ROOT, "web/public/ask-images.js"), "utf8"), { window: win });
const AI = win.JarvisAskImages;
check("helpers load", !!AI && typeof AI.addFiles === "function");

const file = (name, size, type) => ({ name, size, type });
let r = AI.addFiles([], [file("a.png", 100, "image/png"), file("b.jpg", 100, "image/jpeg")]);
check("two pictures are accepted", r.list.length === 2 && r.rejected.length === 0);
r = AI.addFiles(r.list, [file("c.gif", 1, "image/gif"), file("d.webp", 1, "image/webp")]);
check("the fourth is refused with the count limit, the list stays at three",
  r.list.length === 3 && r.rejected.length === 1 && /more than 3/.test(r.rejected[0].reason), JSON.stringify(r.rejected));
r = AI.addFiles([], [file("big.png", AI.MAX_BYTES + 1, "image/png")]);
check("an oversized picture is refused", r.list.length === 0 && /8 MB/.test(r.rejected[0].reason));
r = AI.addFiles([], [file("doc.pdf", 10, "application/pdf"), file("x.svg", 10, "image/svg+xml")]);
check("a pdf and an svg are refused", r.list.length === 0 && r.rejected.length === 2);
r = AI.addFiles([], [file("pasted.png", 10, "")]);
check("a file with no type is judged by its extension", r.list.length === 1);
r = AI.addFiles([], [file("noext", 10, "")]);
check("...and refused when that is no picture either", r.list.length === 0);
const before = [file("a.png", 1, "image/png")];
AI.addFiles(before, [file("b.png", 1, "image/png")]);
check("addFiles never mutates its input", before.length === 1);
check("rejectedMessage joins reasons and is empty for none",
  AI.rejectedMessage([]) === "" && /a\.pdf isn't a PNG/.test(AI.rejectedMessage([{ name: "a.pdf", reason: "isn't a PNG, JPEG, GIF or WebP picture" }])));
check("base64Of strips the data-URL header", AI.base64Of("data:image/png;base64,QUJD") === "QUJD" && AI.base64Of("nope") === "");

const GOOD = "ci_" + "ab12".repeat(6) + ".png";
check("pictureUrl only for a minted id",
  AI.pictureUrl(GOOD) === "/api/conv-images/" + GOOD && AI.pictureUrl("../x.png") === "" &&
  AI.pictureUrl("ci_zz.png") === "" && AI.pictureUrl(null) === "" && AI.pictureUrl(GOOD + "/..") === "");
const ex = {
  user: "look\n\n[Pictures the sender attached...]\nPicture 1 (a.png): A cat.",
  shown: "look",
  images: [{ id: GOOD, name: "a.png", size: 5 }, { id: "../evil.png", name: "x" }],
};
check("picturesOf keeps valid ids only", AI.picturesOf(ex).length === 1 && AI.picturesOf(ex)[0].src === "/api/conv-images/" + GOOD);
check("textOf draws what was typed, not the description note", AI.textOf(ex) === "look");
check("a picture-only message draws no words after a reload",
  AI.textOf({ user: "(sent a picture)\n\n[note]", shown: "(sent a picture)", images: ex.images }) === "");
check("an exchange with no pictures draws `user` untouched", AI.textOf({ user: "plain words" }) === "plain words");
check("an exchange whose references are all invalid falls back to `user`",
  AI.textOf({ user: "keep me", shown: "x", images: [{ id: "bad" }] }) === "keep me");
check("junk never throws", AI.textOf(null) === "" && AI.picturesOf(undefined).length === 0 && AI.picturesOf({ images: "x" }).length === 0);

// ---- 2. the server's checking and staging ------------------------------------------------------
const PNG = Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]), Buffer.alloc(40, 48)]);
const JPG = Buffer.concat([Buffer.from([0xff, 0xd8, 0xff, 0xe0]), Buffer.alloc(40, 48)]);
const GIF = Buffer.concat([Buffer.from("GIF89a"), Buffer.alloc(20, 48)]);
const WEBP = Buffer.concat([Buffer.from("RIFF"), Buffer.alloc(4), Buffer.from("WEBP"), Buffer.alloc(20, 48)]);
const b64 = (b) => b.toString("base64");

(async () => {
  const S = await import(path.join(ROOT, "web/ask-images.js"));
  check("limits: three pictures, 8 MB", S.MAX_IMAGES === 3 && S.MAX_IMAGE_BYTES === 8 * 1024 * 1024);
  check("sniff: png jpg gif webp", [PNG, JPG, GIF, WEBP].map(S.sniffImageKind).join() === "png,jpg,gif,webp");
  check("sniff: text and tiny buffers are nothing", S.sniffImageKind(Buffer.from("hello world, not a picture")) === "" && S.sniffImageKind(Buffer.alloc(3)) === "");

  let p = S.parseAskImages(undefined);
  check("nothing attached is fine", p.ok && p.images.length === 0);
  p = S.parseAskImages([]);
  check("an empty list is fine", p.ok && p.images.length === 0);
  p = S.parseAskImages("not a list");
  check("a non-list is refused", !p.ok);
  p = S.parseAskImages([{ name: "a.png", data: b64(PNG) }, { name: "b.jpg", data: b64(JPG) }]);
  check("two real pictures pass with their kinds", p.ok && p.images.map((i) => i.kind).join() === "png,jpg");
  p = S.parseAskImages([{ name: "x.png", data: b64(PNG) }, { name: "x", data: b64(PNG) }, { name: "x", data: b64(PNG) }, { name: "x", data: b64(PNG) }]);
  check("four pictures are refused", !p.ok && /At most 3/.test(p.error), p.error);
  p = S.parseAskImages([{ name: "fake.png", data: b64(Buffer.from("MZ this is an exe pretending to be a picture")) }]);
  check("bytes that are not a picture are refused whatever the name says", !p.ok && /isn't a PNG/.test(p.error), p.error);
  p = S.parseAskImages([{ name: "big.png", data: "A".repeat(12 * 1024 * 1024) }]);
  check("an oversized value is refused before it is decoded", !p.ok && /8 MB/.test(p.error), p.error);
  p = S.parseAskImages([{ name: "big.png", data: b64(Buffer.concat([PNG, Buffer.alloc(8 * 1024 * 1024)])) }]);
  check("a decoded size over the limit is refused", !p.ok && /8 MB/.test(p.error), p.error);
  p = S.parseAskImages([{ name: "e.png", data: "" }]);
  check("an empty picture is refused", !p.ok && /empty/.test(p.error), p.error);
  p = S.parseAskImages([null]);
  check("a null entry is refused", !p.ok);
  p = S.parseAskImages([{ name: "a.png" }]);
  check("an entry with no data is refused", !p.ok);
  check("cleanName drops directories and control characters",
    S.cleanName("..\\..\\evil/../x\u0000y.png") === "x y.png" && S.cleanName("") === "picture" && S.cleanName(null) === "picture");
  check("cleanName clips long names", S.cleanName("a".repeat(300)).length === 80);

  const base = fs.mkdtempSync(path.join(os.tmpdir(), "askimg-test-"));
  const parsed = S.parseAskImages([{ name: "../shot.png", data: b64(PNG) }, { name: "p.jpg", data: b64(JPG) }]);
  const staged = await S.stageAskImages(parsed.images, "what is this?", base);
  const manifest = JSON.parse(fs.readFileSync(staged.manifestPath, "utf8"));
  check("the manifest lists each file with its label and the typed words",
    manifest.files.length === 2 && manifest.files[0].name === "shot.png" && manifest.shown === "what is this?", JSON.stringify(manifest));
  check("each staged file's extension comes from its bytes",
    manifest.files[0].path.endsWith(".png") && manifest.files[1].path.endsWith(".jpg"));
  check("the staged bytes are the picture", fs.readFileSync(manifest.files[0].path).equals(PNG));
  check("everything is under the private folder", manifest.files.every((f) => path.dirname(f.path) === staged.dir) && path.dirname(staged.manifestPath) === staged.dir);
  if (process.platform !== "win32") {
    check("the folder and files are private", (fs.statSync(staged.dir).mode & 0o077) === 0 && (fs.statSync(manifest.files[0].path).mode & 0o077) === 0);
  }
  fs.rmSync(staged.dir, { recursive: true, force: true });
  fs.rmSync(base, { recursive: true, force: true });

  // ---- 3. wiring (text checks) ------------------------------------------------------------
  const server = fs.readFileSync(path.join(ROOT, "web/server.js"), "utf8");
  const app = fs.readFileSync(path.join(ROOT, "web/public/app.js"), "utf8");
  const html = fs.readFileSync(path.join(ROOT, "web/public/index.html"), "utf8");
  const css = fs.readFileSync(path.join(ROOT, "web/public/style.css"), "utf8");

  check("server: pictures are checked before the redo drop and before any child starts",
    server.indexOf("parseAskImages(msg.images)") > 0 &&
    server.indexOf("parseAskImages(msg.images)") < server.indexOf('"ai-drop-from"') &&
    server.indexOf("parseAskImages(msg.images)") < server.indexOf("spawnAndStream(ws, \"ask\""));
  check("server: a message of only pictures is not 'Nothing to ask'", /!text && !quote && pictures\.images\.length === 0/.test(server));
  check("server: the child gets the manifest through JARVIS_ASK_IMAGES_FILE", server.includes("extraEnv.JARVIS_ASK_IMAGES_FILE = staged.manifestPath"));
  check("server: the staged folder is removed on exit and on a failed spawn",
    server.includes("fs.rm(askImagesDir, { recursive: true, force: true })") && server.includes("if (!ws.activeChild) for (const f of askCleanups) f();"));
  check("server: the picture route accepts only the minted shape and sends nosniff + sandbox",
    server.includes("/^ci_[0-9a-f]{24}\\.(png|jpg|gif|webp)$/") && server.includes('"X-Content-Type-Options": "nosniff"') && server.includes('"Content-Security-Policy": "sandbox"'));
  check("server: the route never takes a path from the request", !/conv-images[^\n]*req\.query/.test(server));
  check("server: the typed words and the placeholder are what redo drops by", server.includes('["ai-drop-from", typed]'));

  check("page: the script loads before app.js and the controls exist",
    html.indexOf('src="ask-images.js"') > 0 && html.indexOf('src="ask-images.js"') < html.indexOf('src="app.js"') &&
    html.includes('id="ask-attach-input"') && html.includes('id="btn-ask-attach"') && html.includes('id="ask-attach-bar"'));
  check("page: the file picker only offers the four picture types", /accept="image\/png,image\/jpeg,image\/gif,image\/webp"/.test(html));
  check("app: pictures are read before the composer is cleared, and sent as `images`",
    app.indexOf("payloads = await Promise.all(files.map(fileToPayload))") > 0 &&
    app.indexOf("payloads = await Promise.all(files.map(fileToPayload))") < app.indexOf("clearAttachments();") &&
    app.includes("images: payloads.length ? payloads : undefined"));
  check("app: the saved copies and the typed words are what a reload draws",
    app.includes("AI.textOf(ex)") && app.includes("AI.picturesOf(ex)"));
  check("app: paste and drop only react to real picture files", app.includes("window.JarvisAskImages.isPicture(f)") && app.includes('Array.from(e.dataTransfer.types || []).includes("Files")'));
  check("app: a redo after a reload says why it cannot resend the pictures", app.includes("can't be sent again after a reload"));
  check("app: an organize-json command is never taken for a message with pictures", app.includes("quotes.length === 0 && files.length === 0 ? ORGANIZE_JSON_RE"));
  check("css: thumbnails and bubble pictures are styled", css.includes(".ask-attach-thumb") && css.includes(".ask-msg__pics"));

  for (const n of failed) console.log("FAILED: " + n);
  console.log(passed + " passed, " + failed.length + " failed");
  process.exit(failed.length ? 1 : 0);
})().catch((e) => {
  console.log("CRASH: " + (e && e.stack || e));
  process.exit(1);
});
