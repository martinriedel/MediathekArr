from conftest import make_items

from rulesets_service.generator import candidate_topics, generate
from rulesets_service.matcher import Item


class FakeLLM:
    enabled = True

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def chat_json(self, messages):
        self.calls.append(list(messages))
        return self.answers.pop(0)


def test_heuristic_finds_season_episode(show):
    res = generate(show, make_items(show, "{name} (S{s:02d}/E{e:02d})"), llm=None)
    assert res.ok and not res.used_llm
    rs = res.accepted[0].ruleset
    assert rs.matching_strategy == "SeasonAndEpisodeNumber"
    assert res.accepted[0].matched == len(show.episodes)


def test_heuristic_finds_title_match(show):
    res = generate(show, make_items(show, "Testserie - {name}"), llm=None)
    assert res.ok
    assert res.accepted[0].ruleset.matching_strategy in ("ItemTitleExact", "ItemTitleIncludes")


def test_generic_topic_gets_name_filter(show):
    items = make_items(show, "Testserie: {name}", topic="Krimi am Samstag")
    items += [Item(topic="Krimi am Samstag", title=f"Andere Serie: Fall {i}", url_video=f"o{i}") for i in range(20)]
    assert candidate_topics(items, show) == [("Krimi am Samstag", "Testserie")]
    res = generate(show, items, llm=None)
    assert res.ok
    assert res.accepted[0].ruleset.filters == [{"attribute": "title", "type": "Contains", "value": "Testserie"}]


def test_wrong_numbering_is_rejected_without_llm(show):
    # broadcaster counts S/E differently than TVDB: numbers point at the wrong episodes
    items = make_items(show, "{name} (S{s:02d}/E{e:02d})")
    for it in items:
        it.title = it.title.replace("/E0", "/E1")  # E01 -> E11 etc. -> no TVDB episode
    res = generate(show, [i for i in items], llm=None)
    # title-based heuristics still rescue it
    assert res.ok and res.accepted[0].ruleset.matching_strategy != "SeasonAndEpisodeNumber"


def test_llm_used_with_feedback(show):
    items = make_items(show, "Teil {e} der {s}. Runde: {name}?")
    items = [i for i in items if "Trailer" not in i.title]
    for i in items:  # make titles useless for the built-in patterns
        i.title = i.title.replace(i.title.split(": ")[1], "xyz")
    bad = {"rulesets": [{"topic": "Testserie", "matchingStrategy": "ByAbsoluteEpisodeNumber", "episodeRegex": r"Folge (\d+)"}]}
    good = {"rulesets": [{"topic": "Testserie", "matchingStrategy": "SeasonAndEpisodeNumber",
                          "seasonRegex": r"der (\d+)\. Runde", "episodeRegex": r"Teil (\d+)"}]}
    llm = FakeLLM([bad, good])
    res = generate(show, items, llm=llm)
    assert res.ok and res.used_llm
    assert len(llm.calls) == 2
    assert "Ergebnis der Prüfung" in llm.calls[1][-1]["content"]
    assert res.accepted[0].ruleset.season_regex == r"der (\d+)\. Runde"


def test_no_items():
    from rulesets_service.matcher import Show
    res = generate(Show(1, "X", "X", []), [], llm=None)
    assert not res.ok and "Keine Einträge" in res.message


def test_runner_stores_locally_and_skips_known(show, monkeypatch):
    from rulesets_service import runner as runner_mod
    from rulesets_service.config import Settings
    from rulesets_service.db import Database

    db = Database(":memory:")
    r = runner_mod.Runner(Settings(min_match_rate=0.8), db)
    monkeypatch.setattr(r.shows, "get_show", lambda tid: show)
    monkeypatch.setattr(runner_mod, "fetch_items_for_show", lambda s, topics: make_items(s, "{name} (S{s:02d}/E{e:02d})"))

    first = r.run_one(4711)
    assert first["status"] == "ok"
    assert [x["source"] for x in db.list_rulesets(tvdb_id=4711)] == ["generated"]
    assert r.run_one(4711)["status"] == "skipped"
    assert r.run_one(4711, force=True)["status"] == "ok"
    assert len(db.list_rulesets(tvdb_id=4711)) == 1  # replaced, not duplicated


def test_discovery_finds_topic_and_generates(show, monkeypatch, tmp_path):
    from rulesets_service import discovery as disc_mod
    from rulesets_service import runner as runner_mod
    from rulesets_service.config import Settings
    from rulesets_service.db import Database

    db = Database(":memory:")
    s = Settings(min_match_rate=0.8, tvdb_api_key="k", discover_items=100, discover_min_items=3,
                 discover_min_minutes=10, discover_max_topics=10, discover_min_name_score=0.85, discover_channels=("A", "B"),
                 retry_failed_after_hours=72)
    r = runner_mod.Runner(s, db)
    items = make_items(show, "{name} (S{s:02d}/E{e:02d})")
    noise = [Item(topic="Nachrichten", title=f"Kurz {i}", duration=120, url_video=f"n{i}") for i in range(10)]
    unknown = [Item(topic="Unbekannte Doku", title=f"Teil {i}", duration=1800, url_video=f"u{i}") for i in range(5)]
    all_items = items + noise + unknown
    monkeypatch.setattr(disc_mod, "mediathekview_pages", lambda q, n, **kw: iter([all_items[:7], all_items[7:]]))  # both channels return the same entries: counted once
    monkeypatch.setattr(r.shows, "search", lambda name: [{"tvdb_id": 4711, "name": "Testserie", "translations": {}, "aliases": []}]
                        if name == "Testserie" else [{"tvdb_id": 1, "name": "Etwas ganz anderes", "translations": {}, "aliases": []}])
    monkeypatch.setattr(r.shows, "get_show", lambda tid: show)
    monkeypatch.setattr(runner_mod, "fetch_items_for_show", lambda s_, topics: items)

    d = disc_mod.Discovery(s, db, r)
    res = {x["topic"]: x["status"] for x in d.run()}
    assert res == {"Testserie": "ok", "Unbekannte Doku": "no_match"}  # short news clips are not a series
    assert db.list_rulesets(tvdb_id=4711)
    assert d.run() == []  # covered topic and recent no_match are not retried


def test_mediathekview_pages_walks_whole_catalogue(monkeypatch):
    import json

    import httpx
    from rulesets_service import sources
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        n = body["size"] if body["offset"] < 2000 else 3
        return httpx.Response(200, json={"result": {"results": [{"topic": "T", "title": str(i), "duration": 900} for i in range(n)]}})
    monkeypatch.setattr(sources, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    pages = list(sources.mediathekview_pages([], 0, duration_min=600))
    assert [len(p) for p in pages] == [1000, 1000, 3]
    assert [b["offset"] for b in bodies] == [0, 1000, 2000] and bodies[0]["duration_min"] == 600
    assert sum(len(p) for p in sources.mediathekview_pages([], 1500)) == 1500


def test_best_tvdb_match_uses_translations():
    from rulesets_service.discovery import best_tvdb_match
    hit, score = best_tvdb_match("Die Heiland - Wir sind Anwalt", [
        {"tvdb_id": 1, "name": "Heiland: We Are Lawyers", "translations": {"deu": "Die Heiland – Wir sind Anwalt"}},
        {"tvdb_id": 2, "name": "Anwalt", "translations": {}},
    ])
    assert hit["tvdb_id"] == 1 and score > 0.9
