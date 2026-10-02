from datetime import date

from conftest import make_items, ts

from rulesets_service.matcher import Episode, Item, Ruleset, Show, apply_rulesets, filter_matches, format_title, match_item


def test_season_episode_regex(show):
    rs = Ruleset(topic="Testserie", matching_strategy="SeasonAndEpisodeNumber",
                 season_regex=r"S(\d+)/E\d+", episode_regex=r"S\d+/E(\d+)")
    m = match_item(Item(topic="Testserie", title="Die Rückkehr (S01/E03)"), rs, show)
    assert (m.episode.season, m.episode.number) == (1, 3)
    assert m.matched_title == "S01E03"


def test_static_season(show):
    rs = Ruleset(topic="Testserie", matching_strategy="SeasonAndEpisodeNumber", season_regex="S2", episode_regex=r"Folge (\d+)")
    m = match_item(Item(title="Folge 3"), rs, show)
    assert (m.episode.season, m.episode.number) == (2, 3)


def test_absolute_uses_first_episode_with_number(show):
    # mirrors FindEpisodeByAbsoluteEpisodeNumber: compares the per-season number
    rs = Ruleset(topic="Testserie", matching_strategy="ByAbsoluteEpisodeNumber", episode_regex=r"Folge (\d+)")
    m = match_item(Item(title="Folge 2"), rs, show)
    assert (m.episode.season, m.episode.number) == (1, 2)
    assert match_item(Item(title="Folge 0"), rs, show) is None


def test_title_exact_and_dotnet_named_group(show):
    rs = Ruleset(topic="Testserie", matching_strategy="ItemTitleExact",
                 title_regex_rules=[{"type": "regex", "field": "title", "pattern": r"^(?<t>.+?) \(.*\)$"}])
    m = match_item(Item(title="Mord im Biergarten (S01/E02)"), rs, show)
    assert m.episode.name == "Mord im Biergarten"


def test_title_exact_picks_episode_on_air_date():
    show = Show(1, "X", "X", [Episode("Pilot", date(2020, 1, 1), 45, 1, 1), Episode("Pilot", date(2021, 5, 5), 45, 2, 1)])
    rs = Ruleset(topic="X", matching_strategy="ItemTitleExact", title_regex_rules=[{"type": "regex", "field": "title", "pattern": "(.+)"}])
    assert match_item(Item(title="Pilot", timestamp=ts("2020-01-01")), rs, show).episode.season == 1
    assert match_item(Item(title="Pilot", timestamp=ts("2019-01-01")), rs, show).episode.season == 2  # newest


def test_airdate_german_month(show):
    rs = Ruleset(topic="Testserie", matching_strategy="ItemTitleEqualsAirdate",
                 title_regex_rules=[{"type": "regex", "field": "title", "pattern": r"vom (\d{1,2}\. \p{L}+ \d{4})"}])
    m = match_item(Item(title="Sendung vom 8. Januar 2024"), rs, show)
    assert m.episode.name == "Mord im Biergarten"


def test_filters():
    item = Item(title="Hallo Welt", duration=50 * 60)
    assert filter_matches(item, {"attribute": "duration", "type": "GreaterThan", "value": 20})
    assert not filter_matches(item, {"attribute": "duration", "type": "LowerThan", "value": 20})
    assert filter_matches(item, {"attribute": "title", "type": "Contains", "value": "welt"})
    assert filter_matches(item, {"attribute": "title", "type": "Regex", "value": "^Hallo"})


def test_format_title():
    assert format_title("Tom & Jerry: Café (1)") == "Tom.and.Jerry.Cafe.1"  # accents go, like RemoveAccentButKeepGermanUmlauts


def test_apply_skips_trailer_and_reports_unmatched(show):
    items = make_items(show, "{name} (S{s:02d}/E{e:02d})")
    items.append(Item(topic="Testserie", title="Making-of", url_video="m"))
    rs = Ruleset(topic="Testserie", matching_strategy="SeasonAndEpisodeNumber",
                 season_regex=r"S(\d+)/E\d+", episode_regex=r"S\d+/E(\d+)")
    matches, unmatched = apply_rulesets(items, [rs], show)
    assert len(matches) == len(show.episodes)
    assert [i.title for i in unmatched] == ["Making-of"]
