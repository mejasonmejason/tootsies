# Handoff: Perplexity Sonar API — search-control parameters not being used

> **RESOLVED.** Per-surface search controls are now set in `utils/perplexity.py`
> (`_SEARCH_CONFIG`): every surface gets `search_context_size="medium"` and the
> trend/news surfaces get a recency window (music/discourse `week`, recap/chimein
> `day`; `ask` stays unfiltered so evergreen fact-verification pages survive).
> `search()` takes `search_context_size` / `recency` overrides for the eval
> harness at `scripts/eval_perplexity_params.py`. `high` context was deliberately
> NOT shipped by default (cost is cross-surface) — flip it via the eval if a
> surface still hedges. The rest of this doc is the original investigation.

## TL;DR
`utils/perplexity.py` calls Perplexity Sonar with **only `model` + `messages`**, so it inherits every default — including `search_context_size="low"` (shallowest retrieval). Result: low-signal, evergreen filler. Symptom (found while evaluating music posts): every Perplexity response opens with *"I can't verify live trends — results are mostly YouTube mixes / playlist pages."* We were encoding intent as **prose in the query string** ("last few hours", "check Twitter/X") and assuming the API acts on it; those are actually structured **API parameters** we never set. Fix = set the search-control params (per-surface), eval before/after, ship as its own PR.

## Repo context
Tootsies = Discord bot ("Toots"). `utils/perplexity.py` wraps Perplexity Sonar to inject real-time web context into 5 surfaces (ask / discourse / recap / chimein / music). Python 3.11, asyncpg, aiohttp. CI gates on `ruff check .` && `mypy .` && `pytest` (all three must pass). Never push to `main` — branch + PR.

## Root cause (confirmed: docs + live A/B)
Payload at `utils/perplexity.py:74-79`:
```python
payload = {"model": _MODEL, "messages": [{"role": "user", "content": query}]}
```
No search params → all defaults inherited. Biggest culprit is `search_context_size="low"`.

## Parameters available (not used)
Docs: https://docs.perplexity.ai/api-reference/chat-completions-post and https://docs.perplexity.ai/docs/sonar/filters

| Parameter | Current (default) | Consider | Notes |
|---|---|---|---|
| `web_search_options.search_context_size` | **`"low"`** | `"medium"`/`"high"` | **Likely dominant fix.** Low = shallow retrieval → "can't verify" filler. Cost: sonar ~$5/1k req (low) → ~$12/1k (high). |
| `search_recency_filter` | none | `"week"` (music) / `"day"` (news/chimein) | Values: `hour/day/week/month/year`. **Cannot combine with date filters.** |
| `search_domain_filter` | none | allowlist Billboard/Pitchfork; or denylist `-youtube.com` | Max 20 domains. Allowlist (no prefix) OR denylist (`-` prefix), not both. |
| `model` | `sonar` | maybe `sonar-pro` | Higher quality + cost. |
| `search_mode` | `web` | per-surface | `web`/`academic`/`sec`. Date filters silently ignored in `academic`. |

## Key design decision: make it per-surface
- **music / discourse**: `week` recency, `medium`+ context
- **chimein / recap / ask** (breaking/news/sports): `day` or `hour` recency

Note: the query text in `build_search_query` already says "last few hours" for some surfaces — too tight for music, and prose ≠ parameter regardless.

## Live A/B already run (prod key, R&B discourse query)
- **No params** (current): 6 sources, evergreen names (Coco Jones, Ella Mai…)
- **`+search_recency_filter="week"`**: 4 sources, fresh this-week drops (Chxrry, Syd, Young M.A.…)

Hedging was *intermittent* across runs → recency alone isn't a guaranteed fix; docs point to `search_context_size="low"` as the bigger lever. **Test both.**

## Files & exact references
- `utils/perplexity.py:31-32` — `_API_URL`, `_MODEL = "sonar"`
- `utils/perplexity.py:62` — `async def search(self, query, *, purpose=...)`; payload at **lines 74-79** ← the thing to change
- `utils/perplexity.py:243` — `build_search_query(user_input, *, surface, category, channel_name, channel_topic)`; surface branches: **257 (ask), 273 (discourse), 289 (recap), 298 (chimein)**
- `utils/perplexity.py:159` `_SOURCES`, `:164` `_CATEGORY_QUERIES`, `_DEFAULT_TRENDING`
- 5 callers (each passes `purpose=`): `cogs/ask.py:198`, `cogs/discourse.py:258`, `cogs/chimein.py:358`, `cogs/recap.py:142`, `cogs/music.py:185`
- Events: each call emits `pplx_<purpose>` (`pplx_ask/discourse/recap/chimein`; music uses `purpose="music"`). See CLAUDE.md event table.

## Suggested approach
1. Add search params to the payload in `search()` — per-surface config (recency + context_size, maybe domains) threaded via a new arg or a `purpose→config` map.
2. **Cost is cross-surface (5 callers)** — `search_context_size` bump ~doubles per-call cost. Get user sign-off before going `high`.
3. **Eval before/after**: measure "hedging rate" (responses containing "can't verify"/"cannot"/"do not have") across surfaces × {low,medium,high} × {recency on/off}. Copy the pattern in `scripts/eval_music_post.py`.
4. Its own PR (NOT folded into music PR #155).
5. Gotcha: `search_recency_filter` can't combine with `search_after/before_date_filter`.

## Testing live (key is on Railway, not in session env)
Pull `PERPLEXITY_API_KEY` via Railway GraphQL (pattern in CLAUDE.md "Debugging Railway deploys"):
- project `02da7404-8f48-401e-b960-ca4da938e498`, env `c6260f06-4fa6-4d59-a0de-dd284f59c815` (production), service `$RAILWAY_SERVICE_ID`
- query `variables(projectId, environmentId, serviceId)` → `PERPLEXITY_API_KEY`

## Checks before commit
`ruff check .` && `mypy .` && `pytest`. No em dashes in string literals (enforced: `tests/test_persona.py::test_no_em_dashes_anywhere_in_repo`). Branch off `main`, open PR.

## OUT OF SCOPE (separate follow-up, don't conflate)
iTunes reference verification: `utils/apple_music.py` appends whatever iTunes returns for a fuzzy query with **zero** check that resolved artist/title matches what the model named (wrong-track false-positive risk on the links-only music channel). Separate PR on the music side.
