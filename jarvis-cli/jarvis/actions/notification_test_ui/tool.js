"use strict";
// Menu > Notification test.  `host` is provided by Jarvis; this file is run as the body of an async
// function, so a top-level `return` is legal.  host.root is a shadow root: query it, never `document`.
// Only the tool of notification_test.py can be run (host.runTool is checked server-side).
//
// NO LISTS HERE.  The levels, the notification types, the kinds and the positions are all read from
// the tool's `describe` answer (notify_registry.catalog()) when the panel opens and whenever the
// window regains focus; the panel redraws when the catalog's `version` has changed.  The tool
// itself never takes a recipient, and the panel shows the phone-messaging types (`dm`) disabled.
//
// SAFETY: every string that came from the notifier is put on the page with textContent only.
//
// SHAPE: the first half is PURE (no DOM) so a test can load this file with host.__ntPureOnly = true.

// ============================================================================== pure helpers

const WAIT_TEXT = "Reading the notification options\u2026";
const FAILED_LABEL = "Failure (a small toast is drawn as a card)";

/** The tool's arguments for what is chosen in the form. `types` carries the ticked channels AND the
 *  ticked behaviours, exactly as a scheduled job's `channels` does. */
function buildArgs(form) {
  const args = {};
  if (form.level !== "" && form.level != null) args.level = Number(form.level);
  const types = (form.types || []).concat(form.behaviours || []);
  if (types.length) args.types = types;
  if (form.failed) args.failed = true;
  if (form.kind) args.kind = form.kind;
  if (form.position) args.position = form.position;
  const count = Number(form.count);
  if (Number.isInteger(count) && count > 1) args.count = count;
  if (form.title) args.title = form.title;
  if (form.message) args.message = form.message;
  return args;
}

/** The sentences the preview shows, from notifier.resolve_delivery()'s answer. */
function previewLines(plan, labelFor, lookLabel) {
  if (!plan) return [];
  const lines = [];
  const channels = (plan.channels || []).map(labelFor).join(", ") || "nothing";
  lines.push({ text: "Delivered through: " + channels + ".", cls: "" });
  if (plan.disabled) lines.push({ text: "Notifications are turned off in notify_config.json, so nothing will be delivered.", cls: "nt__bad" });
  lines.push({ text: "On the page: " + lookLabel(plan.look) + (plan.look === "compact" && plan.tone ? " (" + plan.tone + ")" : "") + ".", cls: "" });
  (plan.forced_reasons || []).forEach((r) => lines.push({ text: r.charAt(0).toUpperCase() + r.slice(1) + ".", cls: "nt__warn" }));
  if (plan.muted) lines.push({ text: "This level draws nothing on the page; it only goes to the inbox.", cls: "nt__warn" });
  if (plan.skipped_dm && plan.skipped_dm.length) lines.push({ text: "Left out so no real message is sent to your phone: " + plan.skipped_dm.map(labelFor).join(", ") + ".", cls: "nt__warn" });
  return lines;
}

/** One line for a result row. */
function resultLine(r, labelFor, lookLabel) {
  const bits = ["level " + r.level, lookLabel(r.look)];
  if (r.delivered && r.delivered.length) bits.push("delivered: " + r.delivered.map(labelFor).join(", "));
  if (r.failed_channels && r.failed_channels.length) bits.push("failed: " + r.failed_channels.map(labelFor).join(", "));
  if (r.deferred_to_digest) bits.push("held for the digest");
  return bits.join(" \u00B7 ");
}

if (host.__ntPureOnly) return { buildArgs, previewLines, resultLine, WAIT_TEXT, FAILED_LABEL };

// ============================================================================== the panel

const root = host.root;
const $ = (sel) => root.querySelector(sel);
const S = { catalog: null, version: "", results: [], previewTimer: null, seq: 0 };

