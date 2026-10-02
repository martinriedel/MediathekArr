"""Runs the generator for a list of shows and stores the result locally or on a remote ruleset service."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .config import Settings
from .db import Database
from .generator import GenerationResult, generate, ruleset_from_payload
from .llm import LLMClient
from .matcher import apply_rulesets
from .sources import ShowSource, fetch_items_for_show, sonarr_series

log = logging.getLogger(__name__)


class RemoteTarget:
    """Pushes results to a hosted ruleset service (GENERATOR_TARGET_URL)."""

    def __init__(self, url: str, api_key: str):
        self.url = url.rstrip("/")
        self.headers = {"X-Api-Key": api_key}

    def has_rulesets(self, tvdb_id: int) -> bool:
        r = httpx.get(f"{self.url}/api/rulesets", params={"tvdbId": tvdb_id}, headers=self.headers, timeout=30)
        r.raise_for_status()
        return bool(r.json())

    def store(self, result: GenerationResult) -> None:
        body = {"name": result.show_name, "rulesets": result.rulesets_payload(), "matchRate": result.match_rate}
        r = httpx.put(f"{self.url}/api/generated/{result.tvdb_id}", json=body, headers=self.headers, timeout=30)
        r.raise_for_status()


class Runner:
    def __init__(self, settings: Settings, db: Database):
        self.s = settings
        self.db = db
        self.shows = ShowSource(settings)
        self.llm = LLMClient(settings)
        self.remote = RemoteTarget(settings.target_url, settings.target_api_key) if settings.target_url else None

    def _has_rulesets(self, tvdb_id: int) -> bool:
        if self.remote:
            return self.remote.has_rulesets(tvdb_id)
        return bool(self.db.list_rulesets(tvdb_id=tvdb_id))

    def _recently_failed(self, tvdb_id: int) -> bool:
        last = self.db.last_generation(tvdb_id)
        if not last or last["status"] == "ok":
            return False
        when = datetime.fromisoformat(last["attempted_at"])
        return datetime.now(timezone.utc) - when < timedelta(hours=self.s.retry_failed_after_hours)

    def run_one(self, tvdb_id: int, force: bool = False, force_llm: bool = False, dry_run: bool = False) -> dict[str, Any]:
        if not force and self._has_rulesets(tvdb_id):
            return {"tvdbId": tvdb_id, "status": "skipped", "message": "hat schon Rulesets"}
        if not force and self._recently_failed(tvdb_id):
            return {"tvdbId": tvdb_id, "status": "skipped", "message": "kürzlich ohne Erfolg versucht"}

        show = self.shows.get_show(tvdb_id)
        if show is None:
            self.db.log_generation(tvdb_id, "", "error", "Serie bei TVDB nicht gefunden")
            return {"tvdbId": tvdb_id, "status": "error", "message": "Serie bei TVDB nicht gefunden"}

        known_topics = sorted({t for r in self.db.list_rulesets(tvdb_id=tvdb_id) for t in r["topic"].split("|") if t.strip()})
        items = fetch_items_for_show(show, known_topics)
        result = generate(show, items, self.llm, self.s.min_match_rate, self.s.llm_max_attempts, force_llm)
        status = "ok" if result.ok else "failed"

        if result.ok and not dry_run:
            if self.remote:
                self.remote.store(result)
            else:
                media_id = self.db.ensure_media(result.show_name, tvdb_id)
                self.db.replace_generated(media_id, result.rulesets_payload(), result.match_rate)
        if not dry_run:
            self.db.log_generation(tvdb_id, result.show_name, status, result.message)
        log.info("%s (%s): %s - %s", result.show_name, tvdb_id, status, result.message)
        return {"tvdbId": tvdb_id, "name": result.show_name, "status": status, "message": result.message,
                "usedLlm": result.used_llm, "tried": result.tried, "rulesets": result.rulesets_payload()}

    def recheck(self, tvdb_id: int, dry_run: bool = False) -> dict[str, Any] | None:
        """Check a show's stored rulesets against the current MediathekView entries.

        Below MIN_MATCH_RATE, rulesets this service generated are generated anew (the old ones stay if that fails);
        imported or hand-made ones are never overwritten, only reported as "stale". Returns None while they still fit."""
        stored = self.db.list_rulesets(tvdb_id=tvdb_id)
        if not stored:
            return None
        show = self.shows.get_show(tvdb_id)
        if show is None:
            return None
        topics = sorted({t for r in stored for t in r["topic"].split("|") if t.strip()})
        items = fetch_items_for_show(show, topics)
        matches, unmatched = apply_rulesets(items, [ruleset_from_payload(r) for r in stored], show)
        total = len(matches) + len(unmatched)
        if not total:
            return None  # nothing in the Mediathek right now, nothing to judge
        rate = len(matches) / total
        if rate >= self.s.min_match_rate:
            return None
        name = stored[0]["mediaName"]
        summary = f"passt nur noch auf {len(matches)}/{total} Einträge ({rate:.0%})"
        if any(r["source"] != "generated" for r in stored):
            if not dry_run:
                self.db.log_generation(tvdb_id, name, "stale", summary + "; importiert oder von Hand, bleibt unverändert")
            return {"tvdbId": tvdb_id, "name": name, "status": "stale", "message": summary}
        res = self.run_one(tvdb_id, force=True, dry_run=dry_run)
        status = "regenerated" if res["status"] == "ok" else "stale"
        return {"tvdbId": tvdb_id, "name": name, "status": status, "message": f"{summary}; neu erzeugt: {res['message']}"}

    def recheck_all(self, dry_run: bool = False) -> list[dict[str, Any]]:
        out = []
        for tvdb_id in sorted({r["tvdbId"] for r in self.db.list_rulesets() if r["tvdbId"]}):
            try:
                res = self.recheck(tvdb_id, dry_run=dry_run)
            except Exception as ex:
                log.warning("Prüfung von %s fehlgeschlagen: %s", tvdb_id, ex)
                continue
            if res:
                log.info("Prüfung %s (%s): %s - %s", res["name"], tvdb_id, res["status"], res["message"])
                out.append(res)
        return out

    def run_many(self, tvdb_ids: list[int], **kw: Any) -> list[dict[str, Any]]:
        out = []
        for tid in tvdb_ids:
            try:
                out.append(self.run_one(tid, **kw))
            except Exception as ex:
                log.exception("Generation for %s failed", tid)
                self.db.log_generation(tid, "", "error", str(ex))
                out.append({"tvdbId": tid, "status": "error", "message": str(ex)})
        return out

    def sonarr_ids(self) -> list[int]:
        return [s["tvdb_id"] for s in sonarr_series(self.s)]

    def search_ids(self, names: list[str]) -> list[int]:
        ids = []
        for n in names:
            hits = self.shows.search(n)
            if hits:
                ids.append(hits[0]["tvdb_id"])
            else:
                log.warning("Keine TVDB-Serie für %r gefunden", n)
        return ids
