"""Finds shows on its own: walks the newest MediathekView entries, groups them by topic, looks each
uncovered topic up on TVDB and hands the match to the generator. Needs TVDB_API_KEY for the search."""
from __future__ import annotations

import difflib
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import Settings
from .db import Database
from .matcher import Item, format_title, should_skip_item
from .runner import Runner
from .sources import mediathekview_query

log = logging.getLogger(__name__)


def _norm(s: str | None) -> str:
    return format_title((s or "").split("(")[0]).casefold().replace(".", " ").replace("-", " ").strip()


def candidate_topics(items: list[Item], min_items: int, min_minutes: int) -> list[tuple[str, int]]:
    """Topics that look like a series: several entries of programme length. Returns [(topic, count)], biggest first."""
    by_topic: dict[str, list[Item]] = defaultdict(list)
    for it in items:
        if it.topic and not should_skip_item(it):
            by_topic[it.topic].append(it)
    out = []
    for topic, its in by_topic.items():
        long_enough = [it for it in its if it.duration >= min_minutes * 60]
        if len(long_enough) >= min_items:
            out.append((topic, len(long_enough)))
    return sorted(out, key=lambda t: -t[1])


def best_tvdb_match(topic: str, hits: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, float]:
    """Pick the TVDB search hit whose name (any language or alias) is closest to the topic."""
    wanted = _norm(topic)
    best, best_score = None, 0.0
    for h in hits:
        names = [h.get("name"), *(h.get("translations") or {}).values(), *(h.get("aliases") or [])]
        score = max((difflib.SequenceMatcher(None, wanted, _norm(n)).ratio() for n in names if isinstance(n, str) and n), default=0.0)
        if score > best_score:
            best, best_score = h, score
    return best, best_score


class Discovery:
    def __init__(self, settings: Settings, db: Database, runner: Runner):
        self.s = settings
        self.db = db
        self.runner = runner

    def _covered_topics(self) -> set[str]:
        return {t.strip() for r in self.db.list_rulesets() for t in r["topic"].split("|") if t.strip()}

    def _recently_tried(self, topic: str) -> bool:
        last = self.db.last_discovery(topic)
        if not last or last["status"] == "ok":
            return bool(last)
        when = datetime.fromisoformat(last["attempted_at"])
        return datetime.now(timezone.utc) - when < timedelta(hours=self.s.retry_failed_after_hours)

    def run(self, max_topics: int | None = None, dry_run: bool = False) -> list[dict[str, Any]]:
        if not self.runner.shows.can_search:
            raise RuntimeError("Entdeckung braucht TVDB_API_KEY für die Suche nach Seriennamen")
        items = mediathekview_query([], self.s.discover_items)
        covered = self._covered_topics()
        todo = [(t, n) for t, n in candidate_topics(items, self.s.discover_min_items, self.s.discover_min_minutes)
                if t not in covered and not self._recently_tried(t)]
        log.info("Entdeckung: %d Einträge, %d neue Themen", len(items), len(todo))
        results = []
        for topic, count in todo[: max_topics or self.s.discover_max_topics]:
            results.append(self._one(topic, count, dry_run))
        return results

    def _one(self, topic: str, count: int, dry_run: bool) -> dict[str, Any]:
        try:
            hit, score = best_tvdb_match(topic, self.runner.shows.search(topic))
        except Exception as ex:
            return self._log(topic, None, "error", f"TVDB-Suche fehlgeschlagen: {ex}", dry_run)
        if hit is None or score < self.s.discover_min_name_score:
            return self._log(topic, None, "no_match", f"keine passende TVDB-Serie ({count} Einträge)", dry_run)
        res = self.runner.run_one(hit["tvdb_id"], dry_run=dry_run)
        status = {"ok": "ok", "skipped": "covered"}.get(res["status"], "failed")
        return self._log(topic, hit["tvdb_id"], status, f"{hit.get('name')}: {res['message']}", dry_run)

    def _log(self, topic: str, tvdb_id: int | None, status: str, message: str, dry_run: bool) -> dict[str, Any]:
        if not dry_run:
            self.db.log_discovery(topic, tvdb_id, status, message)
        log.info("Entdeckung %r: %s - %s", topic, status, message)
        return {"topic": topic, "tvdbId": tvdb_id, "status": status, "message": message}
