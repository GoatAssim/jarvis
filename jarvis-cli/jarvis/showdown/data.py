"""Static game data: the type chart (built in) and the moves / pokedex tables
(fetched once from the Showdown client's public data files, cached on disk).

Nothing here raises. A missing table means "unknown": the engine then scores
without damage numbers instead of crashing a battle.
"""

import json
import re
import threading
from pathlib import Path

ENCODING = "utf-8"
JARVIS_DIR = Path.home() / ".jarvis"
DATA_DIR = JARVIS_DIR / "showdown" / "data"

# Public data files served next to the client. UNVERIFIED from the build sandbox
# (it has no internet): `jarvis showdown` first run will say if a URL is wrong.
DATA_URLS = {
    "moves": "https://play.pokemonshowdown.com/data/moves.json",
    "pokedex": "https://play.pokemonshowdown.com/data/pokedex.json",
}
FETCH_TIMEOUT = 20

TYPES = ("Normal", "Fire", "Water", "Electric", "Grass", "Ice", "Fighting", "Poison", "Ground",
         "Flying", "Psychic", "Bug", "Rock", "Ghost", "Dragon", "Dark", "Steel", "Fairy")

# attacking type -> {defending type: multiplier}; anything not listed is 1.
_CHART = {
    "Normal": {"Rock": .5, "Steel": .5, "Ghost": 0},
    "Fire": {"Fire": .5, "Water": .5, "Grass": 2, "Ice": 2, "Bug": 2, "Rock": .5, "Dragon": .5, "Steel": 2},
    "Water": {"Fire": 2, "Water": .5, "Grass": .5, "Ground": 2, "Rock": 2, "Dragon": .5},
    "Electric": {"Water": 2, "Electric": .5, "Grass": .5, "Ground": 0, "Flying": 2, "Dragon": .5},
    "Grass": {"Fire": .5, "Water": 2, "Grass": .5, "Poison": .5, "Ground": 2, "Flying": .5, "Bug": .5,
              "Rock": 2, "Dragon": .5, "Steel": .5},
    "Ice": {"Fire": .5, "Water": .5, "Grass": 2, "Ice": .5, "Ground": 2, "Flying": 2, "Dragon": 2, "Steel": .5},
    "Fighting": {"Normal": 2, "Ice": 2, "Poison": .5, "Flying": .5, "Psychic": .5, "Bug": .5, "Rock": 2,
                 "Ghost": 0, "Dark": 2, "Steel": 2, "Fairy": .5},
    "Poison": {"Grass": 2, "Poison": .5, "Ground": .5, "Rock": .5, "Ghost": .5, "Steel": 0, "Fairy": 2},
    "Ground": {"Fire": 2, "Electric": 2, "Grass": .5, "Poison": 2, "Flying": 0, "Bug": .5, "Rock": 2, "Steel": 2},
    "Flying": {"Electric": .5, "Grass": 2, "Fighting": 2, "Bug": 2, "Rock": .5, "Steel": .5},
    "Psychic": {"Fighting": 2, "Poison": 2, "Psychic": .5, "Dark": 0, "Steel": .5},
    "Bug": {"Fire": .5, "Grass": 2, "Fighting": .5, "Poison": .5, "Flying": .5, "Psychic": 2, "Ghost": .5,
            "Dark": 2, "Steel": .5, "Fairy": .5},
    "Rock": {"Fire": 2, "Ice": 2, "Fighting": .5, "Ground": .5, "Flying": 2, "Bug": 2, "Steel": .5},
    "Ghost": {"Normal": 0, "Psychic": 2, "Ghost": 2, "Dark": .5},
    "Dragon": {"Dragon": 2, "Steel": .5, "Fairy": 0},
    "Dark": {"Fighting": .5, "Psychic": 2, "Ghost": 2, "Dark": .5, "Fairy": .5},
    "Steel": {"Fire": .5, "Water": .5, "Electric": .5, "Ice": 2, "Rock": 2, "Steel": .5, "Fairy": 2},
    "Fairy": {"Fire": .5, "Fighting": 2, "Poison": .5, "Dragon": 2, "Dark": 2, "Steel": .5},
}

_ID = re.compile(r"[^a-z0-9]")
_SAFE = re.compile(r"[^A-Za-z0-9 .'\-:%/()+,]")


def to_id(text):
    """Showdown's id form: lower case letters and digits only."""
    return _ID.sub("", str(text or "").lower())


def clean(text, limit=40):
    """Text that came from the server, made safe to show or put in a prompt: a short
    run of ordinary characters only (no markup, newlines or control characters)."""
    return _SAFE.sub("", str(text or "")).strip()[:limit]


def effectiveness(move_type, defender_types):
    """Type multiplier of `move_type` against a list of defending types."""
    row = _CHART.get(str(move_type or "").capitalize(), {})
    mult = 1.0
    for t in defender_types or []:
        mult *= row.get(str(t).capitalize(), 1)
    return mult


class DataStore:
    """The moves and pokedex tables, keyed by Showdown id."""

    def __init__(self, data_dir=None):
        self.dir = Path(data_dir) if data_dir else DATA_DIR
        self.moves = {}
        self.dex = {}
        self._lock = threading.Lock()

    @property
    def ready(self):
        return bool(self.moves) and bool(self.dex)

    def load(self):
        """Read the cached tables from disk. Returns self."""
        for name, attr in (("moves", "moves"), ("pokedex", "dex")):
            try:
                data = json.loads((self.dir / (name + ".json")).read_text(encoding=ENCODING))
                if isinstance(data, dict):
                    setattr(self, attr, data)
            except (OSError, ValueError):
                pass
        return self

    def fetch(self, getter=None):
        """Download both tables and cache them. `getter(url) -> text` is injectable for
        tests; the default uses `requests` (already a Jarvis dependency).
        Returns {"ok": bool, "error": str|None}."""
        def _default(url):
            import requests  # lazy: only the worker ever needs it
            resp = requests.get(url, timeout=FETCH_TIMEOUT)
            resp.raise_for_status()
            return resp.text

        getter = getter or _default
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            for name, url in DATA_URLS.items():
                text = getter(url)
                data = json.loads(text)
                if not isinstance(data, dict) or not data:
                    return {"ok": False, "error": "%s.json was empty or not an object" % name}
                (self.dir / (name + ".json")).write_text(text, encoding=ENCODING)
        except Exception as e:  # noqa: BLE001 - a download problem is a result, not a crash
            return {"ok": False, "error": "could not fetch game data: %s" % e}
        self.load()
        return {"ok": self.ready, "error": None if self.ready else "tables loaded but empty"}

    def ensure(self, getter=None):
        """Load from disk; fetch only when something is missing."""
        with self._lock:
            self.load()
            if self.ready:
                return {"ok": True, "error": None}
            return self.fetch(getter)

    # -- lookups (all return None / [] when unknown) -------------------------------
    def move(self, name):
        entry = self.moves.get(to_id(name))
        return entry if isinstance(entry, dict) else None

    def species(self, name):
        entry = self.dex.get(to_id(name))
        if entry is None and "-" in str(name or ""):
            entry = self.dex.get(to_id(str(name).split("-")[0]))
        return entry if isinstance(entry, dict) else None

    def types_of(self, species):
        entry = self.species(species)
        return list(entry.get("types") or []) if entry else []

    def base_stats(self, species):
        entry = self.species(species)
        base = entry.get("baseStats") if entry else None
        return dict(base) if isinstance(base, dict) else {}
