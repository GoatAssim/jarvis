"""Generic, table-driven trimming of tool-call *results* before they go
back to the model \u2014 the result-side counterpart to tools.py's
compact_schemas_for_prompt / name_only_schemas_for_prompt (which shrink
what the model is told a tool *accepts*, before it's even called). This
shapes what a tool actually *returns*, after it runs.

Driven by the same capacity-mode system as everything else (see
ai_client.PROMPT_MODE_DEFS's tool_result_verbosity knob), one level per
mode: "full" (400% Capacity) keeps every field a tool returns untouched;
"medium" (100% Capacity) drops fields a tool's author flagged as merely
nice-to-have; "low" (50% Capacity) keeps only what's flagged necessary.

Unclassified tools (no entry in TOOL_RESULT_SPECS below) are returned
completely unchanged at every verbosity \u2014 this is an opt-in allowlist,
never a blind filter that could silently drop something a tool actually
needs to answer with. Add an entry below for any tool whose result is
large enough to be worth shaping; ai_client._make_tool_executor calls
shape_result() for every tool automatically, so nothing else changes.

Each spec is a dict of any of these keys (all optional):

  drop_fields      \u2014 {"medium": [...], "low": [...]}: top-level keys
                     removed outright at that verbosity. Each level's list
                     is independent (self-describing), not a cumulative
                     diff \u2014 a key you want gone at both levels needs to
                     be listed under both.
  truncate_fields   \u2014 {"field": {"medium": n, "low": n}}: a top-level
                     string field capped to n chars (with a "\u2026(trimmed)"
                     suffix when actually cut). Omit a level to leave that
                     field alone at that verbosity.
  list_item_drop    \u2014 {"field": {"medium": [...], "low": [...]}}: for a
                     top-level field that's a list of dicts (e.g. search
                     results), remove these keys from every item at that
                     verbosity \u2014 without changing how many items there
                     are (item *count* stays a job for the tool itself,
                     e.g. max_results, not this module).
  list_item_truncate \u2014 {"field": {"item_field": {"medium": n, "low": n}}}:
                     same idea as truncate_fields, but for a string field
                     inside every item of a list field.

To add a new mode later that needs a *different* verbosity level than the
three below, just use one of "full"/"medium"/"low" as its
tool_result_verbosity \u2014 nothing here is tied to mode names, only to
these three levels.
"""

_TRIM_SUFFIX = "\u2026(trimmed)"


def _truncate(value, n):
    if not isinstance(value, str) or n is None or len(value) <= n:
        return value
    return value[:n].rstrip() + _TRIM_SUFFIX


def shape_result(name, result, verbosity):
    """Return a shaped copy of `result` for tool `name` at `verbosity`
    ("full"/"medium"/"low"). Never mutates the caller's dict, never
    raises \u2014 anything not a plain dict, any verbosity other than
    "medium"/"low", or any tool with no spec below is passed through
    exactly as-is."""
    if verbosity not in ("medium", "low") or not isinstance(result, dict):
        return result
    spec = TOOL_RESULT_SPECS.get(name)
    if not spec:
        return result

    out = dict(result)

    for key in spec.get("drop_fields", {}).get(verbosity, ()) or ():
        out.pop(key, None)

    for field, caps in (spec.get("truncate_fields") or {}).items():
        if field in out:
            out[field] = _truncate(out[field], caps.get(verbosity))

    for field, drops in (spec.get("list_item_drop") or {}).items():
        keys_to_drop = drops.get(verbosity)
        items = out.get(field)
        if not keys_to_drop or not isinstance(items, list):
            continue
        out[field] = [
            {k: v for k, v in item.items() if k not in keys_to_drop}
            if isinstance(item, dict) else item
            for item in items
        ]

    for field, item_fields in (spec.get("list_item_truncate") or {}).items():
        items = out.get(field)
        if not isinstance(items, list):
            continue
        new_items = []
        for item in items:
            if isinstance(item, dict):
                new_item = dict(item)
                for item_field, caps in item_fields.items():
                    if item_field in new_item:
                        new_item[item_field] = _truncate(new_item[item_field], caps.get(verbosity))
                new_items.append(new_item)
            else:
                new_items.append(item)
        out[field] = new_items

    return out


