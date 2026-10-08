"""game_registry.py

Dynamic game registry with compiled regex patterns for score parsing.

The module-level ``game_registry`` singleton is initialised at import time
with default games and must be populated from the GameRegistry sheet by
calling ``game_registry.rebuild(store)`` after the SheetStore is created.
"""

import re
import threading
from typing import Any, Dict, List, Tuple


# ---------------------------------------------------------------------------
# Default game list (seeded into GameRegistry sheet on first run)
# ---------------------------------------------------------------------------
_DEFAULT_GAMES: List[Tuple[str, str]] = [
    ("Tango", "time"),
    ("Zip", "time"),
    ("Mini Sudoku", "time"),
    ("Queens", "time"),
    ("Crossclimb", "time"),
    ("Pinpoint", "guesses"),
]

# Games that the registry can offer through /jw-update without making their
# native share formats active by default. Keeping these separate preserves the
# existing group's required games while documenting the formats we understand.
_OPTIONAL_GAMES: List[Tuple[str, str]] = [
    ("Wordle", "guesses"),
    ("4x6", "points"),
    ("4x3", "points"),
    ("MapTap", "points"),
]

# Metadata is intentionally independent from registration. Native parsers
# still check the active GameRegistry rows before accepting any of these.
GAME_METADATA: Dict[str, Dict[str, Any]] = {
    "Wordle": {
        "metric_type": "guesses",
        "aliases": (),
        "urls": ("nytimes.com/games/wordle",),
    },
    "4x6": {
        "metric_type": "points",
        "aliases": ("4×6",),
        "urls": ("hankgreen.com/4x6",),
    },
    "4x3": {
        "metric_type": "points",
        "aliases": ("4×3",),
        "urls": ("4x3.fun", "hankgreen.com/fourbythree"),
    },
    "MapTap": {
        "metric_type": "points",
        "aliases": (),
        "urls": ("maptap.gg",),
    },
}


# ---------------------------------------------------------------------------
# GameRegistryCache
# ---------------------------------------------------------------------------
class GameRegistryCache:
    """In-memory cache of registered games, loaded from the GameRegistry sheet.

    Rebuilds compiled regexes whenever the registry changes.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.games: List[Tuple[str, str]] = list(_DEFAULT_GAMES)
        self.effective_dates: Dict[str, str] = {}
        self.time_header_re: re.Pattern = re.compile(r"(?!)")  # never matches
        self.guesses_header_re: re.Pattern = re.compile(r"(?!)")
        self.points_header_re: re.Pattern = re.compile(r"(?!)")
        self._rebuild_regexes()

    # -- public API ----------------------------------------------------------

    def games_as_tuples(self) -> List[Tuple[str, str]]:
        return list(self.games)

    def game_names(self) -> List[str]:
        return [g for g, _ in self.games]

    def metric_type_for(self, name: str) -> str:
        """Return the registered metric type for a game name or known alias."""
        key = _game_key(name)
        for game, metric_type in self.games:
            if key in {_game_key(game), *(_game_key(a) for a in _aliases_for(game))}:
                return metric_type
        return ""

    def is_registered(self, name: str, metric_type: str = "") -> bool:
        """Whether *name* (or a documented alias) is in the active registry."""
        registered_metric = self.metric_type_for(name)
        return bool(registered_metric) and (
            not metric_type or registered_metric == metric_type.strip().lower()
        )

    def canonical_name(self, name: str) -> str:
        """Resolve a registered game name or alias to its canonical spelling."""
        key = _game_key(name)
        for game, _ in self.games:
            if key in {_game_key(game), *(_game_key(a) for a in _aliases_for(game))}:
                return game
        return name.strip()

    def rebuild(self, store: Any) -> None:
        """Reload game list from the GameRegistry sheet and recompile regexes."""
        rows = store.load_game_registry()
        with self._lock:
            self.games = [(name, mt) for name, mt, _ in rows]
            self.effective_dates = {name: ed for name, mt, ed in rows}
            self._rebuild_regexes()

    def games_for_day(self, day: str) -> List[Tuple[str, str]]:
        """Return only games whose effective_date <= *day*."""
        return [
            (g, mt) for g, mt in self.games
            if self.effective_dates.get(g, "2020-01-01") <= day
        ]

    # -- internal ------------------------------------------------------------

    def _rebuild_regexes(self) -> None:
        time_names = [g for g, mt in self.games if mt == "time"]
        if time_names:
            alt = "|".join(re.escape(g) for g in time_names)
            self.time_header_re = re.compile(
                rf"\b({alt})\b\s*#(\d+)(?:\s*(?:\|\s*)?(\d+):(\d+))?",
                re.IGNORECASE,
            )
        else:
            self.time_header_re = re.compile(r"(?!)")

        guesses_names = [g for g, mt in self.games if mt == "guesses"]
        if guesses_names:
            alt = "|".join(re.escape(g) for g in guesses_names)
            self.guesses_header_re = re.compile(
                rf"\b({alt})\b\s*#(\d+)(?:\s*\|\s*(\d+)(?:\s+guess(?:es)?\b)?)?",
                re.IGNORECASE,
            )
        else:
            self.guesses_header_re = re.compile(r"(?!)")

        point_names = [g for g, mt in self.games if mt == "points"]
        if point_names:
            alternatives = []
            for name in point_names:
                alternatives.append(name)
                alternatives.extend(_aliases_for(name))
            alternatives = sorted(set(alternatives), key=len, reverse=True)
            alt = "|".join(re.escape(name) for name in alternatives)
            self.points_header_re = re.compile(
                rf"\b(?P<game>{alt})\b\s*#(?P<puzzle_id>\d+)"
                rf"\s*\|\s*(?P<score>\d+)\s+points?\b",
                re.IGNORECASE,
            )
        else:
            self.points_header_re = re.compile(r"(?!)")


def _game_key(name: str) -> str:
    return " ".join((name or "").strip().lower().replace("×", "x").split())


def _aliases_for(name: str) -> Tuple[str, ...]:
    for canonical, metadata in GAME_METADATA.items():
        if _game_key(canonical) == _game_key(name):
            return tuple(metadata.get("aliases") or ())
    return ()


def canonical_game_name(name: str) -> str:
    """Canonical spelling for registry entry names, including known aliases."""
    clean = (name or "").strip()
    key = _game_key(clean)
    for canonical, metadata in GAME_METADATA.items():
        if key == _game_key(canonical) or key in {
            _game_key(alias) for alias in metadata.get("aliases", ())
        }:
            return canonical
    return clean


# Module-level singleton; initialised at startup after SheetStore is created.
game_registry = GameRegistryCache()
