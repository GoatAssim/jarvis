/* ============================================================================
 * category-manager.js - "Manage categories": rename, merge and delete a category
 * as a whole, for the Daemons panel and for the Commands panel.
 *
 * A category is only a name inside items' `categories` lists, so each of these is
 * one request that edits every item's list at once:
 *
 *   rename  POST /api/<kind>-categories/rename  { from, to }
 *   delete  POST /api/<kind>-categories/delete  { name }
 *   list    GET  /api/<kind>-categories
 *
 * Renaming to a name that already exists IS the merge, so there is no separate
 * merge button; the dialog says so, and asks first when the new name is in use.
 * Daemons and commands keep separate vocabularies (owner decision Q5), so `kind`
 * ("daemon" or "command") picks the routes and the two never touch each other.
 *
 * It is built from JarvisUI's choose / prompt / confirm, one after another, because
 * the UI kit shows one modal at a time. The list comes back after every action
 * until the person presses Cancel, so several categories can be tidied in a row.
 *
 * EVERYTHING IS TEXT. Names come from files a person may have hand-edited; they are
 * only ever handed to JarvisUI as strings (it uses text nodes), never as markup.
 * ========================================================================= */
(function (global) {
  "use strict";

  const KINDS = {
    daemon: { path: "/api/daemon-categories", one: "service", many: "services", what: "Daemons" },
    command: { path: "/api/command-categories", one: "command", many: "commands", what: "Commands" },
  };

  /* ---- pure -------------------------------------------------------------- */

  const keyOf = (name) => String(name == null ? "" : name).trim().replace(/\s+/g, " ").toLowerCase();

  function countLabel(count, kind) {
    const k = KINDS[kind] || KINDS.daemon;
    return count + " " + (count === 1 ? k.one : k.many);
  }

  // The rows the first dialog offers: one per category, most used first (the server
  // already sorts them), each with how many items carry it.
  function listOptions(rows, kind) {
    return (Array.isArray(rows) ? rows : []).map((r) => ({
      label: String(r.name),
      hint: countLabel(Number(r.count) || 0, kind),
      value: String(r.name),
    }));
  }

  // The existing category a rename target would merge into, or null. Renaming a
  // category to a different spelling of ITSELF (Utility -> utility) is not a merge.
  function mergeTarget(rows, from, to) {
    const fromKey = keyOf(from);
    const toKey = keyOf(to);
    if (!toKey || toKey === fromKey) return null;
    const hit = (Array.isArray(rows) ? rows : []).find((r) => keyOf(r.name) === toKey);
    return hit || null;
  }

  // What the person typed in the rename box, read as an instruction:
  //   { action: "none" }    nothing to do (blank, or exactly what it was)
  //   { action: "rename", to, merge }   merge is the row it would join, or null
  function readRenameAnswer(rows, from, typed) {
    const to = String(typed == null ? "" : typed).trim().replace(/\s+/g, " ");
    if (!to || to === from) return { action: "none" };
    return { action: "rename", to, merge: mergeTarget(rows, from, to) };
  }

  /* ---- network ----------------------------------------------------------- */

  async function call(kind, method, suffix, body) {
    const k = KINDS[kind];
    const res = await fetch(k.path + (suffix || ""), {
      method,
      headers: { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    let data = null;
    try { data = await res.json(); } catch (_) { data = null; }
    if (!res.ok || !data || data.ok === false || data.error) {
      throw new Error((data && data.error) || ("Request failed (" + res.status + ")."));
    }
    return data;
  }

  /* ---- the dialog chain -------------------------------------------------- */

  const ui = () => global.JarvisUI || null;
  const say = (message, level) => {
    if (global.JarvisHost && typeof global.JarvisHost.toast === "function") global.JarvisHost.toast(message, level || "info");
    else if (ui() && ui().toast) ui().toast({ message, level: level || "info" });
  };

  async function renameFlow(kind, rows, name) {
    const k = KINDS[kind];
    const typed = await ui().prompt({
      title: "Rename \u201c" + name + "\u201d",
      body: "Type the new name. If it is a category you already have, the two are merged into one.",
      default: name,
      confirmLabel: "Rename",
    });
    const answer = readRenameAnswer(rows, name, typed);
    if (answer.action === "none") return false;
    if (answer.merge) {
      const ok = await ui().confirm({
        title: "Merge into \u201c" + answer.merge.name + "\u201d?",
        body: "Every " + k.one + " labelled \u201c" + name + "\u201d will be labelled \u201c" + answer.merge.name + "\u201d instead. "
          + "\u201c" + answer.merge.name + "\u201d already has " + countLabel(Number(answer.merge.count) || 0, kind) + ".",
        confirmLabel: "Merge",
      });
      if (!ok) return false;
    }
    const r = await call(kind, "POST", "/rename", { from: name, to: answer.to });
    say((r.merged ? "Merged into " : "Renamed to ") + "\u201c" + answer.to + "\u201d on " + countLabel(r.changed || 0, kind) + ".");
    return true;
  }

  async function deleteFlow(kind, rows, name) {
    const k = KINDS[kind];
    const row = rows.find((r) => keyOf(r.name) === keyOf(name));
    const n = row ? Number(row.count) || 0 : 0;
    const ok = await ui().confirm({
      title: "Delete \u201c" + name + "\u201d?",
      body: "The label is taken off " + countLabel(n, kind) + ". The " + k.many + " themselves are not touched"
        + (kind === "daemon" ? " (none is started or stopped)" : "") + "; one left with no category is shown as Undefined.",
      confirmLabel: "Delete",
      level: "danger",
      focusCancel: true,
    });
    if (!ok) return false;
    const r = await call(kind, "POST", "/delete", { name });
    say("Deleted \u201c" + name + "\u201d from " + countLabel(r.changed || 0, kind) + ".");
    return true;
  }

  // Opens the manager. `onChanged` runs after every change that was saved, so the
  // caller can reload its list. Resolves when the person closes the dialog.
  async function open(opts) {
    const kind = opts && KINDS[opts.kind] ? opts.kind : "daemon";
    const k = KINDS[kind];
    if (!ui()) { say("The dialog kit didn\u2019t load, so categories can\u2019t be managed.", "error"); return; }
    for (;;) {
      let rows;
      try {
        rows = (await call(kind, "GET", "")).categories || [];
      } catch (err) {
        say("Couldn\u2019t read the categories: " + err.message, "error");
        return;
      }
      if (!rows.length) {
        await ui().dialog({
          title: "Manage categories",
          body: "No " + k.one + " has a category yet. Add one from a " + k.one + "\u2019s editor and it will show up here.",
        });
        return;
      }
      const picked = await ui().choose({
        title: "Manage categories \u2014 " + k.what,
        body: "Pick a category to rename, merge or delete. Only the " + k.what.toLowerCase() + " list is affected.",
        options: listOptions(rows, kind),
      });
      if (picked === null || picked === undefined) return;
      const action = await ui().choose({
        title: "\u201c" + picked + "\u201d",
        options: [
          { label: "Rename or merge\u2026", hint: "A name you already use merges the two", value: "rename" },
          { label: "Delete", hint: "Takes the label off, keeps the " + k.many, value: "delete" },
        ],
      });
      if (action === null || action === undefined) continue;
      try {
        const changed = action === "rename" ? await renameFlow(kind, rows, picked) : await deleteFlow(kind, rows, picked);
        if (changed && opts && typeof opts.onChanged === "function") await opts.onChanged();
      } catch (err) {
        say(err.message, "error");
      }
    }
  }

  global.JarvisCategoryManager = {
    open,
    _pure: { keyOf, countLabel, listOptions, mergeTarget, readRenameAnswer },
  };
})(typeof window !== "undefined" ? window : globalThis);
