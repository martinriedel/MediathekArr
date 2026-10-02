"""External data: MediathekView, TVDB, Sonarr and the upstream ruleset API."""
from __future__ import annotations

import codecs
import json
import logging
import lzma
import time
from typing import Any, Iterable, Iterator

import httpx

from .config import Settings
from .matcher import Item, Show

log = logging.getLogger(__name__)

USER_AGENT = "MediathekArr-Rulesets/1.0"
MEDIATHEKVIEW_URL = "https://mediathekviewweb.de/api/query"
TVDB_URL = "https://api4.thetvdb.com/v4"


def _client() -> httpx.Client:
    return httpx.Client(timeout=60, headers={"User-Agent": USER_AGENT}, follow_redirects=True)


# ---------------- MediathekView ----------------

def mediathekview_pages(queries: list[dict[str, Any]], max_size: int = 1000, duration_min: int | None = None,
                        pause: float = 0.0) -> Iterator[list[Item]]:
    """Newest entries first, page by page. max_size 0 walks the whole catalogue."""
    offset = 0
    with _client() as c:
        while not max_size or offset < max_size:
            size = 1000 if not max_size else min(1000, max_size - offset)
            body: dict[str, Any] = {"queries": queries, "sortBy": "filmlisteTimestamp", "sortOrder": "desc",
                                    "future": True, "offset": offset, "size": size}
            if duration_min:
                body["duration_min"] = duration_min
            r = c.post(MEDIATHEKVIEW_URL, content=json.dumps(body), headers={"Content-Type": "text/plain"})
            if r.status_code != 200:
                log.warning("MediathekView query failed: %s %s", r.status_code, r.text[:200])
                return
            page = (r.json().get("result") or {}).get("results") or []
            yield [Item.from_api(x) for x in page]
            if len(page) < size:
                return
            offset += size
            if pause:
                time.sleep(pause)


def mediathekview_query(queries: list[dict[str, Any]], max_size: int = 1000) -> list[Item]:
    return [it for page in mediathekview_pages(queries, max_size) for it in page]


FILMLISTE_URL = "https://liste.mediathekview.de/Filmliste-akt.xz"
_DECODER = json.JSONDecoder()


def _seconds(hms: str) -> int:
    try:
        parts = [int(p) for p in hms.split(":")]
    except ValueError:
        return 0
    total = 0
    for p in parts:
        total = total * 60 + p
    return total


def parse_filmliste(text_chunks: Iterable[str]) -> Iterator[Item]:
    """Entries of MediathekView's full film list, read piece by piece.

    The file is one JSON object with repeated keys: two "Filmliste" rows (metadata, then column names) and one
    "X" row per entry. An empty channel or topic means "same as the entry before"."""
    cols: dict[str, int] = {}
    channel = topic = ""
    buf = ""
    pos = 0
    chunks = iter(text_chunks)
    done = False
    while True:
        i = buf.find('"', pos)
        key_end = buf.find('":', i + 1) if i >= 0 else -1
        if i < 0 or key_end < 0:
            if done:
                return
            buf = buf[pos:]
            pos = 0
            try:
                buf += next(chunks)
            except StopIteration:
                done = True
            continue
        key = buf[i + 1:key_end]
        start = key_end + 2
        while start < len(buf) and buf[start] in " \r\n\t":
            start += 1
        try:
            row, end = _DECODER.raw_decode(buf, start)
        except ValueError:
            if done:
                return
            try:
                buf = buf[pos:] + next(chunks)
            except StopIteration:
                done = True
            pos = 0
            continue
        pos = end
        if not isinstance(row, list):
            continue
        if key == "Filmliste":
            if "Sender" in row and "Thema" in row:
                cols = {name: n for n, name in enumerate(row)}
            continue
        if key != "X":
            continue

        def col(name: str, default: int) -> str:
            n = cols.get(name, default)
            return str(row[n]) if n < len(row) and row[n] is not None else ""
        channel = col("Sender", 0) or channel
        topic = col("Thema", 1) or topic
        try:
            ts = int(col("DatumL", 16) or 0)
        except ValueError:
            ts = 0
        yield Item(channel=channel, topic=topic.replace("–", "-"), title=col("Titel", 2).replace("–", "-"),
                   description=col("Beschreibung", 7), timestamp=ts, duration=_seconds(col("Dauer", 5)),
                   url_website=col("Website", 9), url_video=col("Url", 8))


