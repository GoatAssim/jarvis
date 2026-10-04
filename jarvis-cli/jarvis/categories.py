"""Category names - the one normalisation every surface that has them shares.

L.11 (daemon categories) is the first user; L.14 (command categories) is the
second and must not grow a second copy of these rules. L.14.3 is explicit
that only the *rules* are shared: a daemon's vocabulary and a command's
vocabulary stay separate (owner decision Q5, 2026-10-01), so nothing here
knows what a daemon or a command is.

The same rules are implemented in `web/public/category-input.js`
(`JarvisCategories`). `tests/test_categories.py` runs one corpus through both
and fails if they ever disagree, so a name the editor accepted can never be
re-spelled by the registry when it is saved.

Rules, in the order they are applied to one name:

  1. a control character becomes a space (a pasted tab/newline must not
     survive into a chip, and must not silently glue two words together);
  2. any run of whitespace collapses to one space, and the ends are trimmed;
  3. an empty result is "no name" - dropped from a list, never an error, so an
     untouched blank row in a form is harmless;
  4. longer than MAX_NAME_LEN is cut (lenient / load path) or refused (strict /
     write path) - a person is told, rather than handed a different name.

A list additionally gets case-insensitive uniqueness (the first spelling
wins, so `Utility` then `utility` is just `Utility`) and a cap of
MAX_PER_ITEM so a card never turns into a wall of chips.

Two modes, because the two callers need opposite things:

  lenient (default) - for reading a file a person may have hand-edited: fix
      what can be fixed, drop what cannot, never refuse. A registry that fails
      to load because of one odd category would take every daemon with it.
  strict - for a write: say what is wrong. Silently dropping the ninth
      category someone just typed would look like a save that lost data.
"""

import re
import unicodedata

MAX_NAME_LEN = 24
MAX_PER_ITEM = 8

# An explicit whitespace class, not `\s`: Python's `\s` and JavaScript's `\s`
# disagree about a few characters (U+FEFF, U+001C-001F, U+0085), and the two
# implementations have to agree exactly. Control characters are handled
# before this runs (rule 1), so only the visible-space family is left.
_WS = re.compile("[ \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+")


def normalize_name(value, truncate=True):
    """One category name, cleaned. "" means there was nothing usable.

    `truncate=False` returns the full cleaned text so a strict caller can
    compare its length against MAX_NAME_LEN instead of losing the overflow.
    """
    if not isinstance(value, str):
        return ""
    text = "".join(" " if unicodedata.category(ch) == "Cc" else ch
                   for ch in value)
    text = _WS.sub(" ", text).strip(" ")
    if truncate and len(text) > MAX_NAME_LEN:
        # len() counts code points, as the JS side does via Array.from().
        text = text[:MAX_NAME_LEN].rstrip(" ")
    return text


def key_of(name):
    """The identity of a category: case-insensitive, so `Utility` == `utility`."""
    return normalize_name(name).lower()


def normalize_list(items, strict=False):
    """(clean_list, error). `error` is "" unless `strict` found a problem.

    A bare string is read as a one-item list, so a caller handed a single
    `--category` value does not have to wrap it.
    """
    if items is None:
        return [], ""
    if isinstance(items, str):
        items = [items]
    if not isinstance(items, (list, tuple)):
        return [], ("categories must be a list of names" if strict else "")

    out = []
    seen = set()
    for raw in items:
        if not isinstance(raw, str):
            if strict:
                return [], "category names must be text"
            continue
        full = normalize_name(raw, truncate=False)
        if not full:
            continue
        if len(full) > MAX_NAME_LEN:
            if strict:
                return [], (f"category '{full[:MAX_NAME_LEN]}...' is longer "
                            f"than {MAX_NAME_LEN} characters")
            full = full[:MAX_NAME_LEN].rstrip(" ")
        key = full.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(full)

    if len(out) > MAX_PER_ITEM:
        if strict:
            return [], f"at most {MAX_PER_ITEM} categories per item"
        out = out[:MAX_PER_ITEM]
    return out, ""
