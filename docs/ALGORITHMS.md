# Tootsies algorithms & heuristics

How `/ask`, `/recap`, `/discourse`, the scheduler, and `/order` pre-flight actually work, plus what to tune when behavior feels off.

Each section follows the same shape:

1. **Flow**: numbered steps in execution order with code anchors.
2. **Tunable knobs**: constants and config you can edit to shift behavior.
3. **Cost notes**: Claude API + token implications.

If you're trying to fix "Toots feels off when X", find the command, scan the knobs.

---

## Scheduled cadence per mood

The discourse `mood` setting (set in `/menu`) is the single dial that controls every periodic Toots surface. Manual surfaces (`/ask`, `/recap`, `/discourse category:`, `/order`, mentions) are unaffected by mood, they only see the per-user / per-server rate limits in [`utils/rate_limits.py`](../utils/rate_limits.py).

| Surface | `off` | `chill` | `yaps` |
|---|---|---|---|
| [`/discourse` scheduled posts](../cogs/discourse.py) | silent | 2/day at **12pm + 7pm ET** | 5/day at **9am, 12pm, 3pm, 6pm, 10pm ET** |
| [Chime-in](../cogs/chimein.py) | silent | up to **10/day**, **40 min** cooldown, score **>= 0.8** | **uncapped**, **20 min** cooldown, score **>= 0.6** |
| Hours window (both) | n/a | 9am to 2am ET | 9am to 2am ET |
| Tick frequency | n/a | scheduler: 1/min, chime-in: 1/min | scheduler: 1/min, chime-in: 1/min |

Mood-independent periodics: the DB pruner runs once every 24h ([`bot.py`](../bot.py)).

---

## /ask + @Toots mentions

Same backend, same rate limit. The mention handler in [cogs/ask.py](../cogs/ask.py) is a thin Discord-event wrapper around the same `_answer()` method.

### Flow

