from datetime import date

import pytest

from game_registry import (
    _DEFAULT_GAMES,
    _OPTIONAL_GAMES,
    GAME_METADATA,
    GameRegistryCache,
    canonical_game_name,
    game_registry,
)
from parser import ParsedScore, parse_score, parse_score_for_day
from score_metrics import higher_is_better, metric_sort_value, metric_unit, record_is_dnf


class RegistrySetup:
    """Temporarily install a game list into the parser's shared registry."""

    def __init__(self, games):
        self.games = games
        self.original = None

    def __enter__(self):
        self.original = list(game_registry.games)
        game_registry.games = list(self.games)
        game_registry._rebuild_regexes()
        return game_registry

    def __exit__(self, *_exc):
        game_registry.games = self.original
        game_registry._rebuild_regexes()


def _registered(*names):
    return [*_DEFAULT_GAMES, *(item for item in _OPTIONAL_GAMES if item[0] in names)]


def _six_rows():
    return "\n".join(["🟩🟨🟩🟩🟨🟩"] * 6)


def _four_rows():
    return "\n".join(["🟩🟨⭐"] * 4)


def test_optional_registry_metadata_does_not_change_default_games():
    assert _DEFAULT_GAMES == [
        ("Tango", "time"),
        ("Zip", "time"),
        ("Mini Sudoku", "time"),
        ("Queens", "time"),
        ("Crossclimb", "time"),
        ("Pinpoint", "guesses"),
    ]
    assert _OPTIONAL_GAMES == [
        ("Wordle", "guesses"),
        ("4x6", "points"),
        ("4x3", "points"),
        ("MapTap", "points"),
    ]
    assert canonical_game_name("4×6") == "4x6"
    assert GAME_METADATA["4x3"]["urls"]
    registry = GameRegistryCache()
    assert registry.game_names() == [game for game, _metric in _DEFAULT_GAMES]


def test_native_wordle_requires_registration_and_supports_commas_and_hard_mode():
    share = "Wordle #1,835 - 4/6*"
    assert parse_score(share) is None
    with RegistrySetup(_registered("Wordle")):
        parsed = parse_score(share)
    assert parsed == ParsedScore(
        game="Wordle",
        puzzle_id=1835,
        metric_type="guesses",
        metric_value=4,
        display="4/6",
    )
    assert parsed.status == "solved"


def test_native_wordle_failure_is_failed_and_uses_legacy_compatible_value():
    with RegistrySetup(_registered("Wordle")):
        parsed = parse_score("Wordle 1,836 X/6")
    assert parsed is not None
    assert (parsed.metric_value, parsed.status, parsed.display) == (106, "failed", "DNF(6)")


@pytest.mark.parametrize(
    "header",
    [
        "Wordle 1,835 7/6",
        "Wordle #0 0/6",
        "Wordle 1,835 3/7",
        "Wordle x 4/6",
        "Wordle 1,835 othermode 4/6",
    ],
)
def test_malformed_wordle_headers_are_rejected(header):
    with RegistrySetup(_registered("Wordle")):
        assert parse_score(header) is None


def test_native_four_by_six_parses_points_date_and_rescue_status():
    share = f"""4×6 · Thu Oct 8
{_six_rows()}
12/14 moves 🏅 · 91 pts · 🔥3
https://hankgreen.com/4x6"""
    with RegistrySetup(_registered("4x6")):
        parsed = parse_score_for_day(share, "2026-10-09")
        rescued = parse_score_for_day(share.replace("🏅", ":life_buoy:"), date(2026, 10, 9))
        unicode_rescue = parse_score_for_day(share.replace("🏅", "🛟"), "2026-10-09")
    assert parsed is not None
    assert (parsed.metric_type, parsed.metric_value, parsed.status) == ("points", 91, "solved")
    assert parsed.score_day == "2026-10-08"
    assert parsed.puzzle_id == date(2026, 10, 8).toordinal()
    assert rescued is not None and rescued.status == "failed"
    assert rescued.metric_value == 91
    assert unicode_rescue is not None and unicode_rescue.status == "failed"


def test_four_by_six_yearless_date_selects_nearest_year_across_new_year():
    share = f"""4x6 · Thu Jan 1
{_six_rows()}
12/14 moves · 88 pts
https://hankgreen.com/4x6"""
    with RegistrySetup(_registered("4x6")):
        parsed = parse_score(share, date(2026, 12, 31))
    assert parsed is not None
    assert parsed.score_day == "2027-01-01"


def test_native_four_by_three_wrong_hub_result_is_failed():
    share = f"""4x3 for October 8, 2026
{_four_rows()}
0 points • Called the Wrong Hub
https://4x3.fun"""
    assert parse_score(share) is None
    with RegistrySetup(_registered("4x3")):
        parsed = parse_score(share)
    assert parsed is not None
    assert (parsed.metric_value, parsed.status) == (0, "failed")
    assert parsed.score_day == "2026-10-08"
    assert parsed.puzzle_id == date(2026, 10, 8).toordinal()


