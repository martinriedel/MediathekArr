"""Builds rulesets for a show automatically.

1. Load the show's episodes (TVDB) and the MediathekView items MediathekArr would see for it.
2. Try built-in candidate rulesets per MediathekView topic.
3. If none is good enough and a local LLM is configured, let the LLM propose rulesets,
   feeding back the validation result until one passes or the attempts run out.
4. Every candidate is validated with the Python port of MediathekArr's matcher, so only rulesets
   that actually map MediathekView items to the right TVDB episodes are kept.
"""
from __future__ import annotations

import difflib
import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import regex

from .llm import LLMClient
from .matcher import (FIELDS, FILTER_TYPES, STRATEGIES, Item, Match, Ruleset, Show, apply_rulesets,
                      filter_matches, format_title, should_skip_item)

log = logging.getLogger(__name__)

_GENERIC_EPISODE_NAME = regex.compile(r"^\s*(folge|episode|teil|part)\s*\d+\s*$", regex.IGNORECASE)
_STRIP_PARENS = r"(?:\s*\([^)]*\))*\s*$"


@dataclass
class Evaluation:
    ruleset: Ruleset
    topic_items: int = 0       # non-skipped items with this topic
    in_scope: int = 0          # of those, items that pass the filters
    matched: int = 0
    distinct_episodes: int = 0
    verifiable: int = 0        # matches we can cross-check by episode name or air date
    plausible: int = 0
    unmatched_samples: list[str] = field(default_factory=list)
    implausible_samples: list[str] = field(default_factory=list)

    @property
    def coverage(self) -> float:
        return self.matched / self.in_scope if self.in_scope else 0.0

    @property
    def topic_share(self) -> float:
        return self.matched / self.topic_items if self.topic_items else 0.0

    @property
    def plausibility(self) -> float:
        return self.plausible / self.verifiable if self.verifiable else 1.0

    def passes(self, min_rate: float) -> bool:
        if self.ruleset.matching_strategy == "ItemTitleEqualsAirdate" and self.verifiable * 2 < self.matched:
            return False  # dates alone are too weak (re-uploads, repeats) unless episode names confirm them
        return (self.matched >= 1 and self.coverage >= min_rate and self.topic_share >= 0.5
                and self.plausibility >= 0.7)

    def score(self) -> tuple:
        strategy = self.ruleset.matching_strategy
        preference = -_STRATEGY_PREFERENCE.index(strategy) if strategy in _STRATEGY_PREFERENCE else -99
        return (self.passes(0.0), self.matched * self.plausibility, self.coverage, preference)

    def summary(self) -> str:
        return (f"{self.matched}/{self.in_scope} Einträge zugeordnet ({self.coverage:.0%}), "
                f"{self.distinct_episodes} Folgen, Plausibilität {self.plausible}/{self.verifiable}")


@dataclass
class GenerationResult:
    tvdb_id: int
    show_name: str
    accepted: list[Evaluation]
    tried: int
    used_llm: bool
    message: str

    @property
    def ok(self) -> bool:
        return bool(self.accepted)

    def rulesets_payload(self) -> list[dict[str, Any]]:
        return [ruleset_to_payload(e.ruleset) for e in self.accepted]

    @property
    def match_rate(self) -> float | None:
        if not self.accepted:
            return None
        matched = sum(e.matched for e in self.accepted)
        scope = sum(e.in_scope for e in self.accepted)
        return round(matched / scope, 3) if scope else None


def ruleset_to_payload(rs: Ruleset) -> dict[str, Any]:
    return {
        "topic": rs.topic,
        "priority": rs.priority,
        "filters": rs.filters,
        "titleRegexRules": rs.title_regex_rules,
        "episodeRegex": rs.episode_regex,
        "seasonRegex": rs.season_regex,
        "matchingStrategy": rs.matching_strategy,
    }