function el(tag, attrs, text) {
  const node = document.createElement(tag);
  Object.entries(attrs || {}).forEach(([k, v]) => { if (v !== null && v !== undefined) node.setAttribute(k, v); });
  if (text !== undefined) node.textContent = text;
  return node;
}

function labelFor(id) {
  const a = S.catalog && S.catalog.actions.find((x) => x.id === id);
  return a ? a.label : String(id);
}

function lookLabel(id) {
  const l = S.catalog && (S.catalog.looks || []).find((x) => x.id === id);
  return l ? l.label : String(id);
}

async function callTool(args) {
  try {
    return await host.runTool("notification_test", args || {});
  } catch (err) {
    return { ok: false, error: (err && err.message) || "That didn't work" };
  }
}

// What the form has chosen right now.
function readForm() {
  const ticked = (box) => Array.from(root.querySelectorAll(box + " input:checked:not(:disabled)")).map((i) => i.value);
  const flags = ticked("#ntFlags");
  return {
    level: $("#ntLevel").value,
    types: ticked("#ntTypes"),
    behaviours: flags.filter((v) => v !== "failed"),
    failed: flags.includes("failed"),
    kind: $("#ntKind").value,
    position: $("#ntPosition").value,
    count: $("#ntCount").value,
    title: $("#ntTitle").value.trim(),
    message: $("#ntMessage").value.trim(),
  };
}

function tick(box, id, label, title, opts) {
  const o = opts || {};
  const input = el("input", { type: "checkbox", value: id });
  if (o.disabled) input.disabled = true;
  if (o.checked) input.checked = true;
  const wrap = el("label", { class: "nt__tick", title: title || null, "data-disabled": o.disabled ? "1" : null });
  wrap.appendChild(input);
  wrap.appendChild(document.createTextNode(label));
  box.appendChild(wrap);
}

// Draw every picker from the catalog, keeping what was chosen.  Called on open, on focus, and
// whenever the catalog's `version` has changed.
function draw() {
  const cat = S.catalog;
  const keep = S.catalog && $("#ntLevel").options.length ? readForm() : null;
  const keepIds = new Set((keep ? keep.types.concat(keep.behaviours) : []).concat(keep && keep.failed ? ["failed"] : []));

  const level = $("#ntLevel");
  level.textContent = "";
  level.appendChild(el("option", { value: "" }, "The kind's own level"));
  cat.presets.forEach((p) => level.appendChild(el("option", { value: String(p.level), title: p.description || null }, p.level + " \u00B7 " + p.name)));
  level.value = keep && cat.presets.some((p) => String(p.level) === keep.level) ? keep.level : "";

  const types = $("#ntTypes");
  types.textContent = "";
  let hasDm = false;
  cat.actions.filter((a) => a.type === "channel").forEach((a) => {
    if (a.dm) hasDm = true;
    tick(types, a.id, a.label, a.description, { disabled: a.dm || a.always, checked: a.always || keepIds.has(a.id) });
  });
  $("#ntTypesHelp").textContent = (hasDm ? "Greyed-out types message a real phone or are always on; test those with `jarvis channels-test`. " : "")
    + "Ticking none uses whatever the chosen level delivers.";

  const flags = $("#ntFlags");
  flags.textContent = "";
  tick(flags, "failed", FAILED_LABEL, null, { checked: keepIds.has("failed") });
  cat.actions.filter((a) => a.type === "behaviour").forEach((a) => tick(flags, a.id, a.label, a.description, { checked: keepIds.has(a.id) }));

  const kind = $("#ntKind");
  kind.textContent = "";
  cat.kinds.forEach((k) => kind.appendChild(el("option", { value: k.id }, k.label || k.id)));
  kind.value = keep && cat.kinds.some((k) => k.id === keep.kind) ? keep.kind : (S.defaultKind || cat.kinds[0].id);

  const pos = $("#ntPosition");
  pos.textContent = "";
  pos.appendChild(el("option", { value: "" }, "Default"));
  cat.positions.forEach((p) => pos.appendChild(el("option", { value: p.id }, p.label || p.id)));
  pos.value = keep && cat.positions.some((p) => p.id === keep.position) ? keep.position : "";

  levelHelp();
  $("#ntSend").disabled = false;
  $("#ntLine").textContent = "Pick a level, the types to deliver through and any flags. The preview shows what will happen before you send.";
  schedulePreview();
}

