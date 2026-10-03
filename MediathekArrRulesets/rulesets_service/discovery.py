"""Finds shows on its own: walks the MediathekView catalogue (newest first), groups the entries by topic, looks each
uncovered topic up on TVDB and hands the match to the generator. Needs TVDB_API_KEY for the search."""
from __future__ import annotations

import difflib
import logging
import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .config import Settings
from .db import Database, now
from .matcher import Item, format_title, should_skip_item
from .runner import Runner
from .sources import filmliste_items, mediathekview_pages

log = logging.getLogger(__name__)

FULL_SCAN_KEY = "full_scan_started_at"


def _norm(s: str | None) -> str:
    return format_title((s or "").split("(")[0]).casefold().replace(".", " ").replace("-", " ").strip()


def count_topics(items: Iterable[Item], min_minutes: int, counts: dict[str, int]) -> None:
    """Add each topic's programme-length entries to counts (pages can be fed one by one)."""
    for it in items:
        if it.topic and it.duration >= min_minutes * 60 and not should_skip_item(it):
            counts[it.topic] += 1


def candidate_topics(items: Iterable[Item], min_items: int, min_minutes: int,
                     counts: dict[str, int] | None = None) -> list[tuple[str, int]]:
    """Topics that look like a series: several entries of programme length. Returns [(topic, count)], biggest first."""
    if counts is None:
        counts = defaultdict(int)
        count_topics(items, min_minutes, counts)
    return sorted(((t, n) for t, n in counts.items() if n >= min_items), key=lambda t: -t[1])


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
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.running_full = False

    @property
    def running(self) -> bool:
        return self._lock.locked()

    @property
    def full_scan_pending(self) -> str | None:
        """Start time of a full scan that was stopped or interrupted and will be resumed."""
        return self.db.get_state(FULL_SCAN_KEY)

    def stop(self) -> bool:
        """Ask a running scan to stop after the current topic; a full scan resumes from there next time."""
        if not self.running:
            return False
        self._stop.set()
        return True

    def _covered_topics(self) -> set[str]:
        return {t.strip() for r in self.db.list_rulesets() for t in r["topic"].split("|") if t.strip()}

    def _recently_tried(self, topic: str, full: bool = False, since: str | None = None) -> bool:
        last = self.db.last_discovery(topic)
        if not last or last["status"] in ("ok", "covered"):
            return bool(last)
        if full:
            return bool(since) and last["attempted_at"] > since  # already handled earlier in this full scan
        when = datetime.fromisoformat(last["attempted_at"])
        wait = (timedelta(days=self.s.discover_retry_no_match_days) if last["status"] == "no_match"
                else timedelta(hours=self.s.retry_failed_after_hours))
        return datetime.now(timezone.utc) - when < wait

    def run(self, max_topics: int | None = None, dry_run: bool = False, full: bool = False) -> list[dict[str, Any]]:
        """full: whole catalogue, no topic limit, topics that failed or had no TVDB match are tried again, and all stored
        rulesets are checked against the current entries (generated ones that no longer fit are generated anew)."""
        if not self.runner.shows.can_search:
            raise RuntimeError("Entdeckung braucht TVDB_API_KEY für die Suche nach Seriennamen")
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("Entdeckung läuft bereits")
        self._stop.clear()
        self.running_full = full
        try:
            return self._run(max_topics, dry_run, full)
        finally:
            self.running_full = False
            self._lock.release()

    def _run(self, max_topics: int | None, dry_run: bool, full: bool) -> list[dict[str, Any]]:
        limit_items = 0 if full else self.s.discover_items
        since = None
        if full and not dry_run:
            since = self.db.get_state(FULL_SCAN_KEY)
            if since:
                log.info("Kompletter Scan wird fortgesetzt (begonnen %s)", since)
            else:
                since = now()
                self.db.set_state(FULL_SCAN_KEY, since)
                self.db.clear_rechecked()
        counts: dict[str, int] = defaultdict(int)
        seen: set[int] = set()
        source = "filmliste"
        if self.s.discover_source == "filmliste":
            try:
                self._count_filmliste(counts, seen)
            except Exception as ex:
                log.warning("Filmliste nicht lesbar (%s), nehme die Such-API", ex)
                counts.clear()
                seen.clear()
                source = "api"
        else:
            source = "api"
        if source == "api":
            self._count_api(counts, seen, limit_items)
        covered = self._covered_topics()
        todo = [(t, n) for t, n in candidate_topics([], self.s.discover_min_items, self.s.discover_min_minutes, counts)
                if t not in covered and not self._recently_tried(t, full, since)]
        log.info("Entdeckung%s über %s: %d Einträge, %d Themen, %d neu", " (komplett)" if full else "", source,
                 len(seen), len(counts), len(todo))
        if not full:
            todo = todo[: max_topics or self.s.discover_max_topics]
        elif max_topics:
            todo = todo[:max_topics]
        results = []
        for topic, count in todo:
            if self._stop.is_set():
                log.info("Entdeckung angehalten")
                return results
            results.append(self._one(topic, count, dry_run))
        if full:
            # existing rulesets: does each still fit what the Mediathek shows today?
            for res in self.runner.recheck_all(dry_run=dry_run, skip=self.db.rechecked_ids() if not dry_run else None,
                                               done=None if dry_run else self.db.mark_rechecked,
                                               stop=self._stop.is_set):
                results.append(self._log(res["name"], res["tvdbId"], res["status"], res["message"], dry_run))
            if self._stop.is_set():
                log.info("Kompletter Scan angehalten, wird beim nächsten Start fortgesetzt")
                return results
            if not dry_run:
                self.db.set_state(FULL_SCAN_KEY, None)
                self.db.clear_rechecked()
        return results

    def _add(self, items: Iterable[Item], counts: dict[str, int], seen: set[int]) -> None:
        fresh = []
        for it in items:
            key = hash(it.url_video or f"{it.channel}|{it.topic}|{it.title}|{it.timestamp}")
            if key not in seen:
                seen.add(key)
                fresh.append(it)
        count_topics(fresh, self.s.discover_min_minutes, counts)

    def _count_filmliste(self, counts: dict[str, int], seen: set[int]) -> None:
        """MediathekView's full film list: every channel and every entry in one download."""
        batch: list[Item] = []
        for it in filmliste_items(self.s.discover_filmliste_url):
            batch.append(it)
            if len(batch) >= 5000:
                self._add(batch, counts, seen)
                batch = []
        self._add(batch, counts, seen)
        if not seen:
            raise RuntimeError("Filmliste ist leer")

    def _count_api(self, counts: dict[str, int], seen: set[int], limit_items: int) -> None:
        # one walk per channel, because the search API only pages through a limited window per query;
        # only programme-length entries, and a short pause between pages keeps the load on mediathekviewweb.de low
        for channel in self.s.discover_channels:
            query = [{"fields": ["channel"], "query": channel}]
            for page in mediathekview_pages(query, limit_items, duration_min=self.s.discover_min_minutes * 60, pause=0.5):
                self._add(page, counts, seen)

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
