/* ============================================================================
 * speaker-chip.js — who wrote this line (channel spaces S11).
 *
 * In a shared room an exchange carries `speaker` ({ name, handle, id, owner }, written by
 * conversations.append_exchange). This file turns it into the SAME label the model reads
 * (channels/space.py speaker_label: the stored name, else @handle, else user…<last4>,
 * " (owner)" appended for the owner, capped at 60 characters including that suffix) and
 * into a small chip. Used by the Ask thread's replay (app.js) and by the Channels panel.
 *
 * Pure and tiny on purpose: no state, no fetch. The name is a stranger's text, so the chip is
 * built with textContent only, never markup.
 * ========================================================================= */
(function (global) {
  "use strict";

  const MAX_LABEL_CHARS = 60;
  const OWNER_SUFFIX = " (owner)";

  const clean = (value, cap) => String(value == null ? "" : value).replace(/\s+/g, " ").trim().slice(0, cap);

  // "" when there is no speaker (an ordinary exchange: no chip is drawn at all).
  function label(speaker) {
    if (!speaker || typeof speaker !== "object" || Array.isArray(speaker) || !Object.keys(speaker).length) return "";
    const name = clean(speaker.name, MAX_LABEL_CHARS);
    const handle = clean(String(speaker.handle == null ? "" : speaker.handle).replace(/^@+/, ""), MAX_LABEL_CHARS);
    const uid = String(speaker.id == null ? "" : speaker.id).trim();
    const base = name || (handle ? "@" + handle : "") || (uid ? "user\u2026" + uid.slice(-4) : "someone");
    const suffix = speaker.owner ? OWNER_SUFFIX : "";
    return base.slice(0, MAX_LABEL_CHARS - suffix.length).replace(/\s+$/, "") + suffix;
  }

  // A <span class="speaker-chip"> for a speaker, or null when there is none.
  function chip(speaker, doc) {
    const text = label(speaker);
    if (!text) return null;
    const node = (doc || global.document).createElement("span");
    node.className = "speaker-chip" + (speaker.owner ? " speaker-chip--owner" : "");
    node.textContent = text;
    node.title = "Who wrote this, in a shared room";
    return node;
  }

  global.JarvisSpeaker = { label, chip, MAX_LABEL_CHARS };
})(typeof window !== "undefined" ? window : globalThis);