def filmliste_items(url: str = FILMLISTE_URL) -> Iterator[Item]:
    """Download (xz-compressed) and parse the full film list without holding it in memory as a whole."""
    def chunks() -> Iterator[str]:
        dec = lzma.LZMADecompressor()
        text = codecs.getincrementaldecoder("utf-8")(errors="replace")  # keeps characters split across chunks intact
        with _client() as c, c.stream("GET", url, timeout=300) as r:
            r.raise_for_status()
            for raw in r.iter_bytes(1 << 20):
                yield text.decode(dec.decompress(raw))
            yield text.decode(b"", final=True)
    return parse_filmliste(chunks())


def search_query_for(show: Show) -> str:
    """Same query MediathekArr builds in FetchCachedApiResponseForTvdbId."""
    q = (show.german_name or show.name).replace(" & ", " ")
    if "(" in q:
        q = q.split("(")[0]
    return q.strip()


def fetch_items_for_show(show: Show, extra_topics: list[str] | None = None) -> list[Item]:
    """The items MediathekArr would see for this show: name search in topic+title, plus known ruleset topics."""
    query = search_query_for(show)
    all_queries = [[{"fields": ["topic", "title"], "query": query}]]
    for t in extra_topics or []:
        if t.casefold() != query.casefold():
            all_queries.append([{"fields": ["topic"], "query": t}])
    seen: set[str] = set()
    items: list[Item] = []
    for q in all_queries:
        for it in mediathekview_query(q, 1000):
            key = it.url_video or f"{it.topic}|{it.title}|{it.timestamp}"
            if key not in seen:
                seen.add(key)
                items.append(it)
    return items


# ---------------- TVDB ----------------