def test_four_by_three_zero_point_rule_breaker_is_completed():
    share = f"""4×3 for October 8, 2026
{_four_rows()}
0 points • RULE BREAKER 💀
https://4x3.fun"""
    with RegistrySetup(_registered("4x3")):
        parsed = parse_score(share)
    assert parsed is not None
    assert (parsed.metric_value, parsed.status) == (0, "solved")


def test_four_by_three_rule_breaker_keeps_a_negative_published_score():
    share = f"""4x3 for October 8, 2026
{_four_rows()}
-100 points • RULE BREAKER 💀
https://4x3.fun"""
    with RegistrySetup(_registered("4x3")):
        parsed = parse_score(share)
    assert parsed is not None
    assert (parsed.metric_value, parsed.status) == (-100, "solved")


def test_four_by_three_accepts_iso_date_text():
    share = f"""4×3 · 2026-10-08
{_four_rows()}
124 points • No mistakes
https://4x3.fun"""
    with RegistrySetup(_registered("4x3")):
        parsed = parse_score(share)
    assert parsed is not None
    assert parsed.score_day == "2026-10-08"


def test_four_by_three_out_of_guesses_is_failed_even_without_a_points_line():
    share = "4×3 for October 8, 2026\nOut of guesses\nhttps://4x3.fun"
    with RegistrySetup(_registered("4x3")):
        parsed = parse_score(share)
    assert parsed is not None
    assert (parsed.metric_value, parsed.status) == (0, "failed")


@pytest.mark.parametrize("rows", [_four_rows().splitlines()[:1], _four_rows()])
def test_four_by_three_failure_accepts_variable_official_row_counts(rows):
    share = "4x3 for October 8, 2026\n0 points • Called the Wrong Hub\n" + "\n".join(rows) + "\nhttps://4x3.fun"
    with RegistrySetup(_registered("4x3")):
        parsed = parse_score(share)
    assert parsed is not None and parsed.status == "failed"


@pytest.mark.parametrize("suffix", ["#p=custom", "#d=2026-10-08", "#b=beta"])
def test_four_by_three_rejects_archive_custom_and_beta_links(suffix):
    share = f"""4x3 for October 8, 2026
{_four_rows()}
100 points • No mistakes
https://4x3.fun/{suffix}"""
    with RegistrySetup(_registered("4x3")):
        assert parse_score(share) is None


def test_generic_points_header_supports_registered_alias_and_is_gated():
    message = "4×6 #123 | 98 points"
    assert parse_score(message) is None
    with RegistrySetup(_registered("4x6")):
        parsed = parse_score(message)
    assert parsed is not None
    assert (parsed.game, parsed.puzzle_id, parsed.metric_type, parsed.metric_value) == (
        "4x6", 123, "points", 98
    )


def test_maptap_native_flattened_share_uses_published_total_and_ordinal_day():
    # Slack can flatten the line break between October 8 and the first 100-point round.
    share = (
        "[www.maptap.gg](http://www.maptap.gg) October 8100"
        "[:dart:](https://example.invalid/dart) 89[:tada:] 100[:dart:] 90[:crown:] 93[:trophy:]"
        "Final score: 938."
    )
    with RegistrySetup(_registered("MapTap")):
        parsed = parse_score_for_day(share, "2026-10-08")
    assert parsed is not None
    assert (parsed.metric_type, parsed.metric_value, parsed.status) == ("points", 938, "solved")
    assert parsed.score_day == "2026-10-08"
    assert parsed.puzzle_id == date(2026, 10, 8).toordinal()


def test_maptap_rejects_practice_and_requires_date_for_native_result():
    with RegistrySetup(_registered("MapTap")):
        assert parse_score("MapTap.gg October 8 Final score: 900 practice") is None
        assert parse_score("MapTap.gg Final score: 900") is None
        generic = parse_score("MapTap #123 | 900 points")
    assert generic is not None and generic.puzzle_id == 123


def test_existing_time_and_pinpoint_parsing_remains_compatible():
    time_score = parse_score("Zip #316 | 0:13")
    pinpoint = parse_score("Pinpoint #42 | 5 guesses")
    assert time_score is not None and (time_score.metric_type, time_score.metric_value) == ("time", 13)
    assert pinpoint is not None and pinpoint.status == "failed"
    assert pinpoint.metric_value == 105


def test_metric_helpers_use_status_then_legacy_guess_penalty():
    assert higher_is_better("points")
    assert not higher_is_better("guesses")
    assert metric_sort_value(900, "points") == -900
    assert metric_sort_value(4, "guesses") == 4
    assert metric_unit("points") == "points"
    assert metric_unit("time") == "seconds"
    assert metric_unit("other") is None
    assert record_is_dnf({"status": "failed", "metric_type": "guesses", "metric_value": 4})
    assert not record_is_dnf({"status": "solved", "metric_type": "guesses", "metric_value": 106})
    assert record_is_dnf({"metric_type": "guesses", "metric_value": 105})
    assert not record_is_dnf({"metric_type": "points", "metric_value": 0})
