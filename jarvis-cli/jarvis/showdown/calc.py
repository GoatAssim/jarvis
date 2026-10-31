"""Stat and damage estimates.

This is an ESTIMATE, on purpose. It uses the real Gen 9 damage formula but it does not
know the opponent's ability, item or exact spread, and it ignores crits, multi-hit moves
and most abilities. The engine uses it to rank options; it is never shown as exact.

Random Battle assumption for the opponent: 85 EVs in every stat, 31 IVs, a neutral
nature. Our own Pokemon use the real stats from the server's `|request|`.
"""

from .data import effectiveness

RANDOM_EV = 85
RANDOM_IV = 31
BOOST_STATS = ("atk", "def", "spa", "spd", "spe", "accuracy", "evasion")

_WEATHER_BOOST = {("sunnyday", "Fire"): 1.5, ("sunnyday", "Water"): .5,
                  ("raindance", "Water"): 1.5, ("raindance", "Fire"): .5,
                  ("desolateland", "Fire"): 1.5, ("desolateland", "Water"): 0,
                  ("primordialsea", "Water"): 1.5, ("primordialsea", "Fire"): 0}


def calc_stat(base, level, is_hp=False, ev=RANDOM_EV, iv=RANDOM_IV):
    """A stat at `level` from a base stat (neutral nature)."""
    try:
        base, level = int(base), int(level)
    except (TypeError, ValueError):
        return 0
    core = ((2 * base + iv + ev // 4) * level) // 100
    return core + level + 10 if is_hp else core + 5


def estimate_stats(base_stats, level):
    """{hp, atk, def, spa, spd, spe} for an opponent from its base stats."""
    out = {}
    for stat in ("hp", "atk", "def", "spa", "spd", "spe"):
        out[stat] = calc_stat(base_stats.get(stat, 0), level, is_hp=(stat == "hp"))
    return out


def stage_mult(stage):
    stage = max(-6, min(6, int(stage or 0)))
    return (2 + stage) / 2 if stage >= 0 else 2 / (2 - stage)


def effective_speed(stats, boosts, status):
    spe = (stats or {}).get("spe", 0) * stage_mult((boosts or {}).get("spe", 0))
    return spe * (0.5 if status == "par" else 1)


def damage_range(att, dfn, move, weather=None):
    """Damage `att` does to `dfn` with `move` (a moves.json entry).

    att: {level, types, stats, boosts, status, tera, item}
    dfn: {types, stats, boosts, tera}
    Returns {"min_pct", "max_pct", "eff", "power"} with percentages of the defender's
    MAX HP, or None for a status move / unknown move.
    """
    if not isinstance(move, dict) or move.get("category") == "Status":
        return None
    mtype = move.get("type") or "Normal"
    level = int(att.get("level") or 100)
    dtypes = [dfn.get("tera")] if dfn.get("tera") else list(dfn.get("types") or [])
    eff = effectiveness(mtype, dtypes)
    dmax = (dfn.get("stats") or {}).get("hp") or 1

    fixed = move.get("damage")
    if fixed == "level":
        pct = 0.0 if eff == 0 else level * 100.0 / dmax
        return {"min_pct": pct, "max_pct": pct, "eff": eff, "power": 0}
    if isinstance(fixed, int):
        pct = 0.0 if eff == 0 else fixed * 100.0 / dmax
        return {"min_pct": pct, "max_pct": pct, "eff": eff, "power": 0}

    power = int(move.get("basePower") or 0)
    if power <= 0:
        power = 60  # variable-power move (Low Kick, Gyro Ball, ...): a neutral guess
    physical = move.get("category") == "Physical"
    a_stat, d_stat = ("atk", "def") if physical else ("spa", "spd")
    atk = (att.get("stats") or {}).get(a_stat, 0) * stage_mult((att.get("boosts") or {}).get(a_stat, 0))
    dfe = (dfn.get("stats") or {}).get(d_stat, 0) * stage_mult((dfn.get("boosts") or {}).get(d_stat, 0))
    if atk <= 0 or dfe <= 0:
        return None

    base = ((2 * level) // 5 + 2) * power * atk / dfe
    base = int(base // 50) + 2

    mod = 1.0
    own_types = [att.get("tera")] if att.get("tera") else []
    own_types += list(att.get("types") or [])
    if mtype in own_types:
        mod *= 1.5
    mod *= eff
    mod *= _WEATHER_BOOST.get((weather, mtype), 1)
    if att.get("status") == "brn" and physical:
        mod *= 0.5
    item = att.get("item")
    if item == "lifeorb":
        mod *= 1.3
    elif item == "choiceband" and physical:
        mod *= 1.5
    elif item == "choicespecs" and not physical:
        mod *= 1.5

    high = int(base * mod)
    low = int(base * mod * 0.85)
    return {"min_pct": low * 100.0 / dmax, "max_pct": high * 100.0 / dmax, "eff": eff, "power": power}


def accuracy_of(move):
    acc = (move or {}).get("accuracy", True)
    if acc is True or acc is None:
        return 1.0
    try:
        return max(0.0, min(1.0, float(acc) / 100.0))
    except (TypeError, ValueError):
        return 1.0
