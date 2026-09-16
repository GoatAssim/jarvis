"""conversation_search — let the model find things in past chats.

conversations.other_conversations_context() deliberately gives the model only
a title and a one-line gist of every OTHER conversation, never their
messages, so unrelated chats can't quietly leak into the current one. That's
the right default, but it leaves a real gap: when the user says "what was
that Postgres thing we worked out last month", the model can see a title
that may or may not mention Postgres and nothing else.

This tool is the deliberate, on-request exception. The user has to ask; the
model then searches transcripts and gets back snippets. Retrieval is opt-in
and visible in the tool trace, rather than ambient.

Read-only, so no confirm gate — but note it CAN surface text from other
conversations, which is exactly why it's a tool call the user can see rather
than something folded into the system prompt.
"""

from .. import conv_search


def tool_search_conversations(args, context=None):
    args = args or {}
    query = (args.get("query") or "").strip()
    if not query:
        return {"needs_clarification": True,
                "message": "What should I search for in your past conversations?"}
    mode = (args.get("mode") or "words").strip().lower()
    try:
        results = conv_search.search(
            query,
            mode=mode,
            limit=int(args.get("limit") or 10),
            include_tools=bool(args.get("include_tools")),
            conv_id=(args.get("conversation_id") or "").strip() or None,
            since=(args.get("since") or "").strip() or None,
            until=(args.get("until") or "").strip() or None,
        )
    except conv_search.SearchError as e:
        return {"needs_clarification": True, "message": str(e)}
    except Exception as e:  # noqa: BLE001 — a handler must never raise
        return {"error": "%s: %s" % (type(e).__name__, e)}

    payload = {"query": query, "mode": mode,
               **conv_search.summarize(results), "results": results}
    if not results:
        payload["note"] = (
            "Nothing matched. Try fewer or different words — 'words' mode "
            "requires every term to appear somewhere in the same turn."
        )
    return payload


TOOL_SCHEMAS = [
    {
        "name": "search_conversations",
        "description": (
            "Full-text search across the user's past conversations, returning "
            "matching turns with snippets and the conversation each came from. "
            "Use when they refer to something from an earlier chat you can't see "
            "— 'what did we decide about X', 'find that command I used last "
            "week', 'which chat was the Postgres one'. You normally only get "
            "other conversations' titles, so this is the only way to recall what "
            "was actually said in them. Not needed for the current conversation, "
            "which is already in your context."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What to look for. In the default 'words' mode every "
                                   "term must appear in the same turn, in any order, so "
                                   "prefer two or three distinctive words over a long "
                                   "remembered sentence.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["words", "phrase", "regex"],
                    "description": "'words' (default, all terms, any order), 'phrase' "
                                   "(exact substring), 'regex' (a real regular expression).",
                },
                "limit": {"type": "integer", "description": "Max conversations to return. Default 10."},
                "conversation_id": {
                    "type": "string",
                    "description": "Restrict to one conversation id, e.g. to find "
                                   "something earlier in a long chat.",
                },
                "since": {"type": "string", "description": "Only turns on or after this date (YYYY-MM-DD)."},
                "until": {"type": "string", "description": "Only turns on or before this date (YYYY-MM-DD)."},
                "include_tools": {
                    "type": "boolean",
                    "description": "Also search tool arguments and results from those turns. "
                                   "Default false — otherwise a search for a topic matches "
                                   "any file path that happened to contain the word.",
                },
            },
            "required": ["query"],
        },
    },
]

TOOLS = {"search_conversations": tool_search_conversations}

# Joins the existing "memory" group rather than starting a new one: from the
# model's point of view "what did we say about X" and memory_search are the
# same intent, and a message that routes to one should offer the other. It
# also means this tool inherits the memory group's existing keyword coverage
# instead of depending solely on its own.
TOOL_GROUP = "memory"

TOOL_KEYWORDS = {
    "search_conversations": {
        "past conversations": 10, "previous chat": 9, "earlier chat": 9,
        "we talked about": 9, "we discussed": 9, "what did we decide": 10,
        "search my chats": 10, "find that conversation": 10,
        "last time we": 8, "old conversation": 9,
    },
}

TOOL_CONFIRM_REQUIRED = set()   # read-only
TOOL_AI_REVIEW = set()

# Snippets are the expensive part: three per conversation, each up to ~200
# chars, times ten conversations. At medium the raw user/jarvis text is
# dropped (the snippet already contains the matched context); at low only
# enough to identify the conversation survives.
TOOL_RESULT_SPECS = {
    "search_conversations": {
        "list_item_drop": {
            "results": {
                "medium": ["total_exchanges"],
                "low": ["total_exchanges", "matches", "updated_at"],
            },
        },
    },
}
