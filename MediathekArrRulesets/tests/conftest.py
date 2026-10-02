import os
import sys
from datetime import date, datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("RULESETS_DB_PATH", ":memory:")
os.environ.setdefault("RULESETS_API_KEY", "secret")
os.environ.setdefault("IMPORT_UPSTREAM_ON_START", "false")

from rulesets_service.matcher import Episode, Item, Show  # noqa: E402


def ts(d: str) -> int:
    return int(datetime.fromisoformat(d).replace(tzinfo=timezone.utc).timestamp())


@pytest.fixture
def show() -> Show:
    names = ["Der Anfang", "Mord im Biergarten", "Die Rückkehr", "Schwarzer Peter", "Das Ende",
             "Neue Wege", "Alte Freunde", "Blaues Wunder"]
    eps = []
    for i, n in enumerate(names):
        season, number = (1, i + 1) if i < 4 else (2, i - 3)
        eps.append(Episode(name=n, aired=date(2024, 1, 1) + timedelta(days=i * 7), runtime=45, season=season, number=number))
    return Show(tvdb_id=4711, name="Testserie", german_name="Testserie", episodes=eps)


def make_items(show: Show, title_fmt: str, topic: str = "Testserie") -> list[Item]:
    items = []
    for e in show.episodes:
        aired = e.aired.isoformat()
        items.append(Item(channel="ZDF", topic=topic, title=title_fmt.format(name=e.name, s=e.season, e=e.number, d=e.aired.strftime("%d.%m.%Y")),
                          timestamp=ts(aired), duration=45 * 60, url_video=f"https://x/{topic}/{e.season}/{e.number}.mp4"))
    # noise MediathekArr skips anyway
    items.append(Item(channel="ZDF", topic=topic, title="Testserie - Trailer", timestamp=ts("2024-01-01"), duration=60, url_video="https://x/t.mp4"))
    return items
