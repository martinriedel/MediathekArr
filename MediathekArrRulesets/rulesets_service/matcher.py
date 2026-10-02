"""Python port of MediathekArr's ruleset matching (MediathekArrServer/Services/MediathekSearchService.cs).

The generator uses this to predict exactly how MediathekArr will apply a ruleset,
so a ruleset is only published once it is known to work. Keep it in sync with the C# code.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

import regex  # .NET-like syntax, e.g. (?<name>...) groups

STRATEGIES = [
    "SeasonAndEpisodeNumber",
    "ByAbsoluteEpisodeNumber",
    "ItemTitleIncludes",
    "ItemTitleExact",
    "ItemTitleEqualsAirdate",
]

FILTER_TYPES = ["ExactMatch", "Contains", "Regex", "GreaterThan", "LowerThan"]

FIELDS = [
    "channel", "topic", "title", "description", "timestamp", "duration", "size",
    "url_website", "url_video", "url_video_low", "url_video_hd", "timestamp_date",
]

_SKIP_TITLE_KEYWORDS = ["Audiodeskription", "Hörfassung", "(klare Sprache)", "Gebärdensprache", "Trailer", "Outtakes:"]
_SKIP_URL_KEYWORDS = ["YXVkaW9kZXNrcmlwdGlvbg"]

_STATIC_SEASON = regex.compile(r"^S\d{1,4}$")
_STATIC_EPISODE = regex.compile(r"^E\d{1,4}$")
_INVALID_CHARS = regex.compile(r"""[/:;,"„"’’‚’@#?$%^*+=!|<>,()|·]""")
_WHITESPACE = regex.compile(r"\s+")

_GERMAN_MONTHS = {
    "januar": 1, "februar": 2, "märz": 3, "april": 4, "mai": 5, "juni": 6,
    "juli": 7, "august": 8, "september": 9, "oktober": 10, "november": 11, "dezember": 12,
}


@dataclass
class Item:
    """One MediathekView result (ApiResultItem)."""
    channel: str = ""
    topic: str = ""
    title: str = ""
    description: str = ""
    timestamp: int = 0
    duration: int = 0
    size: int = 0
    url_website: str = ""
    url_video: str = ""
    url_video_low: str = ""
    url_video_hd: str = ""
    url_subtitle: str = ""

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> "Item":
        def num(v: Any) -> int:
            try:
                return int(v)
            except (TypeError, ValueError):
                return 0

        def s(v: Any) -> str:
            return (v or "").replace("–", "-")

        return cls(
            channel=d.get("channel") or "",
            topic=s(d.get("topic")),
            title=s(d.get("title")),
            description=d.get("description") or "",
            timestamp=num(d.get("filmlisteTimestamp", d.get("timestamp"))),
            duration=num(d.get("duration")),
            size=num(d.get("size")),
            url_website=d.get("url_website") or "",
            url_video=d.get("url_video") or "",
            url_video_low=d.get("url_video_low") or "",
            url_video_hd=d.get("url_video_hd") or "",
            url_subtitle=d.get("url_subtitle") or "",
        )


@dataclass
class Episode:
    name: str | None
    aired: date | None
    runtime: int | None
    season: int
    number: int
    absolute: int | None = None


@dataclass
class Show:
    tvdb_id: int
    name: str
    german_name: str
    episodes: list[Episode] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)

    @property
    def display_name(self) -> str:
        return self.name or self.german_name

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> "Show":
        """Parse the `data` object of MediathekArr's get_show.php response."""
        episodes = []
        for e in d.get("episodes") or []:
            aired = None
            if e.get("aired"):
                try:
                    aired = date.fromisoformat(str(e["aired"])[:10])
                except ValueError:
                    aired = None
            episodes.append(Episode(
                name=e.get("name"),
                aired=aired,
                runtime=e.get("runtime"),
                season=int(e.get("seasonNumber") or 0),
                number=int(e.get("episodeNumber") or 0),
                absolute=e.get("absoluteNumber"),
            ))
        aliases = [a.get("name", "") for a in (d.get("aliases") or []) if isinstance(a, dict)]
        return cls(
            tvdb_id=int(d.get("id") or 0),
            name=d.get("name") or "",
            german_name=d.get("german_name") or d.get("name") or "",
            episodes=episodes,
            aliases=aliases,
        )


