"""HTTP API. Public reads in the upstream format; writes need RULESETS_API_KEY."""
from __future__ import annotations

import hmac
import logging
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .config import Settings
from .db import Database
from .matcher import STRATEGIES
from .runner import Runner
from .sources import fetch_upstream_rulesets

log = logging.getLogger("rulesets")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

settings = Settings.from_env()
db = Database(settings.db_path)
runner = Runner(settings, db)


@asynccontextmanager
async def lifespan(_: FastAPI):
    _start_background_jobs()
    yield


app = FastAPI(title="MediathekArr Rulesets", version="1.0", lifespan=lifespan)
STATIC = Path(__file__).parent / "static"


def require_key(x_api_key: str | None = Header(default=None), authorization: str | None = Header(default=None)) -> None:
    if not settings.api_key:
        raise HTTPException(403, "Schreibzugriff ist aus: RULESETS_API_KEY ist nicht gesetzt")
    given = x_api_key or (authorization or "").removeprefix("Bearer ").strip()
    if not given or not hmac.compare_digest(given, settings.api_key):
        raise HTTPException(401, "Ungültiger API-Key")


class MediaIn(BaseModel):
    name: str
    type: str = Field(default="show", pattern="^(show|movie)$")
    tmdbId: int | None = None
    imdbId: str | None = None
    tvdbId: int | None = None


class RulesetIn(BaseModel):
    mediaId: int
    topic: str
    priority: int = 0
    filters: list[dict[str, Any]] | str = []
    titleRegexRules: list[dict[str, Any]] | str = []
    episodeRegex: str | None = None
    seasonRegex: str | None = None
    matchingStrategy: str


class GeneratedIn(BaseModel):
    name: str
    rulesets: list[dict[str, Any]]
    matchRate: float | None = None


class GenerateIn(BaseModel):
    tvdbIds: list[int] = []
    names: list[str] = []
    sonarr: bool = False
    force: bool = False
    forceLlm: bool = False


def _check_ruleset(r: RulesetIn) -> dict[str, Any]:
    if r.matchingStrategy not in STRATEGIES:
        raise HTTPException(400, f"matchingStrategy muss eine von {STRATEGIES} sein")
    if not db.get_media(r.mediaId):
        raise HTTPException(400, "mediaId existiert nicht")
    return r.model_dump()


# ---------------- public, MediathekArr-compatible ----------------

@app.get("/api/v1/rulesets")
@app.get("/metadata/api/rulesets.php")
def public_rulesets(page: int = 1) -> dict[str, Any]:
    return db.public_rulesets_page(page)


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "rulesets": db.count_rulesets()}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


# ---------------- read ----------------

@app.get("/api/media")
def list_media() -> list[dict[str, Any]]:
    return db.list_media()


@app.get("/api/rulesets")
def list_rulesets(mediaId: int | None = None, tvdbId: int | None = None) -> list[dict[str, Any]]:
    return db.list_rulesets(media_id=mediaId, tvdb_id=tvdbId)


@app.get("/api/generation-log")
def generation_log() -> list[dict[str, Any]]:
    return db.generation_log()


@app.get("/api/export")
def export_all() -> dict[str, Any]:
    return db.export_all()


@app.get("/api/settings")
def public_settings() -> dict[str, Any]:
    """What is configured (no secrets), for the overview page."""
    return {
        "writable": bool(settings.api_key),
        "llm": bool(settings.llm_base_url and settings.llm_model),
        "llmModel": settings.llm_model,
        "tvdbDirect": bool(settings.tvdb_api_key),
        "sonarr": bool(settings.sonarr_url and settings.sonarr_api_key),
        "remoteTarget": settings.target_url or None,
        "minMatchRate": settings.min_match_rate,
    }


# ---------------- write ----------------

@app.post("/api/media", dependencies=[Depends(require_key)])
def create_media(m: MediaIn) -> dict[str, Any]:
    return {"success": True, "id": db.create_media(m.model_dump())}


@app.put("/api/media/{media_id}", dependencies=[Depends(require_key)])
def update_media(media_id: int, m: MediaIn) -> dict[str, Any]:
    if not db.get_media(media_id):
        raise HTTPException(404)
    db.update_media(media_id, m.model_dump())
    return {"success": True}


