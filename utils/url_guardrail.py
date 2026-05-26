"""URL allowlist guardrail for model-generated posts.

After the model writes a post (discourse or ask), we verify every URL in the
output came from one of the real sources we passed in (feed hot_urls /
enriched-link URLs, Perplexity citations, web_search results from the tool_use
blocks). Sonnet/Haiku are strongly prompted to never invent URLs but
hallucinations still slip through. This is the belt-and-suspenders catch: any
URL that doesn't match the allowlist gets stripped from the output before it
goes to Discord.

Dedup pass: URLs already visible in the destination channel (or in the user's
question for /ask) are stripped even if they're in the allowlist: re-pasting
a URL the room just saw is redundant.

Matching is normalized: lowercase scheme + host, trailing slash dropped,
trailing punctuation trimmed, common tracking params (utm_*, fbclid, gclid,
igshid, ref, si) stripped. Conservative on purpose: false positives (rejecting
a real link) cost more than false negatives (letting a near-miss through).
"""

from __future__ import annotations

import re
from typing import Any

# Match http(s)://... URLs in free text. Stops at whitespace, brackets/parens,
# quotes, and angle brackets. Trailing punctuation is trimmed separately in
# _strip_trailing_punct so 'http://x.com/foo.' resolves to 'http://x.com/foo'.
# This is the canonical URL regex for the whole project: import from here
# rather than reinventing per-module. Conservative bracket-stopping handles
# Discord's common <URL> / (URL) / [URL] wrappers correctly.
URL_RE = re.compile(r"https?://[^\s<>\"'()\[\]{}]+")
_TRAILING_PUNCT = ".,!?;:'\""
_TRACKING_PARAM_KEYS = ("utm_", "fbclid", "gclid", "igshid", "ref", "si")


def _strip_trailing_punct(url: str) -> str:
    while url and url[-1] in _TRAILING_PUNCT:
        url = url[:-1]
    return url


def _strip_tracking_params(url: str) -> str:
    if "?" not in url:
        return url
    base, query = url.split("?", 1)
    kept: list[str] = []
    for part in query.split("&"):
        if not part:
            continue
        key = part.split("=", 1)[0].lower()
        if any(key == k or key.startswith(k) for k in _TRACKING_PARAM_KEYS):
            continue
        kept.append(part)
    return base + ("?" + "&".join(kept) if kept else "")


def normalize(url: str) -> str:
    """Normalize a URL for allowlist comparison.

    Lowercases scheme and host, strips tracking params, trims trailing
    punctuation and slash. Path and query case are preserved (URLs are
    technically case-sensitive past the host).
    """
    url = _strip_trailing_punct(url.strip())
    url = _strip_tracking_params(url)
    if "://" in url:
        scheme, rest = url.split("://", 1)
        if "/" in rest:
            host, path = rest.split("/", 1)
            url = f"{scheme.lower()}://{host.lower()}/{path}"
        else:
            url = f"{scheme.lower()}://{rest.lower()}"
    return url.rstrip("/")


def extract_urls(text: str) -> list[str]:
    """Find all URLs in text, trimmed of trailing punctuation."""
    return [_strip_trailing_punct(m.group(0)) for m in URL_RE.finditer(text)]