@dataclass
class Ruleset:
    """A ruleset as MediathekArr consumes it. `filters` and `title_regex_rules` are parsed lists."""
    topic: str
    matching_strategy: str
    priority: int = 0
    filters: list[dict[str, Any]] = field(default_factory=list)
    title_regex_rules: list[dict[str, Any]] = field(default_factory=list)
    episode_regex: str | None = None
    season_regex: str | None = None

    @property
    def topics(self) -> list[str]:
        return [t.strip() for t in self.topic.split("|") if t.strip()]


@dataclass
class Match:
    item: Item
    episode: Episode
    matched_title: str


def should_skip_item(item: Item) -> bool:
    if any(k in item.title for k in _SKIP_TITLE_KEYWORDS):
        return True
    if item.channel == "ARD":
        return any(k in item.url_website for k in _SKIP_URL_KEYWORDS)
    if item.channel == "SWR":
        return any(k in item.url_video for k in _SKIP_URL_KEYWORDS)
    return False


def get_field_value(item: Item, name: str) -> str:
    if name == "timestamp_date":
        return datetime.fromtimestamp(item.timestamp, tz=timezone.utc).strftime("%Y%m%d")
    if name in ("timestamp", "duration", "size"):
        return str(getattr(item, name))
    if name in FIELDS:
        return getattr(item, name)
    return ""


def _try_float(v: str) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def filter_matches(item: Item, flt: dict[str, Any]) -> bool:
    value = get_field_value(item, flt.get("attribute", ""))
    expected = str(flt.get("value", ""))
    kind = flt.get("type")
    try:
        if kind == "ExactMatch":
            return value.casefold() == expected.casefold()
        if kind == "Contains":
            return expected.casefold() in value.casefold()
        if kind == "Regex":
            return regex.search(expected, value) is not None
        if kind in ("GreaterThan", "LowerThan"):
            a, b = _try_float(value), _try_float(expected)
            if a is None or b is None:
                return False
            return a > b * 60 if kind == "GreaterThan" else a < b * 60
    except regex.error:
        return False
    return False


def remove_accents_keep_umlauts(text: str) -> str:
    out = []
    for c in unicodedata.normalize("NFD", text):
        if unicodedata.category(c) != "Mn" or c == "̈":
            out.append(c)
    return unicodedata.normalize("NFC", "".join(out))


def format_title(title: str | None) -> str:
    if not title:
        return ""
    title = title.replace("–", "-")
    title = remove_accents_keep_umlauts(title)
    title = title.replace("&", "and")
    title = _INVALID_CHARS.sub("", title)
    title = _WHITESPACE.sub(".", title).replace("..", ".")
    return title


def _extract(source: str | None, pattern: str | None) -> str | None:
    """ExtractValueUsingRegex: first capture group."""
    if not pattern or not source:
        return None
    try:
        m = regex.search(pattern, source)
    except regex.error:
        return None
    if m and m.re.groups >= 1:
        return m.group(1)
    return None


def build_title(item: Item, rules: list[dict[str, Any]]) -> str | None:
    parts = []
    for rule in rules:
        kind = str(rule.get("type", "")).lower()
        if kind == "static":
            if rule.get("value"):
                parts.append(rule["value"])
        elif kind == "regex":
            pattern, fld = rule.get("pattern"), rule.get("field")
            if pattern and fld:
                value = get_field_value(item, fld)
                if value:
                    try:
                        m = regex.search(pattern, value)
                    except regex.error:
                        return None
                    last = m.group(m.re.groups) if m and m.re.groups else (m.group(0) if m else None)
                    if m and last:
                        parts.append(last)
                    else:
                        return None
    return "".join(parts)


