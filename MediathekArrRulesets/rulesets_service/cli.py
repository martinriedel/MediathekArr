"""Command line: `python -m rulesets_service <command>`."""
from __future__ import annotations

import argparse
import json
import logging
import sys

from .config import Settings
from .db import Database
from .runner import Runner
from .sources import fetch_upstream_rulesets


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="rulesets_service", description="MediathekArr ruleset service")
    sub = p.add_subparsers(dest="cmd", required=True)

    serve = sub.add_parser("serve", help="HTTP-Dienst starten")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=5008)

    imp = sub.add_parser("import-upstream", help="Rulesets aus UPSTREAM_RULESETS_URLS (oder --url) importieren")
    imp.add_argument("--url")

    gen = sub.add_parser("generate", help="Rulesets automatisch erzeugen")
    gen.add_argument("--tvdb-id", type=int, action="append", default=[], help="mehrfach möglich")
    gen.add_argument("--name", action="append", default=[], help="Serienname, per TVDB gesucht (braucht TVDB_API_KEY)")
    gen.add_argument("--sonarr", action="store_true", help="alle Serien aus Sonarr")
    gen.add_argument("--force", action="store_true", help="auch Serien, die schon Rulesets haben")
    gen.add_argument("--force-llm", action="store_true", help="eingebaute Muster überspringen, direkt die KI fragen")
    gen.add_argument("--dry-run", action="store_true", help="nur anzeigen, nichts speichern")

    disc = sub.add_parser("discover", help="neue Serien in MediathekView finden und Rulesets erzeugen (braucht TVDB_API_KEY)")
    disc.add_argument("--max-topics", type=int, help="höchstens so viele neue Themen pro Lauf")
    disc.add_argument("--dry-run", action="store_true", help="nur anzeigen, nichts speichern")

    sub.add_parser("export", help="alle Daten als JSON ausgeben")

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()

    if args.cmd == "serve":
        import uvicorn
        uvicorn.run("rulesets_service.main:app", host=args.host, port=args.port)
        return 0

    db = Database(settings.db_path)

    if args.cmd == "import-upstream":
        for url in [args.url] if args.url else settings.upstream_urls:
            entries = fetch_upstream_rulesets(url)
            print(json.dumps({"url": url, "fetched": len(entries), **db.upsert_upstream(entries, source=url)}))
        return 0

    if args.cmd == "export":
        json.dump(db.export_all(), sys.stdout, ensure_ascii=False, indent=2)
        return 0

    runner = Runner(settings, db)
    if args.cmd == "discover":
        from .discovery import Discovery
        for r in Discovery(settings, db, runner).run(args.max_topics, dry_run=args.dry_run):
            print(json.dumps(r, ensure_ascii=False))
        return 0

    ids = list(args.tvdb_id)
    if args.name:
        ids += runner.search_ids(args.name)
    if args.sonarr:
        ids += runner.sonarr_ids()
    ids = list(dict.fromkeys(ids))
    if not ids:
        p.error("keine Serien angegeben (--tvdb-id, --name oder --sonarr)")
    results = runner.run_many(ids, force=args.force, force_llm=args.force_llm, dry_run=args.dry_run)
    for r in results:
        print(json.dumps(r, ensure_ascii=False))
    ok = sum(1 for r in results if r["status"] == "ok")
    print(f"{ok}/{len(results)} Serien mit neuem Ruleset", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