def enforce_allowlist(
    text: str,
    allowlist: list[str],
    *,
    recently_seen: list[str] | None = None,
) -> tuple[str, list[str], list[str]]:
    """Strip URLs from text. Returns (cleaned_text, rejected, deduped).

    - rejected: URLs not in the allowlist (hallucinations). Always stripped.
    - deduped: URLs in the allowlist BUT also in `recently_seen`. Stripped
      because the room just saw them; relinking is redundant.

    Trailing punctuation on a URL in `text` is dropped from the URL but stays
    in the text (so "see https://foo.com." stripping the URL leaves "see .").
    Whitespace runs and blank lines that result from the strip are collapsed.
    """
    allow_norm = {normalize(u) for u in allowlist if u}
    seen_norm = {normalize(u) for u in (recently_seen or []) if u}
    rejected: list[str] = []
    deduped: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        raw = match.group(0)
        cleaned = _strip_trailing_punct(raw)
        tail = raw[len(cleaned):]
        norm = normalize(cleaned)
        if norm not in allow_norm:
            rejected.append(cleaned)
            return tail
        if norm in seen_norm:
            deduped.append(cleaned)
            return tail
        return raw

    text = URL_RE.sub(_replace, text)
    text = "\n".join(ln.rstrip() for ln in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip(), rejected, deduped


def ensure_market_citation(
    text: str,
    snapshots: list[Any] | None,
) -> str:
    """Force-append a market URL if the response cites market data without one.

    The model is prompted to cite market URLs but routinely forgets under
    prompt pressure (long system_extra + DATA INTEGRITY rule + many tactics
    compete for attention). This is the mechanical safety net: if markets
    data was fetched AND the response contains no market URL, append the
    snapshot that best matches what she said.

    Matching strategy:
      1. If any snapshot's title text appears in the response, append that URL.
      2. Else, fall back to the first snapshot with a URL.

    No-op when no snapshots were provided or when the response already cites
    one of the snapshot URLs. Safe to call unconditionally after the
    guardrail.
    """
    if not snapshots:
        return text
    snapshot_urls = [getattr(s, "url", "") for s in snapshots]
    snapshot_urls = [u for u in snapshot_urls if u]
    if not snapshot_urls:
        return text
    response_urls = {normalize(u) for u in extract_urls(text)}
    if any(normalize(u) in response_urls for u in snapshot_urls):
        return text
    # Try to match a snapshot title to the response.
    text_lower = text.lower()
    chosen_url = ""
    for s in snapshots:
        title = (getattr(s, "title", "") or "").lower().strip()
        if title and len(title) >= 4 and title in text_lower:
            chosen_url = getattr(s, "url", "")
            if chosen_url:
                break
    if not chosen_url:
        chosen_url = snapshot_urls[0]
    return f"{text.rstrip()}\n\n{chosen_url}"


def enforce_source_links(
    text: str,
    *,
    feed_urls: list[str] | None = None,
    perplexity_context: str | None = None,
    web_search_urls: list[str] | None = None,
    recently_seen_urls: list[str] | None = None,
    market_urls: list[str] | None = None,
) -> tuple[str, list[str], list[str]]:
    """One-call guardrail: assemble the allowlist from the standard source
    channels and enforce it on `text`, with optional dedup.

    Source channels:
    - feed_urls: hot_urls from feeds (discourse) or enriched-link URLs from
      channel/question (ask).
    - perplexity_context: the Perplexity SOURCES block text; URLs are
      extracted using the canonical URL_RE.
    - web_search_urls: URLs returned by the server-side web_search tool
      (collected in ClaudeResult.web_search_urls).
    - recently_seen_urls: URLs already visible in the destination channel
      buffer (discourse) or the user's question + recent chatter (ask).
      Added to the allowlist (so they're not flagged as hallucinations)
      AND tracked for dedup (so they're stripped even though allowed).
    - market_urls: URLs from MarketSnapshot.url fields (polymarket.com,
      kalshi.com, sportsgameodds.com). Without this the constitution's
      "cite the market source" rule is decorative: the model would write
      the URL and the guardrail would strip it as a hallucination.

    Returns (cleaned_text, rejected, deduped). Empty input lists are fine.
    """
    allowlist: list[str] = []
    for src in (feed_urls, web_search_urls, recently_seen_urls, market_urls):
        if src:
            allowlist.extend(src)
    if perplexity_context:
        allowlist.extend(extract_urls(perplexity_context))
    return enforce_allowlist(text, allowlist, recently_seen=recently_seen_urls)
