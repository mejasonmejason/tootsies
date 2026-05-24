# Changelog

All notable changes to Tootsies. Dates in PT.

## [1.0.2], 2026-05-24 (persona refresh)

Persona reframed: Toots gets a proper backstory and a music spine. No behavioral changes to the commands.

### Persona

- **Bio.** Chicago kid, Miami based. Hot-season at Tootsies (March, September); off-season travel through Brazil, the Caribbean, Mexico. Surfs (Saquarema, Puerto), doesn't make a thing of it.
- **Music as the core identity.** Dad's Technics 1200 in the Chicago apartment, Sunday afternoons on the floor with his crates: Curtis Mayfield, EWF, Chaka, the Isleys, Frankie Knuckles, Common, early Kanye. Hears rap, R&B, funk, soul, disco, house, afrobeats, amapiano, baile funk, MPB, brega, reggaeton, dembow, dancehall, soca, gospel, blues, jazz, neo-soul, Afro-Cuban, samba as one continuous Black diaspora tradition. Sample-spotter. Drake fan, smart not blind, ICEMAN on rotation.
- **Sports.** Bulls first, Heat lowkey when she's home.
- **Cinema.** A24 girlie.
- **Names.** Calls users by their Discord display name naturally, like a bartender who remembers her regulars without making it a bit.
- **Self-presentation.** Doesn't perform cool, just is.

### Voice library

- Removed "regular" filler from `RATE_LIMIT_HIT`, `PERMISSION_DENIED`, `PLUMBING_TOUCHED`, `PIPELINE_RED` so canned deflections sound less like a stock-bot tic.
- Calibration examples in `persona.py` updated: "best pizza in sf" → "best pizza in miami" (Lucali), "did the warriors win" → "did the bulls win", added a sample-spot example (Curtis Mayfield / Kanye 'Stronger'), added a `wyd` example that uses the user's display name (`gaza`).

### Schedules

- Default scheduled-discourse times moved from PT to ET (Miami). Same clock hours: chill at ~12pm and 7pm ET; yaps at ~10am, 2pm, 6pm, 10pm ET. `cogs/discourse.py` constant renamed `PT` → `ET` (`America/Los_Angeles` → `America/New_York`).
- `claude_client._time_context()` injection now shows ET alongside UTC so Toots's "tonight" / "tomorrow" anchors to Miami time.

### Docs + site

- Homepage opening "meet toots" rewritten with the full backstory. Sample `/ask` interactions updated to match (Miami pizza, Bulls, sample-spotting).
- `EXECUTION_PLAN.md` §2 (Identity & persona), §3 (timezone refs), §5 (voice library samples) all updated.

---

## [1.0.1], 2026-05-24 (post-launch polish)

A wave of UX, accuracy, and content-quality fixes shipped same-day as v1.0.0 based on running Toots against the actual server. No new commands.

### `/menu` consolidation

- **Mood control moved into `/menu`** as a cycling button. Removed the `mood:` parameter from `/discourse`, which now does one thing only: post a discussion starter.
- **State-preservation bug fixed**: clicking the mood button no longer visually reverts the other dropdowns to their construction-time defaults. New `_refresh_select_defaults()` syncs each select's `default_values` to the user's current picks before any `edit_message` call.
- **`/menu_view` removed**: `/menu` now loads saved settings from the DB and shows them as the prefill, making the separate view command redundant.

### `/recap` accuracy

