"""Shared normalization for score-row identities."""

from datetime import date
from typing import Any, Dict, Iterable, List, Optional, Tuple

from day_utils import _parse_day_key_loose
from parser import canonical_user_id, normalize_game


ScoreIdentity = Tuple[str, str, str, str]


def score_identity(day: object, user_id: object, game: object, puzzle_id: object) -> Optional[ScoreIdentity]:
    """Return a normalized (day, user, game, puzzle) identity when complete."""
    raw_day = str(day or "").strip()
    parsed_day = _parse_day_key_loose(raw_day)
    normalized_day = parsed_day.isoformat() if isinstance(parsed_day, date) else raw_day
    normalized_user = canonical_user_id(str(user_id or "").strip())
    normalized_game = normalize_game(str(game or "").strip())

    raw_puzzle_id = "" if puzzle_id is None else str(puzzle_id).strip()
    try:
        normalized_puzzle_id = str(int(raw_puzzle_id))
    except (TypeError, ValueError):
        normalized_puzzle_id = raw_puzzle_id

    if not (normalized_day and normalized_user and normalized_game and normalized_puzzle_id):
        return None
    return (normalized_day, normalized_user, normalized_game, normalized_puzzle_id)


def deduplicate_score_records(
    records: Iterable[Dict[str, Any]], day: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Keep the last complete score identity, preserving incomplete fixtures.

    The identity is (day, canonical user, canonical game, puzzle id). Rows
    missing any identity field remain untouched because they cannot safely be
    merged.
    """
    items = list(records)
    keys: List[Optional[ScoreIdentity]] = []
    last_index_by_key: Dict[ScoreIdentity, int] = {}
    for index, record in enumerate(items):
        record_day = record.get("day")
        if not str(record_day or "").strip():
            record_day = day
        key = score_identity(record_day, record.get("user_id"), record.get("game"), record.get("puzzle_id"))
        keys.append(key)
        if key is not None:
            last_index_by_key[key] = index

    return [
        record for index, record in enumerate(items)
        if keys[index] is None or last_index_by_key[keys[index]] == index
    ]