1. **Gate**: guild must be configured (post-`/menu`), user under the per-user daily cap.
2. **Defer**: slash command shows the "Toots is thinking…" indicator.
3. **Gather channel context**: pull the last 30 messages from this channel via [`recent_messages()`](../utils/feeds.py). Bot/webhook posts are filtered out by default (we want what humans are saying).
4. **Format context**: render via [`format_for_prompt()`](../utils/feeds.py). Each message becomes one line: `display_name: content [media-labels]`. Embed text from auto-unfurled URLs (X posts, articles) is inlined as `[embed: title / description (url)]`.
5. **Harvest images**: [`recent_image_urls()`](../utils/feeds.py) walks the messages newest-first and pulls up to **8 image URLs** (from attachments OR Tenor/GIPHY embed previews) under the 5 MB vision cap.
6. **Build the Claude call**.
   - Model: **Haiku 4.5** (fast, cheap).
   - System prompt: [persona.system_prompt()](../persona.py) = constitution + persona + voice examples. Cached with `cache_control: ephemeral` so repeat calls hit the prompt cache (~1 k tokens saved each).
   - System extra: TASK instruction + source-trust hierarchy (web > Toots's taste > channel chatter, never quoted) + format rules (restate question, ~140-char answer cap).
   - User message: `[ctx, current time...]\n\n{question}\n\nRecent channel chatter: ...`
   - Vision blocks: up to 8 images appended as `{"type": "image", "source": {"type": "url", "url": ...}}`.
   - Tools: `web_search_20250305`, always available, Claude decides when to invoke.
7. **Emit `claude_api` event** with model/purpose/tokens/latency.
8. **Consume rate-limit slot**: increments only on a successful answer (failures don't burn slots).
9. **Send reply** as the slash command followup or message reply for mentions.

### Tunable knobs

| What | Where | Current | Effect of changing |
|---|---|---|---|
| Per-user daily cap | [`utils/rate_limits.DEFAULT_PER_USER_DAILY`](../utils/rate_limits.py) | 20 | Higher = more daily usage allowed; `/menu` setting overrides per-guild |
| Channel context size | [`cogs/ask.py:_answer()`](../cogs/ask.py) `recent_messages(limit=30)` | 30 messages | Higher = richer context, more input tokens |
| Vision images per call | [`cogs/ask.py:_answer()`](../cogs/ask.py) `recent_image_urls(limit=8)` | 8 | Higher = better visual context, more cost (~85 tokens fixed + variable per image) |
| Vision hard cap | [`claude_client._call`](../claude_client.py) image_urls slice | 10 | Final safety cap regardless of caller |
| Image size cap | [`utils/feeds._VISION_MAX_BYTES`](../utils/feeds.py) | 5 MB | Anthropic's vision ceiling; raising won't help (API rejects) |
| Mention auto-reply on Discord-reply | [`cogs/ask.py` mention handler](../cogs/ask.py) | Ignores auto-mention, requires explicit @Toots | Make stricter if mentions get noisy |
| Web search availability | [`cogs/ask.py:_answer()`](../cogs/ask.py) `use_web=True` | Always on | Disabling cuts cost but kills accuracy on factual questions |
| System prompt cache | [`claude_client._call`](../claude_client.py) `cache_control` | ephemeral | Remove only if you want to A/B-test prompt changes faster |

### Cost notes

- Typical call: ~1.5 k input tokens (persona cached) + ~500 output tokens + 0-8 images. Haiku pricing makes this fractions of a cent.
- The web_search tool is metered separately by Anthropic.
- Channel context grows linearly with `limit=30`; bump cautiously.

---

## /recap period:<1h | 1d | today>

### Flow

1. **Gate**: configured guild, user under daily cap.
2. **Resolve window**: `_period_to_window()` in [cogs/recap.py](../cogs/recap.py) maps the choice to a `timedelta`:
   - `1h` → last 60 minutes
   - `1d` → rolling 24 hours
   - `today` → since midnight UTC (so at 2am UTC this is a 2-hour window)
3. **Read channel**: `recent_messages(limit=200, within=window, include_bots=True)`. `include_bots=True` is critical for feed channels and for /recap to see Toots's own scheduled posts as part of the room.
4. **Dead-channel check**: `is_channel_dead(messages)` returns True only when the list is literally empty. Anything else (even 1 short message) goes to Claude, who decides whether to quip about thin content.
5. **If dead**: emit `recap_deflected` event with the diagnostic (`no_permission` vs `no_messages`), post a one-line diagnostic to `#bot-logs` at full verbosity, return a `CHANNEL_DEAD` canned quip.
6. **Otherwise**: call [`claude_client.recap()`](../claude_client.py):
   - Model: **Haiku 4.5**.
   - System extra instructs: weight reactions, fold in real facts via web_search when the room references a specific game/release/news, use vision to name what's IN the meme when relevant.
   - Vision: up to 8 image URLs from recent messages.
   - Tools: `web_search_20250305`.
7. **Emit event, consume slot, reply.**

### Tunable knobs

| What | Where | Current | Effect |
|---|---|---|---|
| Per-user daily cap | shared with /ask via `DEFAULT_PER_USER_DAILY` | 20 | Same as /ask |
| Channel read limit | [`cogs/recap.py`](../cogs/recap.py) `recent_messages(limit=200, ...)` | 200 messages | Higher = more thorough recap, more input tokens |
| Dead-channel threshold | [`utils/feeds.is_channel_dead()`](../utils/feeds.py) | 0 (only literally empty) | Raise to require N+ messages (we trust Claude to handle thin content now) |
| Vision images for recap | [`cogs/recap.py`](../cogs/recap.py) `recent_image_urls(limit=8)` | 8 | Same trade as /ask |
| Period choices | [`cogs/recap.py:_period_to_window()`](../cogs/recap.py) | 1h / 1d / today | Add longer windows here (e.g. `3d`, `week`) if mods want |
| `today` timezone | UTC | UTC | Could be made per-guild via /menu; punted for now |

### Cost notes

- Larger context than /ask (200 msgs vs 30) but same Haiku pricing. Each /recap might run ~3-5 k input tokens.
- Web search may fire several times per recap if multiple topics need facts.

---

## /discourse (manual: `category:`)

### Flow

1. **Gate**: configured guild, under per-server daily cap (20/day shared across all users).
2. **Compose sources blob** in [cogs/discourse.py:`_compose()`](../cogs/discourse.py):
   - **(1) Feed channels** filtered by category, up to 5 feed channels, last 24h, 10 messages each, `include_bots=True`.
   - **(2) Current channel**: last 1 hour, 20 messages.
   - **(3) Recent discourse history**: last 72h for this category from `discourse_history` table, with timestamps. Used as anti-repeat context.
3. **Call [`claude_client.discourse()`](../claude_client.py)** with all sources concatenated:
   - Model: **Sonnet 4.6** (needs judgment, not just text completion).
   - System extra: instructs to pick the freshest threadworthy thing, bake current state into the post for future dedup (e.g. "lakers vs nuggets r2, series tied 1-1"), use web_search for verification.
   - `must_post=True` for manual invocations, even if recent topics cover the field, pick a different angle rather than skipping.
   - Tools: `web_search_20250305`.
4. **Handle response**: if Claude returns literal "EMPTY" (shouldn't with `must_post=True` but defense in depth), fall back to a `DISCOURSE_FALLBACK` canned quip and emit `discourse_fallback` event.
5. **Store the post in `discourse_history`** for future dedup. Stored value is the first 200 chars of the post (likely contains the state info Claude was instructed to bake in).
6. **Emit `claude_api` event, consume server slot, reply.**

### Tunable knobs

| What | Where | Current | Effect |
|---|---|---|---|
| Per-server daily cap | [`utils/rate_limits.DEFAULT_PER_SERVER_DAILY`](../utils/rate_limits.py) | 20 | /menu setting overrides per guild |
| Feed channels per call | [`cogs/discourse.py:_compose()`](../cogs/discourse.py) `feeds[:5]` | 5 | More = richer source pool, more tokens |
| Feed read window | same file | 24 hours | Shorten for tighter "what's fresh" feel |
| Feed messages per channel | same file | 10 | Wider sampling vs token cost |
| Current channel read | same file | 1 hour / 20 msgs | Adjust based on guild activity level |
| Dedup history window | [`db.recent_discourse()`](../db.py) | 72 hours | Plan documents this; shorten if topics evolve fast in your community |
| Categories | [`cogs/discourse.py` CATEGORIES](../cogs/discourse.py) | pop, sports, cinema, hiphop, nba, custom | Add new categories here + feed channels in `/menu → Feeds` |

---

## Discourse scheduler (`/discourse mood:<chill | yaps | off>`)

### Flow

`scheduler_tick` in [cogs/discourse.py](../cogs/discourse.py) is a `tasks.loop(minutes=1)`. Every minute:

1. **For each configured guild**, fetch its `discourse_schedule.mood`.
2. **If mood is `off`**: skip.
3. **Schedule lookup**: `chill` = [12:00 PT, 19:00 PT], `yaps` = [10:00, 14:00, 18:00, 22:00 PT].
4. **Slot calculation**: how many scheduled slots have elapsed today in PT? That's `expected`. How many have we actually posted in today's bucket? That's `state.posts_today`.
5. **If `posts_today >= expected`**: we're caught up, skip.
6. **Otherwise**: pull last 72h of cross-category discourse history.
7. **Call `claude_client.mood_post(recent_with_timestamps=...)`**: Haiku, no web_search, with explicit instructions to return literal "EMPTY" if all topics are stale repeats.
8. **Consume the slot** regardless, `record_schedule_post()` increments `posts_today`. This is intentional: if Claude returned EMPTY at 12:00 PT, retrying every minute would burn API calls. Skip cleanly, next attempt is the next scheduled slot.
9. **If Claude returned text**: post to the configured discourse channel, store in `discourse_history` under category `"scheduled"`.

### Tunable knobs

| What | Where | Current |
|---|---|---|
| Scheduled times | [`cogs/discourse.py` CHILL_TIMES / YAPS_TIMES](../cogs/discourse.py) | chill: 12pm/7pm PT; yaps: 9am/12pm/3pm/6pm/10pm ET |
| Tick frequency | `@tasks.loop(minutes=1)` | 1 min |
| Cross-category history depth | `recent_discourse_all(limit=20)` | 20 most recent posts |
| Model | `claude.mood_post()` uses HAIKU | Haiku 4.5 |
| Web search | NOT enabled (different from manual /discourse) | Off; flip to on if scheduled posts feel stale |

### When the bot is "talking too much" or "too quiet"

- Too quiet: switch mood `chill` → `yaps`, or add more scheduled times to `CHILL_TIMES`.
- Too talkative: switch to `chill` or `off`, or shrink `YAPS_TIMES`.
- Repetitive: shorten the dedup window in `discourse_history` (currently 72h).

---

## /order pre-flight

### Flow

In [`claude_client.preflight_order()`](../claude_client.py):

1. Call **Sonnet 4.6** with a structured instruction to classify the order into one of three buckets:
   - `ALLOW: <one-line summary>`, valid order, summary is what to build
   - `PLUMBING: <which protected path>`, would touch constitution/persona core/CI/Dockerfile/etc.
   - `REJECT: <reason>`, moderation, NSFW, incoherent, off-scope
2. Parse the first line of the response.
3. **Fails closed**: if Claude returns anything that doesn't start with one of those three words, return `("reject", "unparseable preflight response: ...")`.

The cog then branches on the verdict for the user-facing message (different deflection quip for `plumbing` vs `reject`) and the bot-logs post (🔧 vs 🚫).

### Tunable knobs

| What | Where | Current |
|---|---|---|
| Protected paths | system prompt inline in [`preflight_order()`](../claude_client.py) | constitution.py, persona.py core, .github/, Dockerfile, railway.toml, Procfile, db.py connection, bot.py boot, requirements.txt deletions |
| Exceptions | same prompt | voice library additions, new tables/cogs/deps, new optional env vars |
| Model | Sonnet 4.6 | Don't downgrade; preflight is the safety net. |
| Max tokens | 250 | Plenty for "ALLOW: …" verdict |

---

## Chime-in

Toots leans into the conversation when she has something real to say. No commands of its own, it rides on two settings already in `/menu`:
- **Listen channel:** the configured `discourse_channel`. Whatever room is your "chatter" / "general" channel is the one Toots will listen in on.
- **On/off + cadence:** the discourse mood. `mood=off` silences chime-in; `chill` makes her reserved (10/day, 40 min cooldown, 0.8 threshold); `yaps` makes her chatty (uncapped, 20 min cooldown, 0.6 threshold).

### Design intent

Chime-in (and `/discourse`) is meant to get the ROOM talking to each other, not to start a back-and-forth between one user and Toots. Both prompts ([`chimein_post`](../claude_client.py), [`discourse`](../claude_client.py), [`mood_post`](../claude_client.py)) explicitly tell the model: drop the take or the prompt, don't ask questions aimed at you, don't tee yourself up for a reply. Toots is the bartender setting up the room's next argument and walking off, not a participant.

### Flow

In [`cogs/chimein.py`](../cogs/chimein.py):

1. **on_message listener** appends every qualifying human message (non-bot, non-empty or has attachments/embeds) posted in the guild's `discourse_channel` to an in-memory deque (max 50 messages).
2. **`tasks.loop(seconds=60)` tick** refreshes each guild's listen channel from settings, then walks every (guild, channel) with new buffered activity and runs the gate sequence in `_maybe_chime_in_one()`:
   - **mood_off_gate**: if the discourse mood is `off`, skip
   - **hours_gate**: only 9am-2am ET, else skip
   - **cooldown_gate**: no chime-in within the mood-tuned cooldown (40 min chill / 20 min yaps)
   - **daily_cap_gate**: bounded by the mood-tuned daily cap (10 for chill, uncapped for yaps) per channel per 24h
   - Calls **Haiku 4.5** [`chimein_score()`](../claude_client.py) on the buffer, returns `(score, vibe, hook)`
   - **vibe_gate**: drop if vibe in `{vulnerable, catchup, other}` (Toots doesn't interrupt private moments)
   - **threshold_gate**: drop if score < mood-tuned threshold (0.8 chill / 0.6 yaps)
3. If all gates pass, call **Sonnet 4.6** [`chimein_post()`](../claude_client.py) with the buffer + hook + recent image URLs (vision + web search both available) to generate the one-line take.
4. Send the message, record in `chimein_history` for cooldown + daily cap tracking, emit `chimein_posted` event.

### Tunable knobs

| What | Where | Current |
|---|---|---|
| Min buffer to score | `BUFFER_MIN_FOR_SCORE` in [`cogs/chimein.py`](../cogs/chimein.py) | 5 messages |
| Buffer max | `BUFFER_MAX` | 50 messages per channel |
| Per-mood cadence | `MOOD_TUNING` | chill: 0.8 / 5 / 40min · yaps: 0.6 / 10 / 20min |
| Hours window | `HOURS_START_ET`, `HOURS_END_ET_NEXT_DAY` | 9am to 2am ET |
| Skip vibes | `SKIP_VIBES` | `vulnerable`, `catchup`, `other` |
| Tick frequency | `TICK_SECONDS` | 60s (cheap, only scores buffers with new activity) |
| Scoring model | Haiku 4.5 | One-shot scoring, returns JSON-like line |
| Posting model | Sonnet 4.6 | Same model + tools as `/discourse` for tone parity |

### Parser hardening

[`_parse_chimein_score()`](../claude_client.py) is deliberately tolerant of model drift: strips markdown fences, extracts the first `{...}` block, clamps score to `[0, 1]`, coerces unknown vibes to `other`, and falls back to `(0.0, "other", "")` on any parse failure. This guarantees a bad response skips the slot rather than risking a misfired post.

### Observability

Two event kinds in [`utils/events.py`](../utils/events.py):

- `chimein_evaluated`: emitted once per skipped slot with `decision` field naming which gate stopped it (`mood_off_gate`, `hours_gate`, `cooldown_gate`, `daily_cap_gate`, `vibe_gate`, `threshold_gate`, `empty_generation`) plus `mood` where relevant. Lets you plot "where are we losing chime-in candidates?"
- `chimein_posted`: emitted on every actual post with `score`, `vibe`, `hook`, `mood`. Lets you see what Toots actually weighed in on and under which mood.

### When chime-in is "too chatty" or "too quiet"

- Switch the discourse `mood` from yaps to chill (or vice versa) in `/menu`. That's the intended dial.
- If both moods feel wrong, edit `MOOD_TUNING` in [`cogs/chimein.py`](../cogs/chimein.py). Bumping `chill.threshold` up makes her even more reserved when chill; dropping `yaps.cooldown` makes her even chattier when yaps.
- Repeating herself: the per-channel `recent_self_posts` block in `chimein_score()` is the existing dedup; pass more history if needed.

---

## Shared layers (all commands)

### Time context

Every Claude call gets a prefix in the user message:

```
[ctx, current time: 2026-05-24 09:00 UTC, 2026-05-24 02:00 PDT, weekday: Sunday]
```

Built in [`claude_client._time_context()`](../claude_client.py). Costs ~25 tokens, fixes day-of-week hallucinations. Spelled-out weekday so Claude doesn't have to compute.

### Persona caching

The full system prompt (~1 k tokens) is sent with `cache_control: {"type": "ephemeral"}`. Anthropic caches it for the next ~5 minutes; repeat calls within that window pay only for cache reads.

### Rate limits

Two flavors in [`utils/rate_limits.py`](../utils/rate_limits.py):

- **Per-user daily** (`/ask`, `/recap`), default 20, override via `/menu → per_user_daily_limit`.
- **Per-server daily** (`/discourse` manual, `/order`), default 20, override via `/menu → per_server_daily_limit`.
- **Cooldown**: only `/order` has one (15 min per user).

Hitting a cap emits a `rate_limit_hit` event. The bot returns a Toots-voice deflection from `voice.RATE_LIMIT_HIT` rather than a sterile error.

### Vision blocks

Image URLs are attached as Anthropic `image` content blocks:
```python
{"type": "image", "source": {"type": "url", "url": "..."}}
```

`_call()` hard-caps at 10 images per call regardless of caller. Per-image cost: ~85 fixed tokens + variable detail tokens (image-size dependent).

### Event emission

Every metric-worthy event flows through [`utils/events.emit()`](../utils/events.py) which writes a single `EVENT {...json...}` log line with the `tootsies.events` logger name. See [CLAUDE.md → Structured events](../CLAUDE.md#structured-events-for-dashboards) for the event catalog and how to query in Railway dashboards.

---

## Cheat sheet: "I want to change X"

| Desire | Change |
|---|---|
| "Toots is too chatty" | `/discourse mood:chill` or `/discourse mood:off` |
| "Toots feels under-informed" | already always-on web search; consider raising image cap in `_answer()` |
| "/ask answers are too short" | persona's ~140 char cap in [persona.py](../persona.py); not enforced by code, only by prompt, Claude tries |
| "Recaps miss context" | already on; if recap is dead, channel might be quiet or bot perms missing |
| "Too many low-quality /order PRs" | tighten preflight system prompt in [claude_client.preflight_order](../claude_client.py) |
| "/discourse repeats itself" | shorten 72h window in [`db.recent_discourse()`](../db.py) or raise to widen |
| "Costs are too high" | drop image cap in [_answer()](../cogs/ask.py) / [recap()](../cogs/recap.py); shorten channel context |
| "Want to add a new command" | `/order add a /foo command that does Y` and let the bot ship it |