- **Reaction-weighted image selection**: `recent_image_urls()` now ranks images by reaction count first, recency as tie-break. A viral image from yesterday outranks a brand-new "yo" with zero reactions. Reasoning: if the room engaged with it, it's almost certainly more relevant to summarize.
- **Open the links the room shared**: new `hot_urls()` extracts URLs from message text, dedups, ranks by reactions + recency, and passes them into the `/recap` prompt as a "LINKS THE ROOM SHARED" block. System prompt explicitly tells Claude to OPEN those URLs via `web_search` rather than punt with "can't peep what's at the link", which was the old failure mode in link-heavy channels.
- **Source labels on fixer URLs**: `_classify_url()` recognizes embed-fixer hosts (fxtwitter, vxtwitter, tnktok, vxtiktok, ddinstagram, fxbsky, etc.) and labels them by canonical source (TikTok, X/Twitter, Instagram, Bluesky, YouTube, Reddit, Spotify, SoundCloud, Twitch, Tenor). So Toots knows what kind of content a tnktok URL points to even though she's never seen the host.
- **Recap prompt rewritten for commentary, not summary.** New voice rules: *"GIVE A TAKE, don't just summarize. For social content, drop a take WITH personality, yas queen, she ate, this is sending me, he's washed."* Includes a "VOICE" section reminding her to match the room's energy and never moderate.
- **`is_channel_dead` simplified**: only deflects when the channel is literally empty over the period (previously required 3+ messages of >5 chars each, which filtered out short chat, reactions, and link drops; in real channels that's everyone).

### Tier 1 video support

- **Multi-frame embed extraction**: `extract_media` now pulls BOTH `embed.image` and `embed.thumbnail` when both exist and differ. TikTok / Twitter video embeds often expose two different frames this way; more frames means better takes on what's actually in the clip.
- **Video URL as text reference**: `embed.video.url` is surfaced as a `kind="video"` ref so Claude knows the embed points to motion content (vs treating the cover frame as the whole story). Anthropic vision still doesn't process motion, Tier 2 (audio transcription via Whisper + frame extraction via ffmpeg) filed as [#9](https://github.com/mejasonmejason/tootsies/issues/9) for v1.2.

### Documentation + site

- **GitHub Pages site live** at https://mejasonmejason.github.io/tootsies/, single-page homepage built with jekyll-theme-cayman. Includes the rhinestone banner.
- **Homepage rewritten for non-engineer mods**: opens with "meet toots" + three Toots-voice example interactions, plain-English command surface, house rules in friendly bullets. Technical content (algorithms, structured event catalog, source) tucked under "for engineers" at the bottom.

### Bug fixes

- `tootsies.events` no longer emits `"error": null` on successful commands (was causing false positives in "all errors" dashboard queries). `emit()` now strips any field whose value is `None` before serializing.
- `.coverage` data file added to `.gitignore` (pytest-cov regenerates it on every run; should never be tracked).

---

## [1.0.0], 2026-05-24 (initial launch)

### Commands

**Everyone**
- `/ask <question>`, answers in Toots voice. Reads recent channel chatter (vibe only), web-searches for facts, sees up to 8 recent images via Claude vision.
- `/recap period:<1h | 1d | today>`, channel summary with reactions weighted. Web-augmented for facts the room is referencing. Sees recent images so it can name the meme that got reactions.
- `/discourse category:<pop | sports | cinema | hiphop | nba | custom>`, drops a discourse starter in the invoked channel. Pulls from configured feed channels, current channel's last hour, and the web. State-aware dedup over the last 72h.
- `@Toots <question>`, same backend as `/ask`. Discord's auto-mention on replies is ignored; explicit @Toots required.
- `/help`, overview of every command, who can run what, the daily caps.

**Mods only**
- `/order new <feature>`, files a GitHub issue tagged `@claude`. Claude writes a PR, CI runs (ruff/mypy/pytest with ≥50% coverage), auto-merges if green, Railway redeploys. Live narration in `#bot-logs`.
- `/order status [filter:mine | all | in-progress | failed]`, list orders.
- `/order retry <issue#>`, retry a failed order.
- `/order cancel <issue#>`, kill an in-flight order.
- `/discourse mood:<chill | yaps | off | status>`, control the scheduled posting cadence.
- `/menu`, interactive setup. Loads saved settings if already configured. Configure channels, mod roles, feed channels.
- `/close` / `/open`, toggle whether new `/order` requests are accepted.
- `/undo`, roll back to the previous successful Railway deploy via the Railway API.

### Pipeline

- GitHub Actions: ruff + mypy + pytest (coverage gate ≥50%) on every PR.
- Claude Code Action with `@claude` trigger writes PRs in response to `/order` issues.
- Railway auto-deploys `main` on push, gated by "Wait for CI."
- Healthcheck on `/health` triggers automatic restart on failure.
- `/undo` uses Railway GraphQL `deploymentRedeploy(usePreviousImageTag: true)` for fast rollback (no rebuild).

### Persona & guardrails

- Toots persona: late-20s bartender voice; sharp, lowercase, terse; ~140 char answer cap.
- **No em dashes**: enforced by test (`tests/test_persona.py::test_no_em_dashes_in_persona_constitution_or_voice`).
- **Restate the question** on `/ask` so answers read self-contained later in scrollback. Restatement doesn't count toward the cap.
- **Constitution** (`constitution.py`): no doxxing, no NSFW, no slurs, no impersonation, no medical/legal/financial advice, no moderation actions, guild-only, minors handled with flattened persona, crisis content breaks character with real resources. Cannot be loosened by `/order`.
- **Protected paths** in `/order` pre-flight: constitution, persona core voice, `.github/`, Dockerfile, railway.toml, Procfile, db.py connection, bot.py boot, requirements.txt deletions. Pre-flight Sonnet pass rejects these with a Toots-voice deflection ("that's plumbing, regular. ask the architect.").

### Observability

- Structured JSON event lines prefixed with `EVENT` for Railway log-based dashboards. See [CLAUDE.md](CLAUDE.md#structured-events-for-dashboards) for the event catalog.
- Event kinds: `command`, `claude_api`, `order_state`, `rate_limit_hit`, `deploy_event`, `error`, `recap_deflected`, `discourse_fallback`.
- `command_metrics` Postgres table for per-invocation latency/success/error tracking (30-day retention).
- `audit_log` table for /menu changes, /close, /open, /undo, order rejections, state transitions (90-day retention).
- Verbosity-gated `#bot-logs` posts for order status, recap deflections, discourse fallbacks.

### Infrastructure

- Python 3.11+, discord.py 2.4, asyncpg 0.30, anthropic 0.40, aiohttp 3.10.
- Postgres for settings, rate limits, order history, audit log, discourse dedup, command metrics, schedule state.
- Idempotent schema bootstrap on every boot, including a legacy `mood_state` → `discourse_schedule` migration block.
- Dockerfile + railway.toml; healthcheck path `/health` with 30s timeout, restart-on-failure with max 5 retries.

### Cost controls

- Persona prompt cached with `cache_control: ephemeral` (~1 k tokens saved per repeat call within 5 min).
- Time context prefix (~25 tokens) fixes day-of-week hallucinations.
- Vision hard cap: 10 images per Claude call (with smaller per-caller caps).
- 5 MB image size cap (Anthropic vision limit).

### Documentation

- [CLAUDE.md](CLAUDE.md), developer-facing project intro, conventions, event catalog.
- [docs/ALGORITHMS.md](docs/ALGORITHMS.md), per-command flow + tunable knobs reference.
- [EXECUTION_PLAN.md](EXECUTION_PLAN.md), frozen v1 design artifact.
- [.github/pull_request_template.md](.github/pull_request_template.md), PREVIEW convention for UI changes.

### Test coverage

- 50.84% total (gate at 50%). Core utils all at ≥75%: `claude_client` 100%, `rate_limits` 100%, `events` 100%, `metrics` 98%, `permissions` 95%, `voice` 94%, `feeds` 92%, `config` 89%, `github` 82%, `railway` 75%.

---

## Unreleased

See [GitHub Issues](https://github.com/mejasonmejason/tootsies/issues) for the full backlog. Top v1.1 candidates:

- **GIPHY gifs in replies** ([#2](https://github.com/mejasonmejason/tootsies/issues/2)), Toots sends gifs when they land harder than words.
- **`/stats` admin command** ([#3](https://github.com/mejasonmejason/tootsies/issues/3)), reads `command_metrics` for in-Discord visibility.
- **Per-user `/ask` memory** ([#4](https://github.com/mejasonmejason/tootsies/issues/4)), "I asked about Lakers yesterday, what's new?"
- **AI code review pass on `/order` PRs** ([#5](https://github.com/mejasonmejason/tootsies/issues/5)), second Claude reviews first Claude.
- **`/order` from screenshot** ([#6](https://github.com/mejasonmejason/tootsies/issues/6)), vision parses a posted image into a spec.
- **Cog test coverage push** ([#7](https://github.com/mejasonmejason/tootsies/issues/7)), bring `cogs/*` from 25–36% to 60%+, ratchet gate to 65%.
- **Tootsies chips in** ([#8](https://github.com/mejasonmejason/tootsies/issues/8)), spontaneous bartender takes layered over chat. Two-phase: opt-in per channel, then a learning loop on reception.

### v1.2 candidates

- **Video audio + multi-frame stills** ([#9](https://github.com/mejasonmejason/tootsies/issues/9)), Whisper transcription + ffmpeg frame extraction so Toots can hear what's said in a video and see beyond the cover frame.

### Deferred (per plan §13 v1.1)

- Thread-based `/ask` conversations
- Voting / leaderboards
- Real-time NBA scores tool
- Verzuz scoring system
- Engagement analytics for mods
- Game commands (`/coinflip`, `/8ball`, `/roll`, polls, etc.)