class ShowSource:
    """Show + episode data in the shape MediathekArr's get_show.php returns.

    With a TVDB key it talks to TVDB v4 directly, otherwise it uses the MediathekArr metadata API.
    Episode names stay in the show's original language, like MediathekArr sees them, so that
    validation predicts MediathekArr's behaviour exactly.
    """

    def __init__(self, settings: Settings):
        self.s = settings
        self._token: str | None = None
        self._token_at = 0.0
        self._cache: dict[int, Show] = {}
        self._payloads: dict[int, tuple[float, dict[str, Any]]] = {}

    @property
    def can_search(self) -> bool:
        return bool(self.s.tvdb_api_key)

    def _tvdb_headers(self) -> dict[str, str]:
        if not self._token or time.time() - self._token_at > 20 * 24 * 3600:
            body = {"apikey": self.s.tvdb_api_key}
            if self.s.tvdb_pin:
                body["pin"] = self.s.tvdb_pin
            with _client() as c:
                r = c.post(f"{TVDB_URL}/login", json=body)
                r.raise_for_status()
                self._token = r.json()["data"]["token"]
                self._token_at = time.time()
        return {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}

    def _tvdb_get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        with _client() as c:
            r = c.get(f"{TVDB_URL}{path}", params=params, headers=self._tvdb_headers())
            r.raise_for_status()
            return r.json()

    def get_show(self, tvdb_id: int) -> Show | None:
        if tvdb_id in self._cache:
            return self._cache[tvdb_id]
        show = self._get_show_tvdb(tvdb_id) if self.s.tvdb_api_key else self._get_show_mediathekarr(tvdb_id)
        if show:
            self._cache[tvdb_id] = show
        return show

    def _get_show_mediathekarr(self, tvdb_id: int) -> Show | None:
        with _client() as c:
            r = c.get(f"{self.s.mediathekarr_api_base_url}/get_show.php", params={"tvdbid": tvdb_id})
            r.raise_for_status()
            data = r.json()
        if data.get("status") != "success" or not data.get("data"):
            return None
        return Show.from_api(data["data"])

    def _get_show_tvdb(self, tvdb_id: int) -> Show | None:
        payload = self.show_payload(tvdb_id)
        return Show.from_api(payload) if payload else None

    def show_payload(self, tvdb_id: int) -> dict[str, Any] | None:
        """The `data` object of get_show.php, built from TVDB directly (needs TVDB_API_KEY); cached for 12 h."""
        cached = self._payloads.get(tvdb_id)
        if cached and time.time() - cached[0] < 12 * 3600:
            return cached[1]
        series = self._tvdb_get(f"/series/{tvdb_id}/extended", {"meta": "episodes", "short": "true"}).get("data")
        if not series:
            return None
        german = series.get("name") or ""
        try:
            german = self._tvdb_get(f"/series/{tvdb_id}/translations/deu").get("data", {}).get("name") or german
        except httpx.HTTPError:
            pass
        aliases = [{"language": a["language"], "name": a.get("name") or ""}
                   for a in series.get("aliases") or [] if isinstance(a, dict) and a.get("language") == "deu"]
        payload = {
            "id": tvdb_id,
            "name": series.get("name") or german,
            "german_name": german,
            "aliases": aliases,
            "episodes": [
                {"name": e.get("name"), "aired": e.get("aired"), "runtime": e.get("runtime"),
                 "seasonNumber": e.get("seasonNumber") or 0, "episodeNumber": e.get("number") or 0,
                 "absoluteNumber": e.get("absoluteNumber")}
                for e in series.get("episodes") or []
            ],
        }
        self._payloads[tvdb_id] = (time.time(), payload)
        return payload

    def search(self, name: str) -> list[dict[str, Any]]:
        """Find series on TVDB by name (needs TVDB_API_KEY). Returns [{tvdb_id, name, year}]."""
        if not self.can_search:
            return []
        data = self._tvdb_get("/search", {"query": name, "type": "series", "limit": 10}).get("data") or []
        out = []
        for d in data:
            tid = d.get("tvdb_id") or str(d.get("id", "")).removeprefix("series-")
            if str(tid).isdigit():
                out.append({"tvdb_id": int(tid), "name": d.get("name"), "year": d.get("year"),
                            "translations": d.get("translations") or {}, "aliases": d.get("aliases") or []})
        return out


# ---------------- Sonarr ----------------

def sonarr_series(settings: Settings) -> list[dict[str, Any]]:
    """All series in Sonarr as [{tvdb_id, title}]."""
    if not settings.sonarr_url or not settings.sonarr_api_key:
        return []
    with _client() as c:
        r = c.get(f"{settings.sonarr_url}/api/v3/series", headers={"X-Api-Key": settings.sonarr_api_key})
        r.raise_for_status()
        return [{"tvdb_id": s["tvdbId"], "title": s.get("title")} for s in r.json() if s.get("tvdbId")]


# ---------------- Upstream rulesets ----------------

def fetch_upstream_rulesets(url: str) -> list[dict[str, Any]]:
    """Paginated {rulesets, pagination} (pcjones.de, this service) or a plain JSON list (Rundfunkarr's rulesets.json)."""
    entries: list[dict[str, Any]] = []
    with _client() as c:
        page = 1
        while page < 100:
            r = c.get(url, params={"page": page})
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list):
                return data
            entries.extend(data.get("rulesets") or [])
            p = data.get("pagination") or {}
            if not p or int(p.get("currentPage", page)) >= int(p.get("totalPages", 0)):
                break
            page += 1
    return entries