function levelHelp() {
  const p = S.catalog && S.catalog.presets.find((x) => String(x.level) === $("#ntLevel").value);
  $("#ntLevelHelp").textContent = p ? (p.description || "") : "Each kind of notification has its own default level; this uses it.";
}

function schedulePreview() {
  clearTimeout(S.previewTimer);
  S.previewTimer = setTimeout(showPreview, 120);
}

async function showPreview() {
  if (!S.catalog) return;
  const seq = ++S.seq;
  const r = await callTool(Object.assign(buildArgs(readForm()), { preview: true }));
  if (seq !== S.seq) return;                       // a newer choice is already being previewed
  const box = $("#ntPreview");
  box.textContent = "";
  if (!r.ok) { box.appendChild(el("div", { class: "nt__bad" }, r.error || "That can't be sent.")); return; }
  previewLines(r.preview, labelFor, lookLabel).forEach((ln) => box.appendChild(el("div", { class: ln.cls || null }, ln.text)));
}

function drawResults() {
  const box = $("#ntResults");
  box.textContent = "";
  $("#ntClear").disabled = !S.results.length;
  if (!S.results.length) { box.appendChild(el("div", { class: "nt__empty" }, "Nothing sent yet.")); return; }
  S.results.forEach((r) => {
    const row = el("div", { class: "nt__result" });
    row.appendChild(el("span", { class: "nt__result-text" }, resultLine(r, labelFor, lookLabel)));
    const btn = el("button", { type: "button", class: "btn btn--sm" }, "Dismiss");
    btn.addEventListener("click", async () => {
      await callTool({ dismiss: [r.id] });
      S.results = S.results.filter((x) => x.id !== r.id);
      drawResults();
    });
    row.appendChild(btn);
    box.appendChild(row);
  });
}

async function send() {
  const btn = $("#ntSend");
  const msg = $("#ntSendMsg");
  btn.disabled = true;
  msg.textContent = "";
  const r = await callTool(buildArgs(readForm()));
  btn.disabled = false;
  if (!r.ok) { msg.textContent = r.error || "That didn't work."; return; }
  msg.textContent = r.sent === 1 ? "Sent." : "Sent " + r.sent + ".";
  S.results = (r.results || []).concat(S.results).slice(0, 20);
  drawResults();
}

// Read the catalog; redraw only if its version changed (or this is the first read).
async function load() {
  const r = await callTool({ describe: true });
  if (!r.ok || !r.catalog) {
    if (!S.catalog) {
      $("#ntLine").textContent = "The notification options could not be loaded. Close and reopen this panel to try again.";
      $("#ntSend").disabled = true;
    }
    return;
  }
  S.defaultKind = r.default_kind;
  if (S.catalog && S.version === r.catalog.version) return;
  S.catalog = r.catalog;
  S.version = r.catalog.version || "";
  draw();
}

root.addEventListener("input", schedulePreview);
root.addEventListener("change", (e) => { if (e.target && e.target.id === "ntLevel") levelHelp(); schedulePreview(); });
$("#ntSend").addEventListener("click", send);
$("#ntClear").addEventListener("click", async () => {
  await callTool({ dismiss: S.results.map((r) => r.id) });
  S.results = [];
  drawResults();
});
$("#ntLine").textContent = WAIT_TEXT;
$("#ntSend").disabled = true;
drawResults();

// Re-check the catalog whenever the page regains focus (no timer: each check starts a tool run).
const onFocus = () => { if (!root.isConnected) { window.removeEventListener("focus", onFocus); return; } load(); };
window.addEventListener("focus", onFocus);
await load();
