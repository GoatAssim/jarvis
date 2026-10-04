/* ============================================================================
 * mcp-servers.js — L.31 MCP servers rework (the panel).
 *
 * Replaces the MCP block that used to live in app.js: a read-only list of
 * "name · enabled · stdio · 3 tool(s) · confirm-gated" lines whose only action
 * was Refresh, and whose empty state said "edit mcp_config.json by hand".
 *
 * WHAT THE PANEL DOES NOW
 * -----------------------
 *   - One card per server: a status word (Ready / Off / Error / Needs fixing /
 *     Not connected yet / Tool list is old), the transport, whether its tools
 *     ask first or are trusted, when the tool list was last read, and the
 *     reason in plain words when something is wrong.
 *   - An on/off switch, Refresh (this server only), Edit and Remove on every
 *     card; a Trusted switch for the confirm gate.
 *   - The server's tools, expandable, each marked when the Tool Manager (L.25)
 *     has switched it off — so "plugged in but the model can't use 3 of 9" is
 *     visible here instead of only in another panel.
 *   - Add / Edit in the panel (arguments one per line, environment variables as
 *     rows). Secrets never come back to the browser: a stored environment
 *     value or a URL's token is shown as "unchanged" and kept unless retyped.
 *   - Search, an "Needs attention" filter, and keyboard keys.
 *
 * WHAT IT DOES NOT CHANGE
 * -----------------------
 *   The security rule in mcp_client.py: servers are added or changed only by a
 *   HUMAN (this panel's buttons, `jarvis mcp-edit`, or the file) — never by a
 *   model tool. The confirm gate for MCP tools is still the default; Trusted is
 *   still an explicit per-server choice, and turning it on asks first here.
 *
 * EVERYTHING IS TEXT, NEVER MARKUP
 * --------------------------------
 *   A server's name, description and its tools' descriptions come from other
 *   people's programs. Every value goes in through textContent / text nodes,
 *   never innerHTML.
 *
 * STRUCTURE
 * ---------
 *   1. constants          4. rendering (list, cards, editor)
 *   2. pure helpers       5. actions
 *      (JarvisMcp._pure,  6. wiring
 *      tests/verify_mcp_servers.js)
 *   3. server access
 * ========================================================================= */

