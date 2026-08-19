"""Safe condition evaluation for jarvis run-steps.

A step's "if" / "unless" accepts two formats:

1. Dict form (recommended) - AND of equality checks. A list value means
   "equals any of these" (an OR just for that one variable):

       {"env": "prod", "region": ["us", "eu"]}

   matches when env == "prod" AND region is "us" or "eu".

2. String-expression form - for anything the dict form can't say cleanly.
   Supports ==, !=, <, >, <=, >=, and, or, not, and parentheses. A bare
   "=" is treated the same as "==". An unquoted bare word that isn't one
   of the command's variable names is treated as a literal string, so
   `env = prod` works without quoting prod (matching how people naturally
   write these by hand):

       "env == 'prod' and (region == 'us' or region == 'eu')"
       "env = prod and branch != main"

Both forms are evaluated by a small, explicitly allow-listed AST walker,
never by eval()/exec(). Anything outside the allow list (function calls,
attribute access, subscripts, comprehensions, etc.) raises ConditionError
instead of running, so a condition can never execute arbitrary code.
"""

import ast
import re

# A lone "=" becomes "==", but ==, !=, <=, >= are left alone.
_BARE_EQUALS_RE = re.compile(r"(?<![=!<>])=(?!=)")

_BOOL_OPS = (ast.And, ast.Or)
_CMP_OPS = (ast.Eq, ast.NotEq, ast.Lt, ast.Gt, ast.LtE, ast.GtE)


class ConditionError(Exception):
    """Raised when an 'if'/'unless' condition can't be parsed or evaluated."""


def _resolve_leaf(node, values):
    if isinstance(node, ast.Name):
        if node.id in values:
            return values[node.id]
        return node.id  # bare word, not a known var -> treat as a literal string
    if isinstance(node, ast.Constant):
        return node.value
    raise ConditionError(f"unsupported term near '{ast.dump(node)}'")


def _as_comparable(a, b):
    """Compare numerically if both sides look like numbers, else as strings."""
    try:
        return float(a), float(b)
    except (TypeError, ValueError):
        return str(a), str(b)


def _truthy(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("", "0", "false", "no", "none")


def _eval(node, values):
    if isinstance(node, ast.BoolOp):
        if not isinstance(node.op, _BOOL_OPS):
            raise ConditionError("only 'and' / 'or' are supported")
        results = [_eval(v, values) for v in node.values]
        return all(results) if isinstance(node.op, ast.And) else any(results)

    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _eval(node.operand, values)

    if isinstance(node, ast.Compare):
        left = _resolve_leaf(node.left, values)
        ok = True
        for op, comparator in zip(node.ops, node.comparators):
            if not isinstance(op, _CMP_OPS):
                raise ConditionError("unsupported comparison operator")
            right = _resolve_leaf(comparator, values)
            a, b = _as_comparable(left, right)
            if isinstance(op, ast.Eq):
                step_ok = a == b
            elif isinstance(op, ast.NotEq):
                step_ok = a != b
            elif isinstance(op, ast.Lt):
                step_ok = a < b
            elif isinstance(op, ast.Gt):
                step_ok = a > b
            elif isinstance(op, ast.LtE):
                step_ok = a <= b
            else:
                step_ok = a >= b
            ok = ok and step_ok
            left = right
        return ok

    if isinstance(node, (ast.Name, ast.Constant)):
        return _truthy(_resolve_leaf(node, values))

    raise ConditionError(f"unsupported expression near '{ast.dump(node)}'")


def eval_expr(expr, values):
    """Evaluate a string condition, e.g. "env == 'prod' and count > 2"."""
    normalized = _BARE_EQUALS_RE.sub("==", expr)
    try:
        tree = ast.parse(normalized, mode="eval")
    except Exception as e:
        raise ConditionError(f"invalid condition {expr!r}: {e}")
    return bool(_eval(tree.body, values))


def eval_dict(cond, values, known_vars):
    """Evaluate a dict condition, e.g. {"env": "prod", "region": ["us", "eu"]}."""
    for key, expected in cond.items():
        if key not in known_vars:
            valid = ", ".join(known_vars) or "(none)"
            raise ConditionError(
                f"condition refers to unknown variable '{key}' "
                f"(this command's variables are: {valid})"
            )
        actual = values.get(key)
        if isinstance(expected, list):
            if str(actual) not in [str(v) for v in expected]:
                return False
        else:
            if str(actual) != str(expected):
                return False
    return True


def matches(cond, values, known_vars):
    """Evaluate a dict-form or string-form condition. None always matches."""
    if cond is None:
        return True
    if isinstance(cond, dict):
        return eval_dict(cond, values, known_vars)
    if isinstance(cond, str):
        return eval_expr(cond, values)
    raise ConditionError(
        f"'if'/'unless' must be a string or object, got {type(cond).__name__}"
    )


def step_matches(step, values, known_vars):
    """A step runs when its 'if' is true (or absent) AND its 'unless' is false (or absent)."""
    if_cond = step.get("if")
    unless_cond = step.get("unless")
    if if_cond is not None and not matches(if_cond, values, known_vars):
        return False
    if unless_cond is not None and matches(unless_cond, values, known_vars):
        return False
    return True


def describe(cond):
    """Render a condition back to a short human-readable string, for --help / logs."""
    if cond is None:
        return None
    if isinstance(cond, str):
        return cond
    if isinstance(cond, dict):
        parts = []
        for k, v in cond.items():
            parts.append(f"{k} in {v}" if isinstance(v, list) else f"{k} == {v}")
        return " and ".join(parts)
    return str(cond)
