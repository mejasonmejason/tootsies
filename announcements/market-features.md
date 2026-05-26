# Toots Now Pulls Live Market Data

Toots got a new trick. Ask her about sports lines, prediction markets, or any "will X happen" question and she'll pull live data before answering. No new commands to learn: just `/ask` or `@Toots` like you always do. She knows when you're asking about odds and goes and gets them.

---

## Three Sources, One Bartender

**Sports lines** via SportsGameOdds: spreads, moneylines, over/unders, player props across NBA, NFL, MLB, NHL, UFC, MLS, and more. Real book lines, not vibes.

**Polymarket**: the crowd's read on anything from Fed rate cuts to album drops. Binary markets (yes/no with a %) and multi-outcome races (primaries, award shows, MVP voting) with every candidate's number.

**Kalshi**: CFTC-regulated prediction markets. Same shape as Polymarket but US-regulated, so the prices carry different weight. Toots checks both and tells you when they disagree.

---

## Examples

### Sports

> **`/ask what's the spread on the lakers game`**
> Toots pulls the current line from live sportsbooks and gives you the number with her read on it. Links the source so you can check yourself.

> **`@Toots any good NBA parlays tonight`**
> She looks at tonight's full slate, picks out the interesting lines, and builds a take around them. No "locks", no sizing advice, just sharp commentary on what the board looks like.

> **`/ask chiefs game over/under, smart side?`**
> Gets the total, tells you which side she likes and why. Cites the actual number from the book.

### Prediction Markets

> **`/ask will drake drop an album by july`**
> Pulls the live Polymarket and Kalshi odds, tells you what the crowd thinks, and layers her own take on top.

> **`/ask fed cuts rates before december?`**
> Checks both platforms for Fed rate markets, shows you where the money is, and breaks down what the spread between Poly and Kalshi means if they differ.

> **`/ask 2028 republican primary, who's got the lead right now`**
> Multi-outcome market. She'll list the full field with each candidate's implied probability, not just the frontrunner. You see the whole race at a glance.

### Just Vibes (Still Works)

> **`/ask is drake done`**
> No market intent detected. No odds fetched. Just Toots being Toots.

> **`/ask best taco spot in oakland`**
> Pure bartender energy. Markets stay out of it.

---

## What She Won't Do

- No "locks." She won't tell you something can't lose.
- No bet sizing. She won't tell you how much to put on anything.
- No financial advice. She's a bartender with a data feed, not a broker.

---

## How It Works (For the Curious)

Every `/ask` runs through a lightweight classifier that decides: is this about sports, a prediction market, or neither? If it's sports, she pulls lines for the right league. If it's a prediction market question, she searches Polymarket and Kalshi in parallel and combines the results. All of it happens before she starts writing her answer, so the market data is baked into her take, not tacked on.

If any source is down or slow, she just answers without it. You'll never see an error, she just drops back to commentary mode.

All market data links to the source. If she cites a number, there's a URL.

---

*Same Toots. Same `/ask`. Now with receipts.*
