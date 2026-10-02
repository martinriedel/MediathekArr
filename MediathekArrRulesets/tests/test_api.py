import json

import pytest
from fastapi.testclient import TestClient

from rulesets_service import main

KEY = {"X-Api-Key": "secret"}

UPSTREAM_ENTRY = {
    "id": 17, "mediaId": 3, "topic": "Tatort|Polizeiruf", "priority": 1,
    "filters": '[{"attribute":"duration","type":"GreaterThan","value":"60"}]',
    "titleRegexRules": '[{"type":"regex","field":"title","pattern":"^(.+?)$"}]',
    "episodeRegex": None, "seasonRegex": None, "matchingStrategy": "ItemTitleExact",
    "media": {"media_id": 3, "media_name": "Tatort", "media_type": "show", "media_tmdbId": None, "media_imdbId": None, "media_tvdbId": 83214},
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = main.Database(str(tmp_path / "t.sqlite"))
    monkeypatch.setattr(main, "db", db)
    return TestClient(main.app)


def test_public_format_matches_mediathekarr(client):
    main.db.upsert_upstream([UPSTREAM_ENTRY])
    data = client.get("/metadata/api/rulesets.php?page=1").json()
    assert data == client.get("/api/v1/rulesets?page=1").json()
    rs = data["rulesets"][0]
    # MediathekArr expects these as JSON strings and ints
    assert isinstance(rs["filters"], str) and isinstance(rs["titleRegexRules"], str)
    assert json.loads(rs["filters"])[0]["type"] == "GreaterThan"
    assert rs["topic"] == "Tatort|Polizeiruf"
    assert rs["media"]["media_tvdbId"] == 83214 and rs["media"]["media_tmdbId"] is None
    assert data["pagination"] == {"currentPage": 1, "totalPages": 1, "totalItems": 1, "itemsPerPage": 500}


def test_writes_need_key(client):
    assert client.post("/api/media", json={"name": "X"}).status_code == 401
    assert client.post("/api/media", json={"name": "X"}, headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.post("/api/media", json={"name": "X"}, headers={"Authorization": "Bearer secret"}).status_code == 200


def test_crud_and_validation(client):
    mid = client.post("/api/media", json={"name": "Serie", "tvdbId": 5}, headers=KEY).json()["id"]
    bad = {"mediaId": mid, "topic": "Serie", "matchingStrategy": "Quatsch"}
    assert client.post("/api/rulesets", json=bad, headers=KEY).status_code == 400
    body = {"mediaId": mid, "topic": "Serie", "matchingStrategy": "SeasonAndEpisodeNumber",
            "seasonRegex": r"S(\d+)", "episodeRegex": r"E(\d+)", "filters": [{"attribute": "duration", "type": "GreaterThan", "value": 10}]}
    rid = client.post("/api/rulesets", json=body, headers=KEY).json()["id"]
    pub = client.get("/api/v1/rulesets").json()["rulesets"][0]
    assert json.loads(pub["filters"])[0]["value"] == 10 and pub["titleRegexRules"] == "[]"
    assert client.get("/api/rulesets?tvdbId=5").json()[0]["id"] == rid
    assert client.delete(f"/api/rulesets/{rid}", headers=KEY).status_code == 200
    assert client.get("/api/rulesets?tvdbId=5").json() == []


def test_upstream_reimport_keeps_local_edits(client):
    main.db.upsert_upstream([UPSTREAM_ENTRY])
    rid = main.db.list_rulesets()[0]["id"]
    edited = {**UPSTREAM_ENTRY, "mediaId": main.db.list_rulesets()[0]["mediaId"], "topic": "Tatort"}
    client.put(f"/api/rulesets/{rid}", json={k: edited[k] for k in ("mediaId", "topic", "priority", "filters", "titleRegexRules", "matchingStrategy")}, headers=KEY)
    stats = main.db.upsert_upstream([UPSTREAM_ENTRY])
    assert stats == {"created": 0, "updated": 0, "kept_local": 1}
    assert main.db.get_ruleset(rid)["topic"] == "Tatort"


def test_generated_upload_replaces_only_generated(client):
    main.db.upsert_upstream([UPSTREAM_ENTRY])
    body = {"name": "Tatort", "matchRate": 0.9, "rulesets": [{"topic": "Tatort", "matchingStrategy": "ItemTitleExact",
                                                              "titleRegexRules": [{"type": "regex", "field": "title", "pattern": "(.+)"}]}]}
    assert client.put("/api/generated/83214", json=body, headers=KEY).status_code == 200
    assert client.put("/api/generated/83214", json=body, headers=KEY).status_code == 200
    sources = sorted(r["source"] for r in client.get("/api/rulesets?tvdbId=83214").json())
    assert sources == ["generated", "upstream"]
    assert client.get("/api/generation-log").json()[0]["status"] == "ok"


def test_export_import_roundtrip(client):
    main.db.upsert_upstream([UPSTREAM_ENTRY])
    dump = client.get("/api/export").json()
    main.db.delete_media(main.db.list_media()[0]["id"])
    assert client.post("/api/import", json=dump, headers=KEY).json() == {"media": 1, "rulesets": 1}
    assert client.get("/health").json()["rulesets"] == 1
