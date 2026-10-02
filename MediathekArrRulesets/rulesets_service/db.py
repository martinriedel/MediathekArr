"""SQLite storage. The media/rulesets columns mirror the upstream PHP editor so exports stay compatible."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS media (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    type TEXT NOT NULL CHECK(type IN ('movie', 'show')),
    tmdbId INTEGER,
    imdbId TEXT,
    tvdbId INTEGER,
    upstream_id INTEGER UNIQUE,
    UNIQUE(name, type)
);

CREATE TABLE IF NOT EXISTS rulesets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mediaId INTEGER NOT NULL,
    topic TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    filters TEXT NOT NULL DEFAULT '[]',
    titleRegexRules TEXT NOT NULL DEFAULT '[]',
    episodeRegex TEXT,
    seasonRegex TEXT,
    matchingStrategy TEXT NOT NULL,
    -- upstream: imported from pcjones.de, manual: created/edited via API, generated: by the generator
    source TEXT NOT NULL DEFAULT 'manual',
    upstream_id INTEGER UNIQUE,
    match_rate REAL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(mediaId) REFERENCES media(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS generation_log (
    tvdbId INTEGER PRIMARY KEY,
    name TEXT,
    status TEXT NOT NULL,
    message TEXT,
    attempted_at TEXT NOT NULL
);
"""

RULESET_FIELDS = ["mediaId", "topic", "priority", "filters", "titleRegexRules", "episodeRegex", "seasonRegex", "matchingStrategy"]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json_text(value: Any) -> str:
    """Rulesets carry filters/titleRegexRules as JSON *strings* (that is what MediathekArr parses)."""
    if value is None or value == "":
        return "[]"
    if isinstance(value, str):
        json.loads(value)  # validate
        return value
    return json.dumps(value, ensure_ascii=False)


