# MediathekArr Rulesets

A self-hostable ruleset service for MediathekArr. It does two things:

1. **Hosting:** it serves rulesets in exactly the format of `mediathekarr.pcjones.de/metadata/api/rulesets.php`,
   so any MediathekArr instance can use it via `MEDIATHEKARR_RULESETS_URL`.
2. **Generating:** it creates rulesets automatically. For each show it loads the TVDB episode list and the
   MediathekView entries MediathekArr would see, tries built-in patterns and, if none fits, asks a **local LLM**
   (any OpenAI-compatible API: Ollama, LM Studio, llama.cpp server, vLLM, ...). Every candidate is validated with a
   Python port of MediathekArr's matcher before it is stored, so only rulesets that map entries to the right
   episodes are published. Failed validations are fed back to the LLM for another attempt.

On first start (empty database) it imports the existing rulesets from pcjones.de and from
[Rundfunkarr](https://github.com/rundfunkarr/rundfunkarr) (same format, MIT licensed), so you only extend them.

## Run

Quickest way on any Docker host (e.g. a Proxmox LXC), using only this folder:

```sh
cp .env.example .env   # set at least RULESETS_API_KEY, plus TVDB/LLM/Sonarr as needed
docker compose up -d
```

Data lands in `./data`. Or add it to an existing compose file:

```yaml
services:
  mediathekarr-rulesets:
    image: ghcr.io/martinriedel/mediathekarr-rulesets:latest  # or build: ./MediathekArrRulesets
    restart: unless-stopped
    environment:
      - RULESETS_API_KEY=change-me          # required for any write (API, generator, import)
      - TVDB_API_KEY=your-tvdb-key          # optional; enables name search and direct TVDB access
      - LLM_BASE_URL=http://ollama:11434/v1 # optional; your local LLM
      - LLM_MODEL=qwen2.5:14b
      - SONARR_URL=http://sonarr:8989       # optional; generate for your whole Sonarr library
      - SONARR_API_KEY=...
      - GENERATE_INTERVAL_HOURS=24          # optional; 0 = only on demand
    volumes:
      - /docker/appdata/mediathekarr-rulesets:/data
    ports:
      - "5008:5008"
```

Then set `MEDIATHEKARR_RULESETS_URL=http://<host>:5008/api/v1/rulesets` on MediathekArr. With `TVDB_API_KEY` set, the
service also serves show data (`get_show.php`) from TVDB with your key, so MediathekArr no longer needs
mediathekarr.pcjones.de at all: also set `MEDIATHEKARR_API_BASE_URL=http://<host>:5008/api/v1`.
The overview page is at `http://<host>:5008/`.

## Generate locally, host elsewhere

Run the generator next to your LLM and let it upload to a public instance:

```sh
export GENERATOR_TARGET_URL=https://rulesets.example.org GENERATOR_TARGET_API_KEY=change-me
export LLM_BASE_URL=http://localhost:11434/v1 LLM_MODEL=qwen2.5:14b TVDB_API_KEY=...
python -m rulesets_service generate --sonarr
python -m rulesets_service generate --name "Hubert ohne Staller" --tvdb-id 262013
python -m rulesets_service generate --tvdb-id 262013 --force --dry-run   # show the result only
```

Shows that already have rulesets are skipped unless `--force`; shows that failed are retried after
`RETRY_FAILED_AFTER_HOURS`. Generated rulesets replace earlier generated ones for that show; imported and manually
edited rulesets are never touched.

## Discovery (fully automatic)

With `TVDB_API_KEY` set, the service finds new shows on its own, no show list needed:

1. Every `DISCOVER_INTERVAL_HOURS` (default 24) it walks the whole MediathekView catalogue, channel by channel
   (`DISCOVER_CHANNELS`), newest first, page by page with a short pause, and only asks for entries of at least
   `DISCOVER_MIN_MINUTES`. `DISCOVER_ITEMS` limits the walk to the newest N entries per channel (0 = everything).
2. It counts entries per topic and keeps topics that look like a series (at least `DISCOVER_MIN_ITEMS` entries) and
   are not covered by any ruleset yet, biggest first.
3. It searches each topic on TVDB and takes the hit whose name (any language or alias) is close enough
   (`DISCOVER_MIN_NAME_SCORE`).
4. The generator builds and validates a ruleset for that show as usual (patterns first, then the LLM).

At most `DISCOVER_MAX_TOPICS` new topics are handled per run, so the backlog is worked off over a few days. Topics
without a working ruleset are retried after `RETRY_FAILED_AFTER_HOURS`, topics without a TVDB match after
`DISCOVER_RETRY_NO_MATCH_DAYS`. Results show up under "Entdeckte Themen" on the overview page. Start a run
by hand with the button there or `python -m rulesets_service discover [--dry-run]`.

**Full scan:** "Kompletter Scan" on the overview page (`POST /api/discover?full=true`, CLI `discover --full`) walks the
whole catalogue regardless of `DISCOVER_ITEMS`, handles every candidate topic without the `DISCOVER_MAX_TOPICS` limit
and retries topics that failed or had no TVDB match before. It can take hours. Only one discovery run happens at a time. Set `DISCOVER_INTERVAL_HOURS=0`
to turn the schedule off. Sonarr and `--tvdb-id`/`--name` remain available for shows discovery does not catch.

## LLM

One integration is built in: the OpenAI-compatible chat API (`/v1/chat/completions`). Practically every local
runtime offers it, so you only set three variables:

| Runtime | `LLM_BASE_URL` | `LLM_MODEL` | `LLM_API_KEY` |
|---|---|---|---|
| Ollama | `http://<host>:11434/v1` | e.g. `qwen2.5:14b` | empty |
| LM Studio | `http://<host>:1234/v1` | name of the loaded model | empty |
| llama.cpp server | `http://<host>:8080/v1` | anything | empty |
| vLLM, LocalAI, text-generation-webui | `http://<host>:<port>/v1` | model name | depends on setup |
| Cloud (OpenAI, OpenRouter, ...) | provider URL | model name | your key |

How it is used:

1. The LLM is only asked when the built-in patterns find no ruleset that passes validation (`--force-llm` or
   `forceLlm` skips the patterns).
2. It gets the MediathekView titles (per topic, with channel, date and duration) and the TVDB episode list, and has
   to answer with rulesets as JSON.
3. Each proposal is validated with the MediathekArr matcher. If it fails, the LLM gets the unmatched and wrongly
   matched entries back and tries again, up to `LLM_MAX_ATTEMPTS` (default 3).

Without `LLM_BASE_URL` the service still works, using only the built-in patterns. A model that handles JSON and
regular expressions well should be enough (an estimate, around 7–14B parameters); no specific model has been
tested yet. The request asks for `response_format: json_object` and falls back to plain JSON prompting if the
server rejects it; answers from reasoning models (`<think>...</think>`) are handled.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `RULESETS_DB_PATH` | `/data/rulesets.sqlite` | SQLite database |
| `RULESETS_API_KEY` | – | Key for writes (`X-Api-Key` or `Authorization: Bearer`). Without it the service is read-only |
| `UPSTREAM_RULESETS_URLS` | pcjones.de `rulesets.php`, Rundfunkarr `data/rulesets.json` | Comma-separated sources for the import (paginated API or plain JSON list). Same rulesets from several sources are stored once |
| `IMPORT_UPSTREAM_ON_START` | `true` | Import upstream rulesets when the database is empty |
| `TVDB_API_KEY`, `TVDB_PIN` | – | TVDB v4 access. Without a key, show data comes from `MEDIATHEKARR_API_BASE_URL` (by TVDB id only) |
| `MEDIATHEKARR_API_BASE_URL` | `https://mediathekarr.pcjones.de/api/v1` | Fallback show data source |
| `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY` | – | OpenAI-compatible endpoint of your LLM. Without it only built-in patterns are used |
| `LLM_MAX_ATTEMPTS` | `3` | LLM rounds per show |
| `MIN_MATCH_RATE` | `0.8` | Share of a topic's entries a ruleset must map to be accepted |
| `SONARR_URL`, `SONARR_API_KEY` | – | Show list for `--sonarr` and the schedule |
| `GENERATE_INTERVAL_HOURS` | `0` | Run the generator for all Sonarr shows periodically |
| `DISCOVER_INTERVAL_HOURS` | `24` | Discovery schedule (needs `TVDB_API_KEY`; `0` = off) |
| `DISCOVER_ITEMS`, `DISCOVER_MIN_ITEMS`, `DISCOVER_MIN_MINUTES` | `0`, `3`, `10` | How many newest entries to scan (0 = whole catalogue), and what counts as a series topic |
| `DISCOVER_MAX_TOPICS`, `DISCOVER_MIN_NAME_SCORE` | `200`, `0.85` | New topics per run; how close the TVDB name must be (0–1) |
| `DISCOVER_CHANNELS` | ARD, ZDF, 3Sat, ARTE.DE, the ARD regional channels, KiKA, ... | Channels discovery walks, comma-separated |
| `DISCOVER_RETRY_NO_MATCH_DAYS` | `30` | When to search TVDB again for a topic that had no match |
| `GENERATOR_TARGET_URL`, `GENERATOR_TARGET_API_KEY` | – | Upload results to another instance instead of the local database |

## API

| Method | Path | |
|---|---|---|
| GET | `/api/v1/rulesets?page=N` (alias `/metadata/api/rulesets.php`) | Public, MediathekArr format |
| GET | `/api/v1/get_show.php?tvdbid=N` | Public, show data in MediathekArr format (needs `TVDB_API_KEY`) |
| GET | `/api/media`, `/api/rulesets?tvdbId=&mediaId=`, `/api/generation-log`, `/api/export` | Read |
| POST/PUT/DELETE | `/api/media[/{id}]`, `/api/rulesets[/{id}]` | Edit by hand (key) |
| POST | `/api/discover[?maxTopics=&full=true]` | Start a discovery run in the background (key) |
| GET | `/api/discovery-log` | Topics discovery has looked at |
| POST | `/api/generate` `{tvdbIds, names, sonarr, force, forceLlm}` | Start generator in the background (key) |
| PUT | `/api/generated/{tvdbId}` `{name, rulesets, matchRate}` | Upload from a remote generator (key) |
| POST | `/api/import/upstream[?url=]`, `/api/import` | Import from the upstream sources / from an export (key) |

## Develop

```sh
pip install -r requirements-dev.txt
python -m pytest
```

`rulesets_service/matcher.py` mirrors `MediathekArrServer/Services/MediathekSearchService.cs`; keep both in sync.