@app.delete("/api/media/{media_id}", dependencies=[Depends(require_key)])
def delete_media(media_id: int) -> dict[str, Any]:
    db.delete_media(media_id)
    return {"success": True}


@app.post("/api/rulesets", dependencies=[Depends(require_key)])
def create_ruleset(r: RulesetIn) -> dict[str, Any]:
    return {"success": True, "id": db.create_ruleset(_check_ruleset(r))}


@app.put("/api/rulesets/{ruleset_id}", dependencies=[Depends(require_key)])
def update_ruleset(ruleset_id: int, r: RulesetIn) -> dict[str, Any]:
    if not db.get_ruleset(ruleset_id):
        raise HTTPException(404)
    db.update_ruleset(ruleset_id, _check_ruleset(r))  # edited by hand -> 'manual', so imports won't overwrite it
    return {"success": True}


@app.delete("/api/rulesets/{ruleset_id}", dependencies=[Depends(require_key)])
def delete_ruleset(ruleset_id: int) -> dict[str, Any]:
    db.delete_ruleset(ruleset_id)
    return {"success": True}


@app.put("/api/generated/{tvdb_id}", dependencies=[Depends(require_key)])
def store_generated(tvdb_id: int, body: GeneratedIn) -> dict[str, Any]:
    """Used by a local generator to publish its result to this (hosted) instance."""
    for r in body.rulesets:
        if r.get("matchingStrategy") not in STRATEGIES:
            raise HTTPException(400, f"matchingStrategy muss eine von {STRATEGIES} sein")
    media_id = db.ensure_media(body.name, tvdb_id)
    ids = db.replace_generated(media_id, body.rulesets, body.matchRate)
    db.log_generation(tvdb_id, body.name, "ok", f"{len(ids)} Ruleset(s) vom Generator hochgeladen")
    return {"success": True, "mediaId": media_id, "ids": ids}


@app.post("/api/generate", dependencies=[Depends(require_key)])
def start_generation(body: GenerateIn, background: BackgroundTasks) -> dict[str, Any]:
    ids = list(body.tvdbIds)
    if body.names:
        if not runner.shows.can_search:
            raise HTTPException(400, "Suche nach Namen braucht TVDB_API_KEY")
        ids += runner.search_ids(body.names)
    if body.sonarr:
        ids += runner.sonarr_ids()
    ids = list(dict.fromkeys(ids))
    background.add_task(runner.run_many, ids, force=body.force, force_llm=body.forceLlm)
    return {"started": True, "tvdbIds": ids}


@app.post("/api/import/upstream", dependencies=[Depends(require_key)])
def import_upstream(url: str | None = Query(default=None)) -> dict[str, Any]:
    entries = fetch_upstream_rulesets(url or settings.upstream_url)
    return {"fetched": len(entries), **db.upsert_upstream(entries)}


@app.post("/api/import", dependencies=[Depends(require_key)])
def import_all(data: dict[str, Any]) -> dict[str, Any]:
    return db.import_all(data)


# ---------------- background jobs ----------------

def _initial_import() -> None:
    try:
        entries = fetch_upstream_rulesets(settings.upstream_url)
        log.info("Upstream-Import: %s", db.upsert_upstream(entries))
    except Exception as ex:
        log.warning("Upstream-Import fehlgeschlagen: %s", ex)


def _schedule_loop() -> None:
    while True:
        try:
            ids = runner.sonarr_ids()
            log.info("Geplanter Lauf für %d Serien aus Sonarr", len(ids))
            runner.run_many(ids)
        except Exception as ex:
            log.warning("Geplanter Lauf fehlgeschlagen: %s", ex)
        time.sleep(settings.generate_interval_hours * 3600)


def _start_background_jobs() -> None:
    if settings.import_upstream_on_start and db.count_rulesets() == 0:
        threading.Thread(target=_initial_import, daemon=True).start()
    if settings.generate_interval_hours > 0 and settings.sonarr_url:
        threading.Thread(target=_schedule_loop, daemon=True).start()