class Database:
    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
        self._lock = threading.RLock()

    # ---------- helpers ----------
    def _all(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    def _one(self, sql: str, args: tuple = ()) -> dict[str, Any] | None:
        rows = self._all(sql, args)
        return rows[0] if rows else None

    def _exec(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, args)
            self._conn.commit()
            return cur.lastrowid or cur.rowcount

    # ---------- media ----------
    def list_media(self) -> list[dict[str, Any]]:
        return self._all("SELECT id, name, type, tmdbId, imdbId, tvdbId FROM media ORDER BY name")

    def get_media(self, media_id: int) -> dict[str, Any] | None:
        return self._one("SELECT id, name, type, tmdbId, imdbId, tvdbId FROM media WHERE id = ?", (media_id,))

    def find_media(self, tvdb_id: int | None = None, name: str | None = None, type_: str | None = None) -> dict[str, Any] | None:
        if tvdb_id:
            row = self._one("SELECT * FROM media WHERE tvdbId = ? ORDER BY id LIMIT 1", (tvdb_id,))
            if row:
                return row
        if name and type_:
            return self._one("SELECT * FROM media WHERE name = ? AND type = ?", (name, type_))
        return None

    def create_media(self, m: dict[str, Any]) -> int:
        return self._exec(
            "INSERT INTO media (name, type, tmdbId, imdbId, tvdbId, upstream_id) VALUES (?, ?, ?, ?, ?, ?)",
            (m["name"], m.get("type", "show"), m.get("tmdbId"), m.get("imdbId"), m.get("tvdbId"), m.get("upstream_id")),
        )

    def update_media(self, media_id: int, m: dict[str, Any]) -> None:
        self._exec(
            "UPDATE media SET name = ?, type = ?, tmdbId = ?, imdbId = ?, tvdbId = ? WHERE id = ?",
            (m["name"], m.get("type", "show"), m.get("tmdbId"), m.get("imdbId"), m.get("tvdbId"), media_id),
        )

    def delete_media(self, media_id: int) -> None:
        self._exec("DELETE FROM media WHERE id = ?", (media_id,))

    def ensure_media(self, name: str, tvdb_id: int | None, type_: str = "show") -> int:
        existing = self.find_media(tvdb_id=tvdb_id, name=name, type_=type_)
        if existing:
            return existing["id"]
        return self.create_media({"name": name, "type": type_, "tvdbId": tvdb_id})

    # ---------- rulesets ----------
    def count_rulesets(self) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM rulesets")
        return row["n"] if row else 0

    def public_rulesets_page(self, page: int, per_page: int = 500) -> dict[str, Any]:
        """Exactly the shape of upstream's rulesets.php, which MediathekArr's RulesetApiResponse expects."""
        page = max(1, page)
        rows = self._all(
            """
            SELECT r.*, m.id AS media_id, m.name AS media_name, m.type AS media_type,
                   m.tmdbId AS media_tmdbId, m.imdbId AS media_imdbId, m.tvdbId AS media_tvdbId
            FROM rulesets r JOIN media m ON r.mediaId = m.id
            ORDER BY r.priority, r.id
            LIMIT ? OFFSET ?
            """,
            (per_page, (page - 1) * per_page),
        )
        total = self.count_rulesets()
        return {
            "rulesets": [
                {
                    "id": r["id"],
                    "mediaId": r["mediaId"],
                    "topic": r["topic"],
                    "priority": r["priority"],
                    "filters": r["filters"] or "[]",
                    "titleRegexRules": r["titleRegexRules"] or "[]",
                    "episodeRegex": r["episodeRegex"],
                    "seasonRegex": r["seasonRegex"],
                    "matchingStrategy": r["matchingStrategy"],
                    "media": {
                        "media_id": r["media_id"],
                        "media_name": r["media_name"],
                        "media_type": r["media_type"],
                        "media_tmdbId": r["media_tmdbId"] or None,
                        "media_imdbId": r["media_imdbId"] or None,
                        "media_tvdbId": r["media_tvdbId"] or None,
                    },
                }
                for r in rows
            ],
            "pagination": {
                "currentPage": page,
                "totalPages": max(1, -(-total // per_page)),
                "totalItems": total,
                "itemsPerPage": per_page,
            },
        }

    def list_rulesets(self, media_id: int | None = None, tvdb_id: int | None = None) -> list[dict[str, Any]]:
        sql = "SELECT r.*, m.name AS mediaName, m.tvdbId AS tvdbId FROM rulesets r JOIN media m ON r.mediaId = m.id"
        args: tuple = ()
        if media_id is not None:
            sql += " WHERE r.mediaId = ?"
            args = (media_id,)
        elif tvdb_id is not None:
            sql += " WHERE m.tvdbId = ?"
            args = (tvdb_id,)
        return self._all(sql + " ORDER BY m.name, r.priority, r.id", args)

    def get_ruleset(self, ruleset_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM rulesets WHERE id = ?", (ruleset_id,))

    def create_ruleset(self, r: dict[str, Any], source: str = "manual", upstream_id: int | None = None, match_rate: float | None = None) -> int:
        return self._exec(
            """INSERT INTO rulesets (mediaId, topic, priority, filters, titleRegexRules, episodeRegex, seasonRegex,
                                     matchingStrategy, source, upstream_id, match_rate, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (r["mediaId"], r["topic"], int(r.get("priority") or 0), _json_text(r.get("filters")),
             _json_text(r.get("titleRegexRules")), r.get("episodeRegex") or None, r.get("seasonRegex") or None,
             r["matchingStrategy"], source, upstream_id, match_rate, now()),
        )

    def update_ruleset(self, ruleset_id: int, r: dict[str, Any], source: str = "manual", match_rate: float | None = None) -> None:
        self._exec(
            """UPDATE rulesets SET mediaId = ?, topic = ?, priority = ?, filters = ?, titleRegexRules = ?,
                   episodeRegex = ?, seasonRegex = ?, matchingStrategy = ?, source = ?, match_rate = ?, updated_at = ?
               WHERE id = ?""",
            (r["mediaId"], r["topic"], int(r.get("priority") or 0), _json_text(r.get("filters")),
             _json_text(r.get("titleRegexRules")), r.get("episodeRegex") or None, r.get("seasonRegex") or None,
             r["matchingStrategy"], source, match_rate, now(), ruleset_id),
        )

    def delete_ruleset(self, ruleset_id: int) -> None:
        self._exec("DELETE FROM rulesets WHERE id = ?", (ruleset_id,))

    def replace_generated(self, media_id: int, rulesets: list[dict[str, Any]], match_rate: float | None) -> list[int]:
        """Swap a show's generated rulesets for new ones. Manual and upstream rulesets are left alone."""
        with self._lock:
            self._conn.execute("DELETE FROM rulesets WHERE mediaId = ? AND source = 'generated'", (media_id,))
            self._conn.commit()
        return [self.create_ruleset({**r, "mediaId": media_id}, source="generated", match_rate=match_rate) for r in rulesets]

    # ---------- upstream import ----------
    def upsert_upstream(self, entries: list[dict[str, Any]]) -> dict[str, int]:
        """Import rulesets in upstream's public format. Rulesets edited locally (source != upstream) are kept."""
        stats = {"created": 0, "updated": 0, "kept_local": 0}
        for e in entries:
            m = e.get("media") or {}
            media_name = m.get("media_name") or "Unknown"
            media_type = m.get("media_type") or "show"
            row = self._one("SELECT * FROM media WHERE upstream_id = ?", (m.get("media_id"),)) if m.get("media_id") else None
            row = row or self.find_media(name=media_name, type_=media_type)
            media_data = {"name": media_name, "type": media_type, "tmdbId": m.get("media_tmdbId"),
                          "imdbId": m.get("media_imdbId"), "tvdbId": m.get("media_tvdbId")}
            if row:
                media_id = row["id"]
                if row.get("upstream_id") is None and m.get("media_id"):
                    self._exec("UPDATE media SET upstream_id = ? WHERE id = ?", (m.get("media_id"), media_id))
            else:
                media_id = self.create_media({**media_data, "upstream_id": m.get("media_id")})

            data = {k: e.get(k) for k in RULESET_FIELDS}
            data["mediaId"] = media_id
            existing = self._one("SELECT * FROM rulesets WHERE upstream_id = ?", (e.get("id"),)) if e.get("id") is not None else None
            if existing is None:
                self.create_ruleset(data, source="upstream", upstream_id=e.get("id"))
                stats["created"] += 1
            elif existing["source"] == "upstream":
                self.update_ruleset(existing["id"], data, source="upstream")
                stats["updated"] += 1
            else:
                stats["kept_local"] += 1
        return stats

    # ---------- export / import ----------
    def export_all(self) -> dict[str, Any]:
        return {
            "media": self._all("SELECT id, name, type, tmdbId, imdbId, tvdbId FROM media ORDER BY id"),
            "rulesets": self._all("SELECT id, mediaId, topic, priority, filters, titleRegexRules, episodeRegex, seasonRegex, "
                                  "matchingStrategy, source, match_rate FROM rulesets ORDER BY id"),
        }

    def import_all(self, data: dict[str, Any]) -> dict[str, int]:
        """Restore an export. Media are matched by tvdbId or name; rulesets are appended."""
        id_map: dict[int, int] = {}
        for m in data.get("media", []):
            id_map[m["id"]] = self.ensure_media(m["name"], m.get("tvdbId"), m.get("type", "show"))
        count = 0
        for r in data.get("rulesets", []):
            if r.get("mediaId") not in id_map:
                continue
            self.create_ruleset({**r, "mediaId": id_map[r["mediaId"]]}, source=r.get("source") or "manual", match_rate=r.get("match_rate"))
            count += 1
        return {"media": len(id_map), "rulesets": count}

    # ---------- generation log ----------
    def log_generation(self, tvdb_id: int, name: str, status: str, message: str) -> None:
        self._exec(
            "INSERT INTO generation_log (tvdbId, name, status, message, attempted_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(tvdbId) DO UPDATE SET name = excluded.name, status = excluded.status, "
            "message = excluded.message, attempted_at = excluded.attempted_at",
            (tvdb_id, name, status, message, now()),
        )

    def generation_log(self) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM generation_log ORDER BY attempted_at DESC")

    def last_generation(self, tvdb_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM generation_log WHERE tvdbId = ?", (tvdb_id,))
