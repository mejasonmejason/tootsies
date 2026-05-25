"""URL allowlist guardrail for model-generated posts.

After the model writes a discourse post, we verify every URL in the output
came from one of the real sources we passed in (feed hot_urls, Perplexity
citations, web_search results from the tool_use blocks). Sonnet is strongly
prompted to never invent URLs but hallucinations still slip through. This is
the belt-and-suspenders catch: any URL that doesn't match the allowlist gets
stripped from the output before it goes to Discord.

Matching is normalized: lowercase scheme + host, trailing slash dropped,
trailing punctuation trimmed, common tracking params (utm_*, fbclid, gclid,
igshid, ref, si) stripped. Conservative on purpose, false positives (rejecting
a real link) cost more than false negatives (letting a near-miss through).
"""

from __future__ import annotations

import re

# Match http(s)://... URLs in free text. Stops at whitespace, brackets/parens,
# quotes, and angle brackets. Trailing punctuation is trimmed separately in
# _strip_trailing_punct so 'http://x.com/foo.' resolves to 'http://x.com/foo'.
_URL_RE = re.compile(r"https?://[^\s<>\"'()\[\]{}]+")
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
    return [_strip_trailing_punct(m.group(0)) for m in _URL_RE.finditer(text)]


def enforce_allowlist(
    text: str,
    allowlist: list[str],
) -> tuple[str, list[str]]:
    """Strip any URL from text that isn't in the normalized allowlist.

    Returns (cleaned_text, rejected_urls). Trailing punctuation on a URL
    in `text` is dropped from the URL but stays in the text (so "see https://
    foo.com." stripping the URL leaves "see ."). Whitespace runs and blank
    lines that result from the strip are collapsed.
    """
    allow_norm = {normalize(u) for u in allowlist if u}
    rejected: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        raw = match.group(0)
        cleaned = _strip_trailing_punct(raw)
        # Trailing punctuation that we trimmed from the URL stays in the
        # text by returning it after the empty replacement target.
        tail = raw[len(cleaned):]
        if normalize(cleaned) in allow_norm:
            return raw
        rejected.append(cleaned)
        return tail

    text = _URL_RE.sub(_replace, text)
    # Per-line whitespace cleanup, then collapse runs of blank lines.
    text = "\n".join(ln.rstrip() for ln in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Collapse mid-line double spaces left by URL removal.
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip(), rejected
