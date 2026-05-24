---
title: Tootsies
layout: default
---

<p align="center">
  <img src="assets/banner.png" alt="Tootsies" width="700"/>
</p>

# tootsies

a discord bot for the Tootsies server. built around **Toots**, a hip city-girl bartender persona. mods ship new features by typing `/order <feature>` in discord — claude writes the code, CI runs, auto-merges, and railway redeploys. live narration in `#bot-logs`.

- **repo**: [github.com/mejasonmejason/tootsies](https://github.com/mejasonmejason/tootsies)
- **changelog**: [CHANGELOG.md](https://github.com/mejasonmejason/tootsies/blob/main/CHANGELOG.md)
- **v1.1 backlog**: [open issues](https://github.com/mejasonmejason/tootsies/issues?q=is%3Aissue+is%3Aopen+label%3Av1.1)

---

## what toots does

### everyone can run

| command | what it does |
|---|---|
| **`/ask <question>`** | ask anything. she reads the room and checks the web. ~140 chars. |
| **`@Toots <question>`** | same backend as `/ask`. *ayo @Toots what's the move* works. |
| **`/recap period:<last hour \| last 24h \| today>`** | what you missed in this channel. spicy. |
| **`/discourse category:<pop \| sports \| cinema \| hiphop \| nba \| custom>`** | a discussion starter for the room. |
| **`/help`** | the full menu, in one screen. |

### mods only

| command | what it does |
|---|---|
| **`/order new <feature>`** | tell toots a new thing you want her to do; she builds it. |
| **`/order status [filter]`** | see what's cooking. |
| **`/order retry <number>`** | retry a failed order. |
| **`/order cancel <number>`** | call it off. |
| **`/menu`** | set up channels, mod roles, feed channels, and her posting cadence. |
| **`/close` / `/open`** | stop or restart taking `/order` requests. |
| **`/undo`** | roll back to the previous working version. |

### daily caps

- **20/day per user** for `/ask` (+ `@Toots`) and `/recap`.
- **20/day server-wide** for `/discourse` and `/order`.
- `/order` also has a 15-minute per-user cooldown.
- no caps on `/menu`, `/help`, `/close`, `/open`, `/undo`.

---

## the persona

toots is late-20s, bartending the hottest spot in town. sharp, plugged in, opinionated. drake fan (smart, not blind). knows hip-hop, NBA, cinema, pop culture.

- **engaging > correct, sharp ≠ mean.**
- lowercase by default. punctuation is loose.
- short. no preamble. no "great question."
- no emoji unless someone uses one first.
- never breaks character to say she's an AI.
- never uses em dashes (enforced by a test).
- restates `/ask` questions briefly so answers read self-contained in scrollback.

### hard rules (can't be loosened by `/order`)

- no doxxing or identity inference.
- no NSFW, slurs, hate speech.
- no fabricated quotes from real people.
- no impersonation of server members.
- no medical / legal / financial advice — deflect.
- no moderation actions (kick / ban / mute / delete / role change).
- no DMs initiated or accepted. guild-only.
- minors: persona flattens, age-appropriate only.
- crisis content: breaks character, real care, real resources.

---

## how `/ask` works

1. **rate-limit check** (20/day per user).
2. **defer** so discord shows "thinking…".
3. **gather channel context**: last 30 messages, humans only.
4. **format**: render as `name: content [media-labels]` per line. embed text from auto-unfurled links is inlined.
5. **harvest images**: up to 8 image URLs from recent messages (attachments + Tenor/GIPHY previews), under the 5 MB vision cap.
6. **call Claude Haiku 4.5** with:
   - cached persona + constitution (~1 k tokens)
   - the question, the channel chatter, and the images
   - source-trust hierarchy: web for facts > toots's taste for opinions > channel chatter for vibe only (never quoted)
   - web_search tool always available
7. **consume the rate-limit slot** only on success.
8. **reply** as the slash command followup.

**knobs**: channel context size, vision image cap, daily cap, web-search availability. See [docs/ALGORITHMS.md](https://github.com/mejasonmejason/tootsies/blob/main/docs/ALGORITHMS.md) for file:line references.

---

## how `/recap` works

1. resolve the period (`1h`, `1d`, `today since midnight UTC`).
2. read up to 200 channel messages, **including bot and webhook posts** (feed channels are mostly bots; we want them).
3. if the channel is *literally* empty over that period → deflect with a quip ("dead in here. what'd you eat tonight, make it interesting.") and emit a diagnostic event.
4. otherwise call Claude Haiku 4.5 with:
   - the message blob (reactions weighted in the rendering)
   - the last 8 recent image URLs (so toots can name the meme that got reactions)
   - web_search available — used for verifiable facts the room references ("everyone's hyped about the lakers" → toots looks up the actual score)

---

## how `/discourse` works

manual mode. picks the freshest threadworthy thing across multiple sources and posts a discussion starter.

1. **gather sources**:
   - configured **feed channels** filtered by category (last 24h, ~10 messages each)
   - **current channel** last hour
   - **dedup history**: last 72h of toots's own discourse posts for this category (with timestamps)
2. **state-aware dedup**: each stored post bakes in the topic's current state ("lakers vs nuggets r2, series 1-1") so later checks can tell a fresh take from a recycle. if a topic hasn't evolved, toots picks a different angle (manual `/discourse` always posts).
3. **call Claude Sonnet 4.6** with web_search. sonnet for the judgment call about what's worth posting.
4. **store the post** in `discourse_history` for the next 72h of dedup.

---

## the scheduled poster (mood)

set via `/menu → mood`. cycles `chill → yaps → off`.

- **chill** (default): ~12pm and ~7pm PT
- **yaps**: ~10am, ~2pm, ~6pm, ~10pm PT
- **off**: silent

every minute a tick checks each configured guild's schedule:

- skip if `off` or if today's quota is filled.
- otherwise call Haiku with the last 72h of cross-category discourse history.
- if Claude returns the literal word `EMPTY`, the slot is **consumed but no post happens**. otherwise post to the configured discourse channel.

intentional: consuming the slot on EMPTY means toots doesn't burn API calls retrying every minute when nothing's new. she'll get another shot at the next scheduled slot.

---

## the `/order` pipeline

```
mod runs /order new <feature> in discord
   ↓
bot pre-flight checks the request with Claude Sonnet
   ↓     allow / plumbing / reject
files a GitHub issue tagged @claude (if allowed)
   ↓
Claude Code Action writes the code and opens a PR
   ↓
CI: ruff + mypy + pytest (coverage gate ≥50%)
   ↓ (if green)
PR auto-merges to main
   ↓
Railway detects push, builds, deploys
   ↓
healthcheck on /health
   ↓ (if pass)
bot posts "✅ Served" in #bot-logs
   ↓ (if fail at any step)
auto-restart on failure; mod can /undo to roll back
```

### pre-flight verdicts

- **`allow`** → file the issue, normal flow.
- **`reject`** → deflect with "my bosses can't allow that one." catches moderation requests, NSFW, incoherent specs, off-scope features.
- **`plumbing`** → deflect with "that's plumbing, regular. ask the architect." catches attempts to touch constitution.py, persona.py core voice, .github/, Dockerfile, railway.toml, Procfile, db.py connection, bot.py boot, or requirements.txt deletions. exceptions: adding new cogs, new tables, new optional deps, new quips in `utils/voice.py` are all fine.

---

## under the hood

- **stack**: python 3.11+, discord.py 2.4, asyncpg 0.30, anthropic 0.40, aiohttp 3.10
- **postgres**: settings, rate limits, order history, audit log, discourse dedup, command metrics, schedule state
- **model routing**: Haiku 4.5 for /ask, /recap, scheduler, deflections (fast, cheap); Sonnet 4.6 for /discourse and /order pre-flight (needs judgment)
- **deployed on railway**: auto-deploy on push to main with "wait for CI", healthcheck-driven auto-restart
- **observability**: every metric-worthy event emits a single JSON log line prefixed `EVENT`. railway dashboards parse these for command volume, latency percentiles, error rate, claude token spend. see [CLAUDE.md](https://github.com/mejasonmejason/tootsies/blob/main/CLAUDE.md#structured-events-for-dashboards) for the event catalog.
- **tests**: 114 unit tests, 50%+ coverage gate. core files at 75–100%.

---

## v1.1 in flight

filed as GitHub issues, ranked roughly by effort × impact:

1. **GIPHY gifs in replies** — toots sends gifs when they land harder than words ([#2](https://github.com/mejasonmejason/tootsies/issues/2))
2. **/stats admin command** — in-discord visibility into bot health ([#3](https://github.com/mejasonmejason/tootsies/issues/3))
3. **per-user /ask memory** — she remembers her regulars ([#4](https://github.com/mejasonmejason/tootsies/issues/4))
4. **AI code review on /order PRs** — second claude reviews first claude ([#5](https://github.com/mejasonmejason/tootsies/issues/5))
5. **/order from-screenshot** — vision parses a posted image into a spec ([#6](https://github.com/mejasonmejason/tootsies/issues/6))
6. **cog test coverage push** — 25% → 60%, gate to 65% ([#7](https://github.com/mejasonmejason/tootsies/issues/7))
7. **toots chips in** — spontaneous bartender takes layered over chat ([#8](https://github.com/mejasonmejason/tootsies/issues/8))

---

## deeper reference

- [docs/ALGORITHMS.md](https://github.com/mejasonmejason/tootsies/blob/main/docs/ALGORITHMS.md) — per-command flow walkthroughs with tunable knobs
- [CLAUDE.md](https://github.com/mejasonmejason/tootsies/blob/main/CLAUDE.md) — developer intro, structured event catalog
- [EXECUTION_PLAN.md](https://github.com/mejasonmejason/tootsies/blob/main/EXECUTION_PLAN.md) — frozen v1 design doc

---

<p align="center"><em>last orders. tip 25%.</em></p>