def _parse_date(s: str) -> date | None:
    s = s.strip()
    m = regex.fullmatch(r"(\d{1,2})\. (\p{L}+) (\d{4})", s)
    if m and m.group(2).lower() in _GERMAN_MONTHS:
        try:
            return date(int(m.group(3)), _GERMAN_MONTHS[m.group(2).lower()], int(m.group(1)))
        except ValueError:
            return None
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%Y%m%d"):
        try:
            parsed = datetime.strptime(s, fmt).date()
        except ValueError:
            continue
        # .NET's dd/MM/yyyy require two digits; strptime is lenient
        if fmt == "%d.%m.%Y" and not regex.fullmatch(r"\d{2}\.\d{2}\.\d{4}", s):
            continue
        if fmt == "%Y%m%d" and not regex.fullmatch(r"\d{8}", s):
            continue
        return parsed
    return None


def _guess_correct(item: Item, candidates: list[Episode]) -> Episode | None:
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    item_date = datetime.fromtimestamp(item.timestamp, tz=timezone.utc).date()
    for e in candidates:
        if e.aired == item_date:
            return e
    dated = [e for e in candidates if e.aired]
    return max(dated, key=lambda e: e.aired) if dated else candidates[0]


def match_item(item: Item, rs: Ruleset, show: Show) -> Match | None:
    if not show.episodes:
        return None
    strategy = rs.matching_strategy

    if strategy == "SeasonAndEpisodeNumber":
        title = build_title(item, rs.title_regex_rules) if rs.title_regex_rules else item.title
        season = rs.season_regex[1:] if rs.season_regex and _STATIC_SEASON.match(rs.season_regex) else _extract(title, rs.season_regex)
        episode = rs.episode_regex[1:] if rs.episode_regex and _STATIC_EPISODE.match(rs.episode_regex) else _extract(title, rs.episode_regex)
        if not season or not episode or not season.strip().isdigit() or not episode.strip().isdigit():
            return None
        s, e = int(season), int(episode)
        ep = next((x for x in show.episodes if x.season == s and x.number == e), None)
        return Match(item, ep, f"S{season}E{episode}") if ep else None

    if strategy == "ByAbsoluteEpisodeNumber":
        title = build_title(item, rs.title_regex_rules) if rs.title_regex_rules else item.title
        value = _extract(title, rs.episode_regex)
        if not value or not value.strip().isdigit() or int(value) == 0:
            return None
        n = int(value)
        # MediathekArr compares against the per-season episode number, not TVDB's absolute number
        ep = next((x for x in show.episodes if x.number == n), None)
        return Match(item, ep, f"E{value}") if ep else None

    constructed = build_title(item, rs.title_regex_rules)
    if not constructed:
        return None

    if strategy == "ItemTitleIncludes":
        needle = format_title(constructed).casefold()
        ep = next((x for x in show.episodes if needle in format_title(x.name).casefold()), None)
        return Match(item, ep, constructed) if ep else None

    if strategy == "ItemTitleExact":
        wanted = format_title(constructed).casefold()
        ep = _guess_correct(item, [x for x in show.episodes if format_title(x.name).casefold() == wanted])
        return Match(item, ep, constructed) if ep else None

    if strategy == "ItemTitleEqualsAirdate":
        d = _parse_date(constructed)
        if d is None:
            return None
        ep = next((x for x in show.episodes if x.aired == d), None)
        return Match(item, ep, constructed) if ep else None

    return None


def apply_rulesets(items: list[Item], rulesets: list[Ruleset], show: Show) -> tuple[list[Match], list[Item]]:
    """Mirror of ApplyRulesetFilters for one show: returns (matches, in-scope items that no ruleset matched)."""
    by_topic: dict[str, list[Ruleset]] = {}
    for rs in sorted(rulesets, key=lambda r: r.priority):
        for t in rs.topics:
            by_topic.setdefault(t, []).append(rs)

    matches: list[Match] = []
    unmatched: list[Item] = []
    for item in items:
        if should_skip_item(item):
            continue
        candidates = by_topic.get(item.topic, [])
        in_scope = False
        hit = None
        for rs in candidates:
            if not all(filter_matches(item, f) for f in rs.filters):
                continue
            in_scope = True
            hit = match_item(item, rs, show)
            if hit:
                break
        if hit:
            matches.append(hit)
        elif in_scope:
            unmatched.append(item)
    return matches, unmatched