def ruleset_from_payload(d: dict[str, Any]) -> Ruleset:
    def as_list(v: Any) -> list:
        if isinstance(v, str):
            v = json.loads(v or "[]")
        return list(v or [])

    return Ruleset(
        topic=str(d.get("topic") or ""),
        matching_strategy=str(d.get("matchingStrategy") or ""),
        priority=int(d.get("priority") or 0),
        filters=as_list(d.get("filters")),
        title_regex_rules=as_list(d.get("titleRegexRules")),
        episode_regex=d.get("episodeRegex") or None,
        season_regex=d.get("seasonRegex") or None,
    )


# ---------------- validation ----------------

def _norm(s: str | None) -> str:
    return format_title(s or "").casefold().replace(".", " ").strip()


# Preferred strategy when several match equally well (most robust first)
_STRATEGY_PREFERENCE = ["SeasonAndEpisodeNumber", "ItemTitleExact", "ItemTitleIncludes",
                        "ByAbsoluteEpisodeNumber", "ItemTitleEqualsAirdate"]


def _plausible(m: Match, strategy: str) -> bool | None:
    """Cross-check a match with a signal the strategy did not use itself.

    True/False if checkable, None if not (title strategies already matched on the episode name;
    a date check would be circular for airdate strategies)."""
    if strategy in ("ItemTitleExact", "ItemTitleIncludes"):
        return None
    ep, item = m.episode, m.item
    checks: list[bool] = []
    if ep.name and not _GENERIC_EPISODE_NAME.match(ep.name):
        name, title = _norm(ep.name), _norm(item.title)
        if name and title:
            checks.append(name in title or difflib.SequenceMatcher(None, name, title).ratio() >= 0.6
                          or any(difflib.SequenceMatcher(None, name, part.strip()).ratio() >= 0.75
                                 for part in regex.split(r" - |: |\(", title)))
    if ep.aired and item.timestamp and strategy != "ItemTitleEqualsAirdate":
        item_date = datetime.fromtimestamp(item.timestamp, tz=timezone.utc).date()
        checks.append(abs((item_date - ep.aired).days) <= 7)
    if not checks:
        return None
    return any(checks)


def evaluate(ruleset: Ruleset, items: list[Item], show: Show) -> Evaluation:
    ev = Evaluation(ruleset=ruleset)
    topics = set(ruleset.topics)
    # In a shared topic (e.g. "Krimi am Samstag") only entries naming the show, or passing the filters, belong to it
    names = [n.casefold() for n in show_names(show)]
    ev.topic_items = sum(
        1 for it in items
        if it.topic in topics and not should_skip_item(it)
        and (not ruleset.filters or any(n in it.title.casefold() for n in names)
             or all(filter_matches(it, f) for f in ruleset.filters)))
    matches, unmatched = apply_rulesets(items, [ruleset], show)
    ev.matched = len(matches)
    ev.in_scope = len(matches) + len(unmatched)
    ev.distinct_episodes = len({(m.episode.season, m.episode.number) for m in matches})
    for m in matches:
        p = _plausible(m, ruleset.matching_strategy)
        if p is None:
            continue
        ev.verifiable += 1
        if p:
            ev.plausible += 1
        elif len(ev.implausible_samples) < 8:
            ev.implausible_samples.append(
                f"'{m.item.title}' ({datetime.fromtimestamp(m.item.timestamp, tz=timezone.utc):%Y-%m-%d}) -> "
                f"S{m.episode.season:02d}E{m.episode.number:02d} '{m.episode.name}' ({m.episode.aired})")
    ev.unmatched_samples = [it.title for it in unmatched[:10]]
    return ev


# ---------------- candidate topics and heuristics ----------------

def show_names(show: Show) -> list[str]:
    names = [show.german_name, show.name, *show.aliases]
    out: list[str] = []
    for n in names:
        n = (n or "").split("(")[0].strip()
        if n and n.casefold() not in [o.casefold() for o in out]:
            out.append(n)
    return out


