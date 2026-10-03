import pytest
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
                 discover_min_minutes=10, discover_max_topics=10, discover_min_name_score=0.85, discover_channels=("A", "B"), discover_source="api",
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
    assert [x["topic"] for x in d.run(full=True)] == ["Unbekannte Doku"]  # a full scan retries no_match, not covered topics

    d._lock.acquire()
    with pytest.raises(RuntimeError):
        d.run()  # only one run at a time
    d._lock.release()


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


def test_discovery_reads_filmliste_and_falls_back(show, monkeypatch):
    from rulesets_service import discovery as disc_mod
    from rulesets_service import runner as runner_mod
    from rulesets_service.config import Settings
    from rulesets_service.db import Database

    s = Settings(tvdb_api_key="k", discover_min_items=3, discover_min_minutes=10)
    r = runner_mod.Runner(s, Database(":memory:"))
    monkeypatch.setattr(r.shows, "search", lambda name: [])
    serie = [Item(channel="WDR", topic="Alte Serie", title=f"Folge {i}", duration=1800, url_video=f"a{i}") for i in range(4)]
    monkeypatch.setattr(disc_mod, "filmliste_items", lambda url: iter(serie))
    api_called = []
    monkeypatch.setattr(disc_mod, "mediathekview_pages", lambda *a, **kw: api_called.append(1) or iter([]))
    d = disc_mod.Discovery(s, r.db, r)
    assert [x["topic"] for x in d.run(dry_run=True)] == ["Alte Serie"] and not api_called

    def broken(url):
        raise OSError("Download kaputt")
    monkeypatch.setattr(disc_mod, "filmliste_items", broken)
    assert d.run(dry_run=True) == [] and api_called  # falls back to the search API


def test_filmliste_download_is_streamed_and_decoded(monkeypatch):
    import lzma

    import httpx
    from rulesets_service import sources
    rows = ['"X":["ZDF","Die Kanzlei","Folge %d – Recht","01.01.2026","20:15:00","00:45:00","700","Ärger","https://z/%d.mp4",'
            '"https://web","","","","","","","1767300000","","DE","false"]' % (i, i) for i in range(3000)]
    text = '{"Filmliste":["a","b"],"Filmliste":["Sender","Thema","Titel","Datum","Zeit","Dauer","Größe [MB]","Beschreibung",' \
           '"Url","Website","Url Untertitel","Url RTMP","Url Klein","Url RTMP Klein","Url HD","Url RTMP HD","DatumL","Url History",' \
           '"Geo","neu"],' + ",".join(rows) + "}"
    blob = lzma.compress(text.encode("utf-8"))
    monkeypatch.setattr(sources, "_client", lambda: httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=blob))))
    monkeypatch.setattr(httpx.Response, "iter_bytes", lambda self, n=None: (blob[i:i + 7] for i in range(0, len(blob), 7)))
    items = list(sources.filmliste_items("https://liste/Filmliste-akt.xz"))
    assert len(items) == 3000
    assert items[2999].title == "Folge 2999 - Recht" and items[0].description == "Ärger" and items[0].duration == 2700


def test_recheck_regenerates_generated_and_reports_imported(show, monkeypatch):
    from rulesets_service import runner as runner_mod
    from rulesets_service.config import Settings
    from rulesets_service.db import Database

    db = Database(":memory:")
    r = runner_mod.Runner(Settings(min_match_rate=0.8), db)
    monkeypatch.setattr(r.shows, "get_show", lambda tid: show)
    current = {"fmt": "{name} (S{s:02d}/E{e:02d})"}
    monkeypatch.setattr(runner_mod, "fetch_items_for_show", lambda s, topics: make_items(s, current["fmt"]))

    assert r.run_one(4711)["status"] == "ok"
    old = db.list_rulesets(tvdb_id=4711)[0]
    assert r.recheck_all() == []  # still fits

    current["fmt"] = "Folge {e}: {name} ({d})"  # the channel changed its title format
    res = r.recheck_all()
    assert [x["status"] for x in res] == ["regenerated"]
    new = db.list_rulesets(tvdb_id=4711)
    assert len(new) == 1 and new[0]["source"] == "generated" and new[0]["id"] != old["id"]

    # imported rulesets are never overwritten, only reported
    db._exec("UPDATE rulesets SET source = 'upstream'")
    current["fmt"] = "{d}"
    res = r.recheck_all()
    assert [x["status"] for x in res] == ["stale"]
    assert db.list_rulesets(tvdb_id=4711)[0]["source"] == "upstream"


def test_full_scan_resumes_after_stop(show, monkeypatch):
    from rulesets_service import discovery as disc_mod
    from rulesets_service import runner as runner_mod
    from rulesets_service.config import Settings
    from rulesets_service.db import Database

    db = Database(":memory:")
    s = Settings(tvdb_api_key="k", discover_min_items=3, discover_min_minutes=10)
    r = runner_mod.Runner(s, db)
    monkeypatch.setattr(r.shows, "search", lambda name: [])
    entries = [Item(channel="WDR", topic=t, title=f"Folge {i}", duration=1800, url_video=f"{t}{i}")
               for t, n in (("Doku A", 6), ("Doku B", 5), ("Doku C", 4)) for i in range(n)]
    monkeypatch.setattr(disc_mod, "filmliste_items", lambda url: iter(entries))
    d = disc_mod.Discovery(s, db, r)

    original = d._one
    def stop_after_first(topic, count, dry_run):
        d._stop.set()
        return original(topic, count, dry_run)
    monkeypatch.setattr(d, "_one", stop_after_first)
    assert [x["topic"] for x in d.run(full=True)] == ["Doku A"]
    assert d.full_scan_pending

    monkeypatch.setattr(d, "_one", original)
    assert [x["topic"] for x in d.run(full=True)] == ["Doku B", "Doku C"]  # resumed, Doku A not again
    assert d.full_scan_pending is None
    assert [x["topic"] for x in d.run(full=True)] == ["Doku A", "Doku B", "Doku C"]  # a new full scan starts over