TOOL_RESULT_SPECS = {
    # Screenshot: the image itself never reaches the model (see the
    # JARVIS_MEDIA pattern) \u2014 only these bookkeeping fields do. Pixel
    # dimensions/file size are nice for a human debugging, not needed for
    # the model to tell the user "done, see the screenshot".
    "take_screenshot": {
        "drop_fields": {
            "low": ["width", "height", "bytes"],
        },
    },
    # web_search: URLs/titles are necessary; snippets are semi-necessary
    # (help the model pick which to fetch, but a longer one than needed
    # just burns tokens before web_fetch gets the real text anyway).
    "web_search": {
        "list_item_truncate": {
            "results": {
                "snippet": {"medium": 160, "low": 80},
            },
        },
    },
    # web_fetch: "text" is by far the largest field here (already capped
    # once by web_tools.FETCH_MAX_CHARS) \u2014 cut it further as capacity
    # drops. url/title/truncated stay untouched at every level.
    "web_fetch": {
        "truncate_fields": {
            "text": {"medium": 2500, "low": 900},
        },
    },
    # Everything search: path/name/is_folder are what a reveal/open action
    # needs; size/modified-date are semi-necessary context that's rarely
    # the deciding factor once there's a matching path.
    "search_files": {
        "list_item_drop": {
            "results": {
                "low": ["size_bytes", "date_modified"],
            },
        },
    },
    # ytdl_info: title/duration/webpage_url/is_live/available_qualities/
    # ffmpeg_available are necessary to decide what/whether to download;
    # uploader/view_count/upload_date/extractor/subtitle info are
    # semi-necessary color that's rarely why the user asked.
    "ytdl_info": {
        "drop_fields": {
            "medium": ["upload_date"],
            "low": [
                "uploader", "extractor", "view_count", "upload_date",
                "subtitle_langs", "auto_caption_langs",
            ],
        },
    },
    # ytdl_formats: format_id/ext/resolution/filesize are what's needed to
    # actually pick a format for ytdl_download; fps/codec/bitrate/note are
    # semi-necessary detail for an unusually picky request.
    "ytdl_formats": {
        "list_item_drop": {
            "formats": {
                "low": ["fps", "vcodec", "acodec", "abr_kbps", "note"],
            },
        },
    },
    # git_run: ok/exit_code/command/cwd are necessary to know what
    # happened; stdout/stderr are semi-necessary (already capped once in
    # git_tools.py) \u2014 cut further as capacity drops, since a long diff
    # or log dump is exactly the kind of thing "50% Capacity" exists for.
    "git_run": {
        "truncate_fields": {
            "stdout": {"medium": 1200, "low": 400},
            "stderr": {"medium": 500, "low": 200},
        },
    },

    # ── the seven read-only get_* tools ─────────────────────────────────
    # Already tiny (a handful of short fields, no lists/long strings), so
    # there's little to trim \u2014 each spec below only drops one or two
    # fields, and only at "low", never "medium". Included anyway for the
    # same reason every tool is: consistency, and because a few bytes
    # dropped from *every single* get_* call in a long ultra-mode session
    # adds up even when it's small per call.
    "get_datetime": {
        # date/time are necessary; weekday/timezone/iso are redundant with
        # them for most asks ("what time is it").
        "drop_fields": {"low": ["weekday", "timezone", "iso"]},
    },
    "get_battery": {
        # has_battery/percent/plugged_in answer "what's my battery at";
        # time_remaining is a nice extra, not the necessary part.
        "drop_fields": {"low": ["time_remaining"]},
    },
    "get_wifi_info": {
        # connected (+ ssid when true) is necessary; signal strength and
        # the diagnostic "note" are semi-necessary detail.
        "drop_fields": {"low": ["signal", "note"]},
    },
    "get_location": {
        # country/timezone are necessary; the privacy disclaimer note is
        # semi-necessary (useful once, not on every call).
        "drop_fields": {"low": ["note"]},
    },
    "get_system_info": {
        # os/os_version/hostname/architecture answer "what OS is this";
        # uptime is semi-necessary extra context.
        "drop_fields": {"low": ["uptime"]},
    },
    "get_disk_usage": {
        # total/free/percent_used are necessary; used_gb is derivable from
        # total-free and rarely the number someone actually asked for.
        "drop_fields": {"low": ["used_gb"]},
    },
    "get_memory_usage": {
        # same reasoning as get_disk_usage.
        "drop_fields": {"low": ["used_gb"]},
    },

    # ── Playnite ─────────────────────────────────────────────────────────
    # query_games/find_game both return a "games" list of _list_game dicts
    # (id, name, installed, favorite, source, status, hours) \u2014 id/name/
    # installed are what's needed to talk about or launch a game; the rest
    # is semi-necessary color that matters for some asks ("what have I not
    # finished") but not most ("launch Hades").
    "playnite_query_games": {
        "list_item_drop": {
            "games": {"low": ["favorite", "source", "status", "hours"]},
        },
    },
    "playnite_find_game": {
        "list_item_drop": {
            "games": {"low": ["favorite", "source", "status", "hours"]},
        },
    },
    # get_game is a deliberate single-game detail lookup, so it keeps
    # everything at "medium" \u2014 only "low" strips it back to the same
    # necessary core as the list tools above (plus its actions, which are
    # necessary for anything play-related).
    "playnite_get_game": {
        "drop_fields": {
            "low": ["favorite", "source", "status", "hours", "genres", "tags", "categories"],
        },
    },
    # Each action needs id/name/isPlayAction(/isLibraryPluginAction) to be
    # launchable; type/path are semi-necessary (path is already clipped to
    # 140 chars in _compact_action).
    "playnite_list_game_actions": {
        "list_item_drop": {
            "actions": {"low": ["type", "path"]},
        },
    },
    # Frequent list is the same _list_game shape as query/find, plus
    # cache bookkeeping (use_count/last_used) that's about ranking, not
    # about the game itself.
    "playnite_list_frequent": {
        "list_item_drop": {
            "games": {"low": ["favorite", "source", "status", "hours", "use_count", "last_used"]},
        },
    },
    # Library-wide breakdown dicts (bySource/byCompletionStatus/topGenres)
    # are the priciest part of this one \u2014 the headline totals stay at
    # every level.
    "playnite_library_stats": {
        "drop_fields": {
            "low": ["bySource", "byCompletionStatus", "topGenres"],
        },
    },
}