def candidate_topics(items: list[Item], show: Show) -> list[tuple[str, str | None]]:
    """[(topic, name_filter)]: name_filter is set when the topic is a generic one (e.g. 'Krimi am Samstag')
    and only items whose title contains the show name belong to the show."""
    names = show_names(show)
    norm_names = [_norm(n) for n in names]
    counts = Counter(it.topic for it in items if not should_skip_item(it))
    result: list[tuple[str, str | None]] = []
    for topic, _ in counts.most_common():
        nt = _norm(topic)
        if any(n and (n == nt or n in nt or (nt in n and len(nt) >= 4)) for n in norm_names):
            result.append((topic, None))
            continue
        for name in names:
            hits = sum(1 for it in items if it.topic == topic and name.casefold() in it.title.casefold())
            if hits >= 2:
                result.append((topic, name))
                break
    return result[:6]


def heuristic_candidates(topic: str, name_filter: str | None) -> list[Ruleset]:
    filters = [{"attribute": "title", "type": "Contains", "value": name_filter}] if name_filter else []
    prefix = rf"{regex.escape(name_filter)}\s*[-:]\s*" if name_filter else ""

    def rs(strategy: str, **kw: Any) -> Ruleset:
        return Ruleset(topic=topic, matching_strategy=strategy, filters=list(filters), **kw)

    title_rules = [
        [{"type": "regex", "field": "title", "pattern": rf"^{prefix}(.+?){_STRIP_PARENS}"}],
        [{"type": "regex", "field": "title", "pattern": rf"^(?:.*? - )?(.+?){_STRIP_PARENS}"}],
        [{"type": "regex", "field": "title", "pattern": rf"^(?:.*?: )?(.+?){_STRIP_PARENS}"}],
        [{"type": "regex", "field": "title", "pattern": r"^(.+?)(?: - .*)?$"}],
    ]
    out = [
        rs("SeasonAndEpisodeNumber", season_regex=r"S(\d+)\s*/\s*E\d+", episode_regex=r"S\d+\s*/\s*E(\d+)"),
        rs("SeasonAndEpisodeNumber", season_regex=r"S(\d+)\s*E\d+", episode_regex=r"S\d+\s*E(\d+)"),
        rs("SeasonAndEpisodeNumber", season_regex=r"Staffel\s*(\d+)", episode_regex=r"(?:Folge|Episode)\s*(\d+)"),
        rs("ByAbsoluteEpisodeNumber", episode_regex=r"(?:Folge|Episode)\s*(\d+)"),
        rs("ByAbsoluteEpisodeNumber", episode_regex=r"\((\d+)\)\s*$"),
        rs("ItemTitleEqualsAirdate", title_regex_rules=[{"type": "regex", "field": "title", "pattern": r"(\d{2}\.\d{2}\.\d{4})"}]),
        rs("ItemTitleEqualsAirdate", title_regex_rules=[{"type": "regex", "field": "title", "pattern": r"(\d{1,2}\. \p{L}+ \d{4})"}]),
        rs("ItemTitleEqualsAirdate", title_regex_rules=[{"type": "regex", "field": "timestamp_date", "pattern": r"(\d{8})"}]),
    ]
    for rules in title_rules:
        out.append(rs("ItemTitleExact", title_regex_rules=rules))
    for rules in title_rules[:2]:
        out.append(rs("ItemTitleIncludes", title_regex_rules=rules))
    return out


# ---------------- LLM ----------------