(function (global) {
  "use strict";

  /* ======================================================================
   * 1. constants
   * ==================================================================== */

  const API = {
    status: "/api/mcp",
    refresh: "/api/mcp/refresh",
    save: "/api/mcp/server",
    server: (name) => "/api/mcp/server/" + encodeURIComponent(name),
  };

  // How each state the backend reports (mcp_client._server_state) is shown.
  // `tone` picks the colour AND a word, so colour is never the only signal.
  const STATES = {
    ok:            { label: "Ready",              tone: "ok" },
    stale:         { label: "Tool list is old",   tone: "warn" },
    not_refreshed: { label: "Not connected yet",  tone: "warn" },
    error:         { label: "Error",              tone: "bad" },
    misconfigured: { label: "Needs fixing",       tone: "bad" },
    disabled:      { label: "Off",                tone: "off" },
  };
  const ATTENTION_STATES = ["error", "misconfigured", "not_refreshed", "stale"];

  const CHIPS = [
    { id: "all", label: "All" },
    { id: "attention", label: "Needs attention" },
    { id: "on", label: "On" },
    { id: "off", label: "Off" },
  ];

  const ENV_KEY_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;
  const MAX_NAME = 64;
  const REFRESH_ALL_KEY = "\u0000all";  // a busy-map key no server name can be

  /* ======================================================================
   * 2. pure helpers (no DOM, no network)
   * ==================================================================== */

  function stateInfo(state) {
    return STATES[state] || { label: String(state || "unknown"), tone: "off" };
  }

  // `ts` is a unix time in SECONDS (Python's time.time()); `nowMs` is Date.now().
  function relativeTime(ts, nowMs) {
    const t = Number(ts);
    if (!t) return "never";
    const secs = Math.max(0, Math.round((nowMs / 1000) - t));
    if (secs < 45) return "just now";
    const mins = Math.round(secs / 60);
    if (mins < 60) return mins + " min ago";
    const hours = Math.round(secs / 3600);
    if (hours < 48) return hours + " h ago";
    return Math.round(secs / 86400) + " d ago";
  }

  function needsAttention(server) {
    if (!server || !server.enabled) return false;
    return ATTENTION_STATES.indexOf(server.state) !== -1 || !!server.renamed_due_to_collision;
  }

  // The command as it would be typed, for display only. An argument with a
  // space is quoted so "a b" and "a", "b" don't look the same.
  function commandLine(server) {
    if (!server) return "";
    if (server.transport && server.transport !== "stdio") return server.url || "";
    const parts = [server.command || ""].concat(server.args || []);
    return parts.filter((p) => p !== "").map((p) => (/\s/.test(p) ? '"' + p + '"' : p)).join(" ");
  }

  function haystack(server) {
    const bits = [server.name, server.slug, server.description, commandLine(server), server.state];
    (server.tools || []).forEach((t) => { bits.push(t.name, t.description); });
    return bits.filter(Boolean).join(" \n ").toLowerCase();
  }

  // Every word typed has to appear somewhere on the server (name, description,
  // command, any of its tools) — so "git commit" finds a server with a
  // "commit" tool even though its name is just "git".
  function matchesQuery(server, query) {
    const words = String(query || "").toLowerCase().split(/\s+/).filter(Boolean);
    if (!words.length) return true;
    const hay = haystack(server);
    return words.every((w) => hay.indexOf(w) !== -1);
  }

  function matchesChip(server, chip) {
    switch (chip) {
      case "attention": return needsAttention(server);
      case "on": return !!server.enabled;
      case "off": return !server.enabled;
      default: return true;
    }
  }

  function filterServers(servers, query, chip) {
    return (servers || []).filter((s) => matchesChip(s, chip) && matchesQuery(s, query));
  }

  function chipCounts(servers) {
    const list = servers || [];
    return {
      all: list.length,
      attention: list.filter(needsAttention).length,
      on: list.filter((s) => s.enabled).length,
      off: list.filter((s) => !s.enabled).length,
    };
  }

  function statusLine(data) {
    const servers = (data && data.servers) || [];
    if (!servers.length) return "no servers yet";
    const bits = [(data.enabled_count || 0) + " on of " + servers.length,
                  (data.total_tools || 0) + " tool" + (data.total_tools === 1 ? "" : "s")];
    const attention = servers.filter(needsAttention).length;
    if (attention) bits.push(attention + " need" + (attention === 1 ? "s" : "") + " attention");
    return bits.join(" \u00b7 ");
  }

  // Arguments are typed one per line, so a space inside an argument needs no
  // quoting and nothing is split by a shell (there is none).
  function parseArgs(text) {
    return String(text || "").split(/\r?\n/).map((l) => l.trim()).filter((l) => l.length > 0);
  }

  // Turns the editor's fields into the request body, or a list of problems.
  // `existing` is the status row being edited (null when adding): it is what
  // lets a blank secret mean "keep what is stored".
  function buildSpec(form, existing) {
    const errors = [];
    const name = String(form.name || "").trim();
    if (!name) errors.push("Give the server a name.");
    else if (name.length > MAX_NAME) errors.push("The name is longer than " + MAX_NAME + " characters.");
    else if (name.charAt(0) === "-") errors.push("The name can't start with a dash.");
    else if (!/[A-Za-z0-9]/.test(name)) errors.push("The name needs at least one letter or digit \u2014 it becomes part of every tool name.");

    const transport = form.transport === "http" ? "http" : "stdio";
    const spec = {
      enabled: !!form.enabled,
      trusted: !!form.trusted,
      transport: transport,
      description: String(form.description || "").trim(),
    };

    if (transport === "stdio") {
      const command = String(form.command || "").trim();
      if (!command) errors.push("A local server needs a command (for example npx or uvx).");
      spec.command = command;
      spec.args = parseArgs(form.argsText);
      spec.cwd = String(form.cwd || "").trim();
      const env = {};
      const seen = {};
      (form.env || []).forEach((row) => {
        const key = String((row && row.key) || "").trim();
        if (!key && !(row && row.value)) return;           // an untouched blank row
        if (!ENV_KEY_RE.test(key)) { errors.push("\u201c" + key + "\u201d isn't a valid variable name (letters, digits, underscore)."); return; }
        if (seen[key]) { errors.push(key + " appears twice."); return; }
        seen[key] = true;
        const value = row.value === undefined || row.value === null ? "" : String(row.value);
        // Blank on a stored variable = keep it. Blank on a new one = empty text.
        env[key] = (value === "" && row.existing) ? null : value;
      });
      spec.env = env;
    } else {
      let url = String(form.url || "").trim();
      if (!url) {
        const canKeep = existing && existing.transport !== "stdio" && existing.url_redacted;
        if (canKeep) url = null;                            // keep the stored one
        else errors.push("A remote server needs a URL starting with http:// or https://.");
      } else if (!/^https?:\/\//i.test(url)) {
        errors.push("The URL must start with http:// or https://.");
      }
      spec.url = url;
    }
    return { name: name, spec: spec, errors: errors };
  }

  const pure = {
    stateInfo, relativeTime, needsAttention, commandLine, matchesQuery, matchesChip,
    filterServers, chipCounts, statusLine, parseArgs, buildSpec,
  };

  /* ======================================================================
   * 3. server access
   * ==================================================================== */

  async function call(method, url, body) {
    const res = await fetch(url, {
      method: method,
      headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    let data = null;
    try { data = await res.json(); } catch (_) { /* no body */ }
    if (!res.ok) throw new Error((data && data.error) || (method + " " + url + " failed (" + res.status + ")"));
    return data || {};
  }

  /* ======================================================================
   * 4. rendering
   * ==================================================================== */

  const S = {
    data: null,            // the last /api/mcp payload
    loadError: "",         // set when the status read itself failed
    query: "",
    chip: "all",
    open: new Set(),       // server names whose details are expanded
    toolsOpen: new Set(),  // server names whose tool list is expanded
    busy: new Map(),       // server name (or REFRESH_ALL_KEY) -> what it is doing
    editor: null,          // null, or { mode: "add" | "edit", server, error, saving }
  };

  function $(id) { return document.getElementById(id); }

  function h(tag, attrs, kids) {
    const node = document.createElement(tag);
    Object.keys(attrs || {}).forEach((key) => {
      const value = attrs[key];
      if (value === null || value === undefined || value === false) return;
      if (key === "class") node.className = value;
      else if (key.slice(0, 2) === "on" && typeof value === "function") node.addEventListener(key.slice(2), value);
      else if (key === "text") node.textContent = String(value);
      else node.setAttribute(key, value === true ? "" : String(value));
    });
    (Array.isArray(kids) ? kids : kids === undefined ? [] : [kids]).forEach((kid) => {
      if (kid === null || kid === undefined || kid === false) return;
      node.appendChild(typeof kid === "string" || typeof kid === "number" ? document.createTextNode(String(kid)) : kid);
    });
    return node;
  }

  function toast(message, kind) {
    const host = global.JarvisHost;
    if (host && typeof host.toast === "function") host.toast(message, kind || "info");
  }

  function servers() { return (S.data && S.data.servers) || []; }
  function findServer(name) { return servers().find((s) => s.name === name) || null; }

  function renderStatus() {
    // While the editor is open, the header's list-level buttons would act on a
    // list nobody can see.
    const add = $("btn-mcp-add");
    if (add) add.hidden = !!S.editor;
    const line = $("mcp-status-line");
    if (line) line.textContent = S.loadError ? "couldn't read MCP status" : (S.data ? statusLine(S.data) : "reading servers\u2026");
    const all = $("btn-mcp-refresh");
    if (all) {
      all.hidden = !!S.editor;
      all.disabled = S.busy.has(REFRESH_ALL_KEY) || !S.data || !(S.data.enabled_count > 0);
      all.textContent = S.busy.has(REFRESH_ALL_KEY) ? "Refreshing\u2026" : "Refresh all";
    }
  }

  function renderChips() {
    const wrap = $("mcp-chips");
    if (!wrap) return;
    const counts = chipCounts(servers());
    wrap.textContent = "";
    CHIPS.forEach((chip) => {
      wrap.appendChild(h("button", {
        type: "button", class: "mcp-chip" + (S.chip === chip.id ? " is-on" : ""),
        "aria-pressed": S.chip === chip.id ? "true" : "false",
        onclick: () => { S.chip = chip.id; render(); },
      }, [chip.label, h("span", { class: "mcp-chip__n", text: counts[chip.id] })]));
    });
  }

  function badge(text, tone) {
    return h("span", { class: "mcp-badge" + (tone ? " mcp-badge--" + tone : ""), text: text });
  }

  function switchControl(on, label, disabled, onToggle) {
    return h("button", {
      type: "button", role: "switch", class: "mcp-switch" + (on ? " is-on" : ""),
      "aria-checked": on ? "true" : "false", "aria-label": label, title: label,
      disabled: disabled ? true : null, onclick: onToggle,
    }, [h("span", { class: "mcp-switch__knob" })]);
  }

  function renderToolList(server) {
    const tools = server.tools || [];
    const box = h("div", { class: "mcp-tools" });
    if (!tools.length) {
      box.appendChild(h("div", { class: "mcp-tools__empty", text: server.enabled
        ? "No tools listed yet \u2014 press Refresh to connect and read them."
        : "Switch the server on to list its tools." }));
      return box;
    }
    tools.forEach((tool) => {
      box.appendChild(h("div", { class: "mcp-tool" + (tool.disabled ? " is-off" : "") }, [
        h("div", { class: "mcp-tool__head" }, [
          h("span", { class: "mcp-tool__name", text: tool.name }),
          tool.disabled ? badge("off in Tool Manager", "off") : null,
        ]),
        tool.description ? h("div", { class: "mcp-tool__desc", text: tool.description }) : null,
        h("div", { class: "mcp-tool__id", text: tool.tool_name }),
      ]));
    });
    return box;
  }

  function detailRow(label, value) {
    return h("div", { class: "mcp-detail" }, [
      h("span", { class: "mcp-detail__k", text: label }),
      h("span", { class: "mcp-detail__v", text: value }),
    ]);
  }

  function renderDetails(server) {
    const box = h("div", { class: "mcp-details" });
    const http = server.transport && server.transport !== "stdio";
    if (http) {
      box.appendChild(detailRow("URL", server.url || "(none)"));
      if (server.url_redacted) box.appendChild(detailRow("", "Part of the stored URL (a login or token) is hidden here."));
    } else {
      box.appendChild(detailRow("Command", commandLine(server) || "(none)"));
      if (server.cwd) box.appendChild(detailRow("Folder", server.cwd));
      if (server.env_keys && server.env_keys.length) {
        box.appendChild(detailRow("Environment", server.env_keys.join(", ") + "  (values are never shown)"));
      }
    }
    box.appendChild(detailRow("Tool prefix", "mcp_" + (server.slug || "?") + "_"));
    box.appendChild(detailRow("Tool list read", relativeTime(server.last_refreshed, Date.now())));
    return box;
  }

  function renderCard(server) {
    const info = stateInfo(server.state);
    const busy = S.busy.get(server.name) || "";
    const isOpen = S.open.has(server.name);
    const toolsOpen = S.toolsOpen.has(server.name);
    const http = server.transport && server.transport !== "stdio";
    const nTools = server.tool_count || 0;

    const flags = [badge(http ? "remote" : "local", null)];
    flags.push(server.trusted ? badge("trusted", "warn") : badge("asks first", null));
    if (server.renamed_due_to_collision) flags.push(badge("name clash", "bad"));

    const meta = [];
    if (server.enabled) {
      meta.push(nTools + " tool" + (nTools === 1 ? "" : "s"));
      if (server.disabled_tool_count) meta.push(server.disabled_tool_count + " switched off in Tool Manager");
      if (server.last_refreshed) meta.push("read " + relativeTime(server.last_refreshed, Date.now()));
    }

    const problems = [];
    (server.problems || []).forEach((p) => problems.push(p));
    if (server.error) problems.push(server.error);
    if (server.renamed_due_to_collision) {
      problems.push("Its name collides with another server's, so its tools use the prefix mcp_" + server.slug + "_ instead.");
    }

    const card = h("div", {
      class: "mcp-card mcp-card--" + info.tone + (busy ? " is-busy" : ""),
      "data-server": server.name,
    }, [
      h("div", { class: "mcp-card__top" }, [
        h("span", { class: "mcp-dot mcp-dot--" + info.tone, "aria-hidden": "true" }),
        h("div", { class: "mcp-card__title" }, [
          h("div", { class: "mcp-card__name", text: server.name }),
          h("div", { class: "mcp-card__state mcp-card__state--" + info.tone, text: busy || info.label }),
        ]),
        h("div", { class: "mcp-card__flags" }, flags),
        switchControl(!!server.enabled, (server.enabled ? "Switch off " : "Switch on ") + server.name, !!busy,
          () => toggleEnabled(server)),
      ]),
      server.description ? h("div", { class: "mcp-card__desc", text: server.description }) : null,
      h("div", { class: "mcp-card__line", text: commandLine(server) }),
      meta.length ? h("div", { class: "mcp-card__meta", text: meta.join(" \u00b7 ") }) : null,
      problems.length ? h("div", { class: "mcp-card__problem", role: "alert" },
        problems.map((p) => h("div", { text: p }))) : null,
      h("div", { class: "mcp-card__actions" }, [
        h("button", { type: "button", class: "btn btn--ghost btn--sm", disabled: busy ? true : null,
                      "aria-expanded": toolsOpen ? "true" : "false",
                      onclick: () => { toggleSet(S.toolsOpen, server.name); render(); } },
          (toolsOpen ? "Hide tools" : "Tools") + " (" + nTools + ")"),
        server.enabled ? h("button", { type: "button", class: "btn btn--ghost btn--sm", disabled: busy ? true : null,
                                       title: "Connect to this server and re-read its tool list",
                                       onclick: () => refreshServer(server) }, "Refresh") : null,
        h("button", { type: "button", class: "btn btn--ghost btn--sm", disabled: busy ? true : null,
                      onclick: () => openEditor("edit", server) }, "Edit"),
        h("button", { type: "button", class: "btn btn--ghost btn--sm",
                      "aria-expanded": isOpen ? "true" : "false",
                      onclick: () => { toggleSet(S.open, server.name); render(); } }, isOpen ? "Less" : "Details"),
        h("span", { class: "mcp-card__spacer" }),
        h("label", { class: "mcp-trust", title: "Trusted: its tools run without asking you first" }, [
          h("input", { type: "checkbox", checked: server.trusted ? true : null, disabled: busy ? true : null,
                       onchange: (e) => { e.target.checked = !!server.trusted; toggleTrusted(server); } }),
          h("span", { text: "Trusted" }),
        ]),
        h("button", { type: "button", class: "btn btn--danger btn--sm", disabled: busy ? true : null,
                      onclick: () => removeServer(server) }, "Remove"),
      ]),
      isOpen ? renderDetails(server) : null,
      toolsOpen ? renderToolList(server) : null,
    ]);
    return card;
  }

  function toggleSet(set, key) { if (set.has(key)) set.delete(key); else set.add(key); }

  function renderEmpty(filtered) {
    if (filtered) {
      return h("div", { class: "mcp-empty" }, [
        h("div", { class: "mcp-empty__title", text: "No server matches" }),
        h("div", { class: "mcp-empty__text", text: "Clear the search or pick \u201cAll\u201d." }),
        h("button", { type: "button", class: "btn btn--ghost btn--sm",
                      onclick: () => { S.query = ""; S.chip = "all"; const q = $("mcp-search"); if (q) q.value = ""; render(); } }, "Clear filters"),
      ]);
    }
    return h("div", { class: "mcp-empty" }, [
      h("div", { class: "mcp-empty__title", text: "No MCP servers yet" }),
      h("div", { class: "mcp-empty__text", text:
        "An MCP server is a separate program that gives Jarvis extra tools \u2014 files, GitHub, a database. " +
        "Add one and its tools join the normal tool list. Tools ask before they run unless you mark the server trusted." }),
      h("button", { type: "button", class: "btn btn--primary btn--sm", onclick: () => openEditor("add") }, "+ Add a server"),
    ]);
  }

  function renderList() {
    const list = $("mcp-list");
    if (!list) return;
    list.textContent = "";
    if (S.loadError) {
      list.appendChild(h("div", { class: "mcp-empty" }, [
        h("div", { class: "mcp-empty__title", text: "Couldn't read the server list" }),
        h("div", { class: "mcp-empty__text", text: S.loadError }),
        h("button", { type: "button", class: "btn btn--ghost btn--sm", onclick: () => load() }, "Try again"),
      ]));
      return;
    }
    if (!S.data) {
      list.appendChild(h("div", { class: "mcp-empty" }, [h("div", { class: "mcp-empty__text", text: "Loading\u2026" })]));
      return;
    }
    const all = servers();
    const shown = filterServers(all, S.query, S.chip);
    if (!shown.length) { list.appendChild(renderEmpty(all.length > 0)); return; }
    shown.forEach((s) => list.appendChild(renderCard(s)));
    if (S.data.name_collisions && S.data.name_collisions.length) {
      list.appendChild(h("div", { class: "mcp-note", text:
        "Two server names reduce to the same tool prefix; the later one got a _2 suffix so neither is lost." }));
    }
  }

  /* ---- the editor ------------------------------------------------------- */

  function field(label, control, hint) {
    return h("label", { class: "mcp-field" }, [
      h("span", { class: "mcp-field__label", text: label }),
      control,
      hint ? h("span", { class: "mcp-field__hint", text: hint }) : null,
    ]);
  }

  function envRow(row) {
    const keyInput = h("input", { class: "mcp-input mcp-input--key", type: "text", value: row.key || "",
                                  placeholder: "NAME", autocomplete: "off", spellcheck: "false", "aria-label": "Variable name" });
    const valueInput = h("input", { class: "mcp-input", type: "password", value: "", autocomplete: "off",
                                    placeholder: row.existing ? "unchanged \u2014 type to replace" : "value",
                                    "aria-label": "Variable value" });
    const wrap = h("div", { class: "mcp-envrow" }, [
      keyInput, valueInput,
      h("button", { type: "button", class: "btn btn--ghost btn--sm", "aria-label": "Remove variable",
                    onclick: () => wrap.remove() }, "\u00d7"),
    ]);
    wrap._read = () => ({ key: keyInput.value, value: valueInput.value, existing: !!row.existing && keyInput.value.trim() === row.key });
    return wrap;
  }

  function renderEditor() {
    const host = $("mcp-editor");
    const listView = $("mcp-list-view");
    if (!host || !listView) return;
    const ed = S.editor;
    host.hidden = !ed;
    listView.hidden = !!ed;
    if (!ed) { host.textContent = ""; return; }
    // Built ONCE per open editor. A background reload (a refresh finishing, a
    // refused save) calls render() again, and rebuilding the form from stored
    // state would silently wipe whatever the owner had typed.
    if (ed.ui && host.firstChild) return;
    host.textContent = "";

    const existing = ed.server || null;
    const isEdit = ed.mode === "edit";
    const http = existing && existing.transport && existing.transport !== "stdio";

    const nameInput = h("input", { class: "mcp-input", id: "mcp-f-name", type: "text", value: existing ? existing.name : "",
                                   autocomplete: "off", spellcheck: "false", maxlength: String(MAX_NAME), placeholder: "files" });
    const transport = h("select", { class: "mcp-input", id: "mcp-f-transport" }, [
      h("option", { value: "stdio", selected: !http ? true : null, text: "Local program (stdio)" }),
      h("option", { value: "http", selected: http ? true : null, text: "Remote URL (http)" }),
    ]);
    const command = h("input", { class: "mcp-input", id: "mcp-f-command", type: "text", value: existing ? existing.command : "",
                                 autocomplete: "off", spellcheck: "false", placeholder: "npx" });
    const args = h("textarea", { class: "mcp-input mcp-input--area", id: "mcp-f-args", rows: "4", spellcheck: "false",
                                 placeholder: "-y\n@modelcontextprotocol/server-filesystem\n~/Documents" },
      existing ? (existing.args || []).join("\n") : "");
    const cwd = h("input", { class: "mcp-input", id: "mcp-f-cwd", type: "text", value: existing ? existing.cwd : "",
                             autocomplete: "off", spellcheck: "false", placeholder: "(optional) folder to start it in" });
    const envBox = h("div", { class: "mcp-envrows" });
    const envRows = [];
    function addEnv(row) { const r = envRow(row); envRows.push(r); envBox.appendChild(r); return r; }
    ((existing && !http && existing.env_keys) || []).forEach((key) => addEnv({ key: key, existing: true }));
    const url = h("input", { class: "mcp-input", id: "mcp-f-url", type: "text",
                             value: existing && http && !existing.url_redacted ? existing.url : "",
                             autocomplete: "off", spellcheck: "false",
                             placeholder: existing && http && existing.url_redacted ? "unchanged \u2014 type a new URL to replace it" : "https://example.com/mcp" });
    const description = h("input", { class: "mcp-input", id: "mcp-f-desc", type: "text", value: existing ? existing.description : "",
                                     autocomplete: "off", maxlength: "300", placeholder: "(optional) what it is for" });
    const enabled = h("input", { type: "checkbox", id: "mcp-f-enabled", checked: (existing ? existing.enabled : true) ? true : null });
    const trusted = h("input", { type: "checkbox", id: "mcp-f-trusted", checked: existing && existing.trusted ? true : null });

    const stdioBlock = h("div", { class: "mcp-block" }, [
      field("Command", command, "The program to run. It is started directly, not through a shell."),
      field("Arguments \u2014 one per line", args, "A line is one argument, spaces and all."),
      field("Folder", cwd),
      h("div", { class: "mcp-field" }, [
        h("span", { class: "mcp-field__label", text: "Environment variables" }),
        envBox,
        h("button", { type: "button", class: "btn btn--ghost btn--sm", onclick: () => addEnv({}) }, "+ Add variable"),
        h("span", { class: "mcp-field__hint", text: "Values are stored in mcp_config.json and are never sent back to this page." }),
      ]),
    ]);
    const httpBlock = h("div", { class: "mcp-block" }, [
      field("URL", url, existing && http && existing.url_redacted
        ? "The stored URL contains a login or token, so it isn't shown. Leave this empty to keep it."
        : "The server's JSON-RPC endpoint."),
    ]);
    function syncTransport() { stdioBlock.hidden = transport.value !== "stdio"; httpBlock.hidden = transport.value !== "http"; }
    transport.addEventListener("change", syncTransport);
    syncTransport();

    const errorBox = h("div", { class: "mcp-editor__error", role: "alert", hidden: true }, "");

    function collect() {
      return {
        name: nameInput.value, transport: transport.value, command: command.value, argsText: args.value,
        cwd: cwd.value, url: url.value, description: description.value,
        enabled: enabled.checked, trusted: trusted.checked,
        env: envRows.filter((r) => r.isConnected).map((r) => r._read()),
      };
    }

    function submit() {
      if (ed.saving) return;
      const built = buildSpec(collect(), existing);
      if (built.errors.length) { showEditorError(built.errors.join(" ")); return; }
      saveServer(built, isEdit ? existing.name : null);
    }

    host.appendChild(h("div", { class: "mcp-editor__head" }, [
      h("div", { class: "mcp-editor__title", text: isEdit ? "Edit " + existing.name : "Add an MCP server" }),
    ]));
    host.appendChild(h("div", { class: "mcp-editor__body" }, [
      field("Name", nameInput, "Becomes part of every tool name: mcp_<name>_<tool>."),
      field("Type", transport),
      stdioBlock, httpBlock,
      field("Description", description),
      h("div", { class: "mcp-checks" }, [
        h("label", { class: "mcp-check" }, [enabled, h("span", { text: "Switched on" })]),
        h("label", { class: "mcp-check" }, [trusted, h("span", { text: "Trusted \u2014 its tools run without asking first" })]),
      ]),
      h("div", { class: "mcp-editor__warn", text:
        "A server is a program that runs on this computer with your permissions. Only add ones you trust. " +
        "Jarvis itself can never add one \u2014 only you can, here or in mcp_config.json." }),
      errorBox,
    ]));
    const saveBtn = h("button", { type: "button", class: "btn btn--primary btn--sm", id: "mcp-f-save",
                                  onclick: submit }, isEdit ? "Save changes" : "Add server");
    host.appendChild(h("div", { class: "mcp-editor__foot" }, [
      h("button", { type: "button", class: "btn btn--ghost btn--sm", onclick: () => closeEditor() }, "Cancel"),
      saveBtn,
    ]));
    ed.ui = { error: errorBox, save: saveBtn, label: isEdit ? "Save changes" : "Add server" };
    host.onkeydown = (e) => {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); submit(); }
    };
    setTimeout(() => { try { (isEdit ? command : nameInput).focus(); } catch (_) { /* gone */ } }, 0);
  }

  function render() {
    renderStatus();
    renderChips();
    renderList();
    renderEditor();
  }

  /* ======================================================================
   * 5. actions
   * ==================================================================== */

  async function load() {
    try {
      S.data = await call("GET", API.status);
      S.loadError = "";
    } catch (err) {
      S.loadError = (err && err.message) || "Failed.";
    }
    render();
  }

  async function busyWhile(key, label, fn) {
    S.busy.set(key, label);
    render();
    try {
      return await fn();
    } finally {
      S.busy.delete(key);
      await load();
    }
  }

  function describeRefresh(result, name) {
    const rows = (result && result.refreshed) || [];
    const failed = rows.filter((r) => r.error);
    if (failed.length) return { ok: false, text: failed.map((r) => r.server + ": " + r.error).join(" \u00b7 ") };
    const tools = rows.reduce((n, r) => n + (r.tools || 0), 0);
    return { ok: true, text: (name ? name + ": " : "") + tools + " tool" + (tools === 1 ? "" : "s") + " found." };
  }

  async function refreshServer(server) {
    await busyWhile(server.name, "Connecting\u2026", async () => {
      try {
        const out = describeRefresh(await call("POST", API.refresh, { server: server.slug }), server.name);
        toast(out.text, out.ok ? "info" : "error");
      } catch (err) {
        toast("Couldn't refresh " + server.name + ": " + err.message, "error");
      }
    });
  }

  async function refreshAll() {
    if (S.busy.has(REFRESH_ALL_KEY)) return;
    await busyWhile(REFRESH_ALL_KEY, "Refreshing\u2026", async () => {
      try {
        const out = describeRefresh(await call("POST", API.refresh, {}), "");
        toast(out.ok ? "MCP servers refreshed. " + out.text + " Restart Jarvis for new tools to appear." : out.text,
              out.ok ? "info" : "error");
      } catch (err) {
        toast("Couldn't refresh MCP servers: " + err.message, "error");
      }
    });
  }

  async function toggleEnabled(server) {
    const turningOn = !server.enabled;
    let result = null;
    await busyWhile(server.name, turningOn ? "Switching on\u2026" : "Switching off\u2026", async () => {
      try {
        result = await call("POST", API.server(server.name) + "/" + (turningOn ? "enable" : "disable"));
      } catch (err) {
        toast(err.message, "error");
        return;
      }
      // A server that was never listed has no tools to offer yet: connect now,
      // so "switch on" ends with the tools visible instead of a second step.
      if (turningOn && result && result.needs_refresh) {
        S.busy.set(server.name, "Connecting\u2026");
        render();
        try {
          const out = describeRefresh(await call("POST", API.refresh, { server: result.slug }), server.name);
          toast(out.text, out.ok ? "info" : "error");
        } catch (err) {
          toast("Switched on, but couldn't connect: " + err.message, "error");
        }
      }
    });
  }

  async function toggleTrusted(server) {
    if (!server.trusted) {
      const host = global.JarvisHost;
      const confirmFn = host && typeof host.confirm === "function" ? host.confirm : null;
      if (confirmFn) {
        const yes = await confirmFn({
          title: "Trust " + server.name + "?",
          body: "Its tools will run without asking you first \u2014 including when Jarvis runs unattended " +
                "(scheduled jobs, chat gateways). Only do this for a server whose every tool you are happy to have run on its own.",
          confirmLabel: "Trust it", cancelLabel: "Keep asking", level: "warn", focusCancel: true,
        });
        if (!yes) return;
      }
    }
    await busyWhile(server.name, "Saving\u2026", async () => {
      try {
        await call("POST", API.server(server.name) + "/" + (server.trusted ? "untrust" : "trust"));
      } catch (err) {
        toast(err.message, "error");
      }
    });
  }

  async function removeServer(server) {
    const host = global.JarvisHost;
    if (host && typeof host.confirm === "function") {
      const yes = await host.confirm({
        title: "Remove " + server.name + "?",
        body: "This deletes its entry from mcp_config.json (a .bak copy is kept) and forgets its tools. " +
              "The program itself is not touched.",
        confirmLabel: "Remove", cancelLabel: "Keep it", level: "danger", focusCancel: true,
      });
      if (!yes) return;
    }
    await busyWhile(server.name, "Removing\u2026", async () => {
      try {
        await call("DELETE", API.server(server.name));
        S.open.delete(server.name);
        S.toolsOpen.delete(server.name);
        toast(server.name + " removed.", "info");
      } catch (err) {
        toast(err.message, "error");
      }
    });
  }

  function openEditor(mode, server) {
    S.editor = { mode: mode, server: server || null, error: "", saving: false };
    render();
  }

  function closeEditor() {
    S.editor = null;
    render();
  }

  // Shows a problem inside the open editor without rebuilding it.
  function showEditorError(message) {
    const ed = S.editor;
    if (!ed) return;
    ed.error = message;
    if (ed.ui) {
      ed.ui.error.textContent = message;
      ed.ui.error.hidden = !message;
      ed.ui.save.disabled = false;
      ed.ui.save.textContent = ed.ui.label;
    }
  }

  async function saveServer(built, replaceName) {
    const ed = S.editor;
    if (!ed) return;
    ed.saving = true;
    showEditorError("");
    if (ed.ui) { ed.ui.save.disabled = true; ed.ui.save.textContent = "Saving\u2026"; }
    let result = null;
    try {
      const body = { name: built.name, spec: built.spec };
      if (replaceName) body.replace = replaceName;
      result = await call("POST", API.save, body);
    } catch (err) {
      ed.saving = false;
      showEditorError(err.message || "Couldn't save.");
      return;
    }
    S.editor = null;
    if (replaceName && replaceName !== built.name) {
      if (S.open.delete(replaceName)) S.open.add(built.name);
      if (S.toolsOpen.delete(replaceName)) S.toolsOpen.add(built.name);
    }
    await load();
    if (result && result.needs_refresh) {
      const row = findServer(built.name);
      if (row) await refreshServer(row);
    } else {
      toast((result && result.created ? "Added " : "Saved ") + built.name + ".", "info");
    }
  }

  /* ======================================================================
   * 6. wiring
   * ==================================================================== */

  function overlay() { return $("mcp-overlay"); }
  function isOpen() { const o = overlay(); return !!o && !o.hidden; }

  function open() {
    const o = overlay();
    if (!o) return;
    o.hidden = false;
    S.editor = null;
    render();
    load();
  }

  function close() {
    const o = overlay();
    if (o) o.hidden = true;
    S.editor = null;
  }

  function wire() {
    const o = overlay();
    if (!o) return;
    const on = (id, type, fn) => { const n = $(id); if (n) n.addEventListener(type, fn); };
    on("mcp-close", "click", close);
    on("btn-mcp-refresh", "click", refreshAll);
    on("btn-mcp-add", "click", () => openEditor("add"));
    on("mcp-search", "input", (e) => { S.query = e.target.value; renderChips(); renderList(); });
    o.addEventListener("click", (e) => { if (e.target === o) close(); });
    document.addEventListener("keydown", (e) => {
      if (!isOpen()) return;
      // A JarvisUI dialog (the Trust / Remove confirmation) owns Esc while it is up.
      if (document.querySelector(".jui-backdrop")) return;
      const typing = /^(INPUT|TEXTAREA|SELECT)$/.test((e.target && e.target.tagName) || "");
      if (e.key === "Escape") {
        // Esc backs out one level: the editor first, then the panel.
        if (S.editor) { if (!S.editor.saving) closeEditor(); } else close();
        e.stopPropagation();
        return;
      }
      if (e.key === "/" && !typing && !S.editor) {
        e.preventDefault();
        const q = $("mcp-search");
        if (q) q.focus();
      }
    }, true);
  }

  global.JarvisMcp = { open: open, close: close, isOpen: isOpen, refresh: load, _pure: pure };

  // No `document` (the Node test loads this file bare) = export only the pure half.
  if (typeof document !== "undefined") {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", wire);
    else wire();
  }
})(typeof window !== "undefined" ? window : globalThis);