SYSTEM_PROMPT = f"""Du erstellst Rulesets für MediathekArr. Ein Ruleset ordnet Einträge aus MediathekView
(deutsche TV-Mediatheken) den Folgen einer Serie bei TheTVDB zu. Antworte ausschließlich mit JSON.

Ein Ruleset hat diese Felder:
- topic: exakter MediathekView-Thema-String (aus den Daten übernehmen). Mehrere mit "|" trennen.
- matchingStrategy: eine von {STRATEGIES}
  * SeasonAndEpisodeNumber: seasonRegex und episodeRegex holen jeweils mit der ersten Capture-Group
    Staffel- und Folgennummer aus dem Titel. Statt Regex geht auch fix "S1" bzw. "E5".
  * ByAbsoluteEpisodeNumber: episodeRegex holt eine Nummer; Treffer ist die erste TVDB-Folge mit dieser
    Folgennummer (nur sinnvoll bei Serien mit einer Staffel).
  * ItemTitleExact: titleRegexRules bauen einen Text, der exakt (normalisiert) dem TVDB-Folgentitel entspricht.
  * ItemTitleIncludes: wie oben, aber der TVDB-Folgentitel muss den Text nur enthalten.
  * ItemTitleEqualsAirdate: titleRegexRules bauen ein Datum (dd.MM.yyyy, yyyy-MM-dd, yyyyMMdd oder
    "7. Juni 2024"), das dem TVDB-Ausstrahlungsdatum entspricht.
- titleRegexRules: Liste aus {{"type":"regex","field":<Feld>,"pattern":<Regex>}} (letzte Capture-Group wird
  angehängt; schlägt sie fehl, gilt der Eintrag als nicht zugeordnet) und {{"type":"static","value":<Text>}}.
  Bei SeasonAndEpisodeNumber/ByAbsoluteEpisodeNumber optional: dann wirken die Regexe auf den gebauten Text.
- filters: Liste aus {{"attribute":<Feld>,"type":<{FILTER_TYPES}>,"value":<Wert>}}. Alle müssen passen.
  GreaterThan/LowerThan vergleichen Sekunden mit value*60 (value in Minuten, z. B. für duration).
- Felder: {FIELDS}
Regexe sind .NET-Syntax. Einträge mit Audiodeskription, Hörfassung, Gebärdensprache, Trailer werden ohnehin ignoriert.

Antwortformat: {{"rulesets": [{{"topic": ..., "matchingStrategy": ..., "seasonRegex": ..., "episodeRegex": ...,
"titleRegexRules": [...], "filters": [...]}}], "reasoning": "kurz"}}"""


def _sample_items(items: list[Item], topic: str, limit: int = 40) -> list[str]:
    rows = [it for it in items if it.topic == topic and not should_skip_item(it)]
    out = []
    for it in rows[:limit]:
        when = datetime.fromtimestamp(it.timestamp, tz=timezone.utc).strftime("%Y-%m-%d") if it.timestamp else "?"
        out.append(f"{it.channel} | {when} | {it.duration // 60} min | {it.title}")
    return out


def _sample_episodes(show: Show, limit: int = 80) -> list[str]:
    eps = [e for e in show.episodes if e.season > 0] or show.episodes
    eps = sorted(eps, key=lambda e: (e.aired or datetime.min.date()), reverse=True)[:limit]
    return [f"S{e.season:02d}E{e.number:02d} | {e.aired or '?'} | {e.name or ''}" for e in eps]


def build_llm_prompt(show: Show, items: list[Item], topics: list[tuple[str, str | None]]) -> str:
    topic_counts = Counter(it.topic for it in items if not should_skip_item(it))
    lines = [f"Serie: {show.german_name} (Originaltitel: {show.name}, TVDB {show.tvdb_id})",
             f"Staffeln bei TVDB: {sorted({e.season for e in show.episodes})}", "",
             "MediathekView-Themen (Anzahl Einträge):"]
    lines += [f"- {t!r}: {topic_counts[t]}" for t, _ in topics] or ["- (keine passenden Themen gefunden)"]
    for t, _ in topics[:3]:
        lines += ["", f"Einträge im Thema {t!r} (Sender | Datum | Dauer | Titel):", *_sample_items(items, t)]
    lines += ["", "TVDB-Folgen (neueste zuerst, Folge | Ausstrahlung | Titel):", *_sample_episodes(show)]
    return "\n".join(lines)


def feedback_for(evals: list[Evaluation], errors: list[str]) -> str:
    lines = ["Ergebnis der Prüfung mit dem echten MediathekArr-Matcher:"]
    lines += [f"- Fehler: {e}" for e in errors]
    for ev in evals:
        lines.append(f"- {ev.ruleset.matching_strategy} für {ev.ruleset.topic!r}: {ev.summary()}")
        if ev.unmatched_samples:
            lines.append("  nicht zugeordnet: " + "; ".join(ev.unmatched_samples[:6]))
        if ev.implausible_samples:
            lines.append("  vermutlich falsch zugeordnet: " + "; ".join(ev.implausible_samples[:4]))
    lines.append("Verbessere die Rulesets. Ziel: fast alle Einträge eines Themas korrekt zuordnen.")
    return "\n".join(lines)


# ---------------- main entry ----------------

def generate(show: Show, items: list[Item], llm: LLMClient | None, min_rate: float = 0.8,
             max_attempts: int = 3, force_llm: bool = False) -> GenerationResult:
    topics = candidate_topics(items, show)
    tried = 0
    best_by_topic: dict[str, Evaluation] = {}

    def consider(ev: Evaluation) -> None:
        cur = best_by_topic.get(ev.ruleset.topic)
        if cur is None or ev.score() > cur.score():
            best_by_topic[ev.ruleset.topic] = ev

    if not force_llm:
        for topic, name_filter in topics:
            for cand in heuristic_candidates(topic, name_filter):
                tried += 1
                consider(evaluate(cand, items, show))

    def accepted() -> list[Evaluation]:
        return [ev for ev in best_by_topic.values() if ev.passes(min_rate)]

    used_llm = False
    if (force_llm or not accepted()) and llm is not None and llm.enabled and items:
        used_llm = True
        messages = [{"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": build_llm_prompt(show, items, topics)}]
        for attempt in range(max_attempts):
            errors: list[str] = []
            evals: list[Evaluation] = []
            try:
                answer = llm.chat_json(messages)
            except Exception as ex:  # network, JSON, ...
                log.warning("LLM attempt %d failed: %s", attempt + 1, ex)
                messages.append({"role": "user", "content": f"Antwort war kein gültiges JSON ({ex}). Bitte nur JSON."})
                continue
            messages.append({"role": "assistant", "content": json.dumps(answer, ensure_ascii=False)})
            for raw in answer.get("rulesets") or []:
                try:
                    rs = ruleset_from_payload(raw)
                    if rs.matching_strategy not in STRATEGIES:
                        raise ValueError(f"unbekannte matchingStrategy {rs.matching_strategy!r}")
                    for pattern in [rs.season_regex, rs.episode_regex,
                                    *[r.get("pattern") for r in rs.title_regex_rules]]:
                        if pattern and not regex.fullmatch(r"[SE]\d{1,4}", pattern):
                            regex.compile(pattern)
                except Exception as ex:
                    errors.append(f"{raw!r}: {ex}")
                    continue
                tried += 1
                ev = evaluate(rs, items, show)
                evals.append(ev)
                consider(ev)
            if accepted() and not any(not ev.passes(min_rate) for ev in evals):
                break
            messages.append({"role": "user", "content": feedback_for(evals, errors)})

    result = sorted(accepted(), key=lambda ev: -ev.matched)
    for i, ev in enumerate(result):
        ev.ruleset.priority = i
    if result:
        msg = "; ".join(f"{ev.ruleset.topic}: {ev.ruleset.matching_strategy}, {ev.summary()}" for ev in result)
    elif not items:
        msg = "Keine Einträge in MediathekView gefunden"
    elif not topics:
        msg = "Kein passendes MediathekView-Thema gefunden"
    else:
        best = max(best_by_topic.values(), key=lambda ev: ev.score(), default=None)
        msg = "Kein Ruleset erreicht die Mindestquote" + (f" (bestes: {best.ruleset.matching_strategy}, {best.summary()})" if best else "")
        if llm is None or not llm.enabled:
            msg += "; keine KI konfiguriert"
    return GenerationResult(show.tvdb_id, show.german_name or show.name, result, tried, used_llm, msg)
