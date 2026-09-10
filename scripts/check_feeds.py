#!/usr/bin/env python3
"""check_feeds.py — check publications before adding them to subscriptions.yaml.

A Substack can look perfectly healthy in a browser and still produce nothing
useful here. Three things go wrong, and none of them are visible from the site:

  * The domain is not the publication. ``readmax.com`` is an unrelated site; the
    real Read Max lives at ``maxread.substack.com``.
  * The publication has moved off Substack and left a stale feed behind.
  * The feed carries teasers only, because posts are paid. That is not a failure
    — it means the publication belongs in ``subscriptions.yaml`` as
    ``tier: paid``, waiting on email ingestion, rather than as ``tier: free``.

This script fetches each candidate, runs the real cleaner over the bodies, and
reports which case you are in. It then prints a ready-to-paste YAML block with
the tier already set correctly.

Usage::

    # Check candidates. Accepts a feed URL, a bare domain, or a Substack handle.
    python scripts/check_feeds.py https://www.astralcodexten.com/feed
    python scripts/check_feeds.py construction-physics.com maxread
    python scripts/check_feeds.py --from-file my-substacks.txt

    # Re-check everything already configured.
    python scripts/check_feeds.py --configured

Exit codes:
    0  every candidate resolved and carries usable posts
    1  at least one candidate could not be used as a free publication
"""

from __future__ import annotations

import argparse
import re
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import feedparser  # noqa: E402
import requests  # noqa: E402

from mainspring import config  # noqa: E402
from mainspring.ingest.rss import TEASER_WORD_THRESHOLD, _entry_body_html  # noqa: E402
from mainspring.ingest.substack_html import clean_body_html  # noqa: E402

#: How many entries to clean when measuring body length. The whole feed is
#: usually 20 posts; a sample is enough to tell full text from a teaser and
#: keeps the check fast.
SAMPLE_SIZE = 6

#: Days since the newest post before a publication is called stale.
#:
#: Worth checking separately from body length, because a publication that has
#: moved elsewhere leaves a feed behind that still parses and still carries full
#: text, it just stops updating. Read Max is the case in point: it moved to
#: Patreon, and its Substack feed still returns twenty complete posts. Sixty days
#: is generous for a newsletter but short enough to catch an abandoned one.
STALE_DAYS = 60


@dataclass
class Result:
    """What we learned about one candidate."""

    given: str
    feed_url: str
    ok: bool
    verdict: str
    title: str = ""
    slug: str = ""
    homepage: str = ""
    entries: int = 0
    median_words: int = 0
    full_text_entries: int = 0
    #: Word count from fetching one post page, when the feed carried only a
    #: teaser and the fallback was probed. None if it was never needed.
    fetched_words: int | None = None
    latest: str = ""
    age_days: int | None = None
    stale: bool = False
    tier: str = "free"
    note: str = ""

    @property
    def worth_adding(self) -> bool:
        """True if this belongs in subscriptions.yaml at all, in some form.

        Distinct from ``ok``: a paid publication and a stale one both fail the
        "works as a free publication today" test but are still worth printing,
        the first as ``tier: paid`` and the second so the user can decide.
        """
        return self.ok or self.tier == "paid" or self.stale


#: A URL *path* already pointing at a feed, so ``/feed`` must not be appended.
#: Matched against the path only, never the whole URL, non-Substack publishers
#: hang query strings off their feeds (``/feed/?post_type=article``,
#: ``/feed/?rss``) and testing the full string would miss those and produce
#: ``/feed/?post_type=article/feed``.
_FEED_PATH = re.compile(
    r"(?:/feed/?$|/rss/?$|/atom/?$|/feeds?/\w+/?$|\.rss$|\.xml$|\.atom$)",
    re.IGNORECASE,
)


def normalize(candidate: str) -> str:
    """Turn whatever the user typed into a feed URL.

    Accepts a full feed URL, a bare domain, or a Substack handle::

        https://www.example.com/feed             ->  unchanged
        https://x.org/thing.rss                  ->  unchanged
        https://x.org/feed/?post_type=article    ->  unchanged
        www.example.com                          ->  https://www.example.com/feed
        example.substack.com                     ->  https://example.substack.com/feed
        example                                  ->  https://example.substack.com/feed

    ``/feed`` is only appended when the URL does not already look like a feed —
    a WordPress site with a query string, or a path ending ``.rss``, is left
    alone.
    """
    text = candidate.strip()
    if not text:
        return ""

    if not text.startswith(("http://", "https://")):
        # A bare word with no dot is a Substack handle.
        bare = text.rstrip("/")
        text = f"https://{bare}.substack.com" if "." not in bare else f"https://{bare}"

    parsed = urlparse(text)
    if _FEED_PATH.search(parsed.path) or parsed.query:
        # A path that already names a feed, or any query string at all, a
        # publisher who bothered to write one is pointing at something specific.
        return text

    return f"{text.rstrip('/')}/feed"


def slug_for(title: str, feed_url: str) -> str:
    """Suggest a URL-safe slug from the publication title.

    Falls back to the hostname when the title yields nothing usable — a
    publication called "!!!" still needs a filename.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    if slug:
        return slug[:80]
    host = urlparse(feed_url).hostname or "publication"
    return re.sub(r"[^a-z0-9]+", "-", host.replace("www.", "").split(".")[0]).strip("-")


def _age_in_days(entry: object) -> int | None:
    """Days between the newest post's date and now, or None if undated."""
    from datetime import datetime, timezone

    from mainspring.ingest.rss import _parse_date

    published = _parse_date(entry)
    if published is None:
        return None
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return max(0, (now - published).days)


def _probe_fallback(result: Result, entry: object, *, timeout: int) -> Result:
    """Decide what a teaser-only feed actually means, by fetching one post.

    Many free publications — most non-Substack news sites — put an excerpt in
    RSS and the article on the page. The ingester handles that: below
    ``TEASER_WORD_THRESHOLD`` it fetches the post URL and runs the cleaner over
    it. So a teaser feed is only a problem if that fetch also fails, which is
    what a paywall looks like.

    Fetches exactly one post — enough to tell the two apart without hammering
    anyone. Mutates and returns ``result``.
    """
    url = str(getattr(entry, "link", "") or "").strip()
    if not url:
        result.ok = False
        result.tier = "paid"
        result.verdict = "teasers only, no post URL to fall back to"
        return result

    try:
        response = requests.get(
            url,
            headers={"User-Agent": config.USER_AGENT},
            timeout=timeout,
            allow_redirects=True,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        result.ok = False
        result.tier = "paid"
        result.verdict = "teasers only, post page unreachable"
        result.note = f"Could not fetch {url} to test the fallback ({type(exc).__name__})."
        return result

    from mainspring.ingest.substack_html import extract_post_body

    body = extract_post_body(response.text, base_url=url)
    result.fetched_words = body.word_count

    if body.word_count >= TEASER_WORD_THRESHOLD:
        result.verdict = "full text by fetching the post"
        result.note = (
            f"The feed carries excerpts (median {result.median_words} words), but "
            f"the post page cleans to {body.word_count:,} words, so Mainspring will "
            "fetch it. Usable as tier: free, at one extra request per post — "
            "slower ingests than a Substack, and more load on the publisher."
        )
    else:
        result.ok = False
        result.tier = "paid"
        result.verdict = "teasers only"
        result.note = (
            f"The feed carries {result.median_words} words and the post page only "
            f"cleans to {body.word_count}, so there is no full text to be had. "
            "That is what a paywall looks like. Add as tier: paid if you subscribe "
            "— email ingestion will reach it."
        )
    return result


def check(candidate: str, *, timeout: int) -> Result:
    """Fetch and assess one candidate publication."""
    feed_url = normalize(candidate)
    if not feed_url:
        return Result(candidate, "", False, "empty input")

    # Substack custom domains are frequently www-only, so "example.com" fails DNS
    # where "www.example.com" works. Retrying is cheaper than making the user
    # guess which of the two their publication uses.
    attempts = [feed_url]
    host = urlparse(feed_url).hostname or ""
    if host and not host.startswith("www."):
        attempts.append(feed_url.replace(f"//{host}", f"//www.{host}", 1))

    response = None
    last_error = ""
    for attempt in attempts:
        try:
            response = requests.get(
                attempt,
                headers={"User-Agent": config.USER_AGENT},
                timeout=timeout,
                allow_redirects=True,
            )
            feed_url = attempt
            break
        except requests.RequestException as exc:
            last_error = type(exc).__name__

    if response is None:
        return Result(
            candidate, feed_url, False, "unreachable",
            note=f"No response from {' or '.join(attempts)} ({last_error}). "
                 "Check the domain spelling.",
        )

    if response.status_code != 200:
        return Result(
            candidate, feed_url, False, f"HTTP {response.status_code}",
            note="Check the domain — the publication may use a different one.",
        )

    content_type = response.headers.get("content-type", "")
    parsed = feedparser.parse(response.content)

    if not parsed.entries:
        # A 200 with no entries almost always means the URL is a web page, not
        # a feed, i.e. the domain belongs to something else entirely.
        hint = (
            "This URL returns a web page, not a feed. The domain probably is not "
            "this publication."
            if "html" in content_type
            else "The feed parsed but is empty. The publication may have moved."
        )
        return Result(candidate, feed_url, False, "no entries", note=hint)

    title = str(parsed.feed.get("title", "") or "").strip()
    homepage = str(parsed.feed.get("link", "") or "").strip()

    # Clean real bodies rather than trusting raw payload length, chrome inflates
    # a teaser to look like an article.
    word_counts: list[int] = []
    for entry in parsed.entries[:SAMPLE_SIZE]:
        body = clean_body_html(_entry_body_html(entry), base_url=entry.get("link", ""))
        word_counts.append(body.word_count)

    median_words = int(statistics.median(word_counts)) if word_counts else 0
    full_text = sum(1 for count in word_counts if count >= TEASER_WORD_THRESHOLD)
    latest = str(parsed.entries[0].get("published", "") or "").strip()
    age_days = _age_in_days(parsed.entries[0])

    result = Result(
        given=candidate,
        feed_url=feed_url,
        ok=True,
        verdict="",
        title=title or "(untitled feed)",
        slug=slug_for(title, feed_url),
        homepage=homepage or feed_url.rsplit("/feed", 1)[0],
        entries=len(parsed.entries),
        median_words=median_words,
        full_text_entries=full_text,
        latest=latest,
        age_days=age_days,
        stale=age_days is not None and age_days > STALE_DAYS,
    )

    # Staleness is checked first: "this publication has stopped posting" is more
    # actionable than "its posts are teasers", and a moved publication is often
    # both.
    if result.stale:
        result.ok = False
        result.verdict = f"stale — newest post is {age_days} days old"
        moved = re.search(r"\b(patreon|ghost|beehiiv|moved|now at)\b", title, re.I)
        result.note = (
            "The feed works but has not updated in months. "
            + (
                f"Its title ({title!r}) suggests the publication has moved — "
                "find its current home and use that feed instead."
                if moved
                else "Check whether the publication has moved or shut down."
            )
        )
    elif full_text == 0:
        # A teaser feed is not the end of it: the ingester falls back to fetching
        # the post page. Whether that works is the difference between "free, at
        # the cost of one extra request per post" and "paid, wait for email
        # ingestion", so actually try it rather than guess from the feed.
        result = _probe_fallback(result, parsed.entries[0], timeout=timeout)
    elif full_text < len(word_counts) / 2:
        result.verdict = "mostly teasers"
        result.note = (
            f"Only {full_text} of {len(word_counts)} sampled posts carry full "
            "text, so this is likely a paid publication with occasional free "
            "posts. Usable as free, but the front page will be thin."
        )
    else:
        result.verdict = "full text"

    return result


def report(results: list[Result]) -> None:
    """Print a human-readable summary, then a YAML block to paste."""
    print()
    for result in results:
        mark = "ok  " if result.ok else "FAIL"
        print(f"[{mark}] {result.given}")
        print(f"       feed:    {result.feed_url}")
        if result.title:
            print(f"       title:   {result.title}")
            print(
                f"       posts:   {result.entries} in feed, "
                f"{result.full_text_entries}/{min(SAMPLE_SIZE, result.entries)} "
                f"sampled carry full text, median {result.median_words:,} words"
            )
            if result.latest:
                age = f"  ({result.age_days} days ago)" if result.age_days is not None else ""
                print(f"       latest:  {result.latest}{age}")
        print(f"       verdict: {result.verdict}")
        if result.note:
            print(f"       note:    {result.note}")
        print()

    usable = [r for r in results if r.worth_adding]
    if not usable:
        print("Nothing to add.")
        return

    print("-" * 72)
    print("Paste into subscriptions.yaml under `publications:`\n")
    for result in usable:
        if result.stale:
            print(f"  # STALE, no posts in {result.age_days} days. Verify before keeping.")
        elif result.tier == "paid":
            print("  # Teasers only. Waits on email ingestion (Milestone 2).")
        print(f"  - name: {result.title}")
        print(f"    slug: {result.slug}")
        print(f"    feed_url: {result.feed_url}")
        print(f"    tier: {result.tier}")
        if result.homepage:
            print(f"    homepage_url: {result.homepage}")
        print()
    print("-" * 72)
    print("Then: python scripts/build_edition.py")


def main(argv: list[str] | None = None) -> int:
    """Check candidates and print a report. Returns the process exit code."""
    parser = argparse.ArgumentParser(
        description="Check publications before adding them to subscriptions.yaml.",
    )
    parser.add_argument(
        "candidates",
        nargs="*",
        help="Feed URLs, bare domains, or Substack handles.",
    )
    parser.add_argument(
        "--from-file",
        type=Path,
        metavar="PATH",
        help="Read candidates from a file, one per line. Blank lines and # comments ignored.",
    )
    parser.add_argument(
        "--configured",
        action="store_true",
        help="Re-check every publication already in subscriptions.yaml.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=config.REQUEST_TIMEOUT,
        help=f"Seconds per request (default {config.REQUEST_TIMEOUT}).",
    )
    args = parser.parse_args(argv)

    candidates = list(args.candidates)

    if args.from_file:
        if not args.from_file.exists():
            print(f"No such file: {args.from_file}", file=sys.stderr)
            return 1
        for line in args.from_file.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                candidates.append(line)

    if args.configured:
        try:
            candidates.extend(s.feed_url for s in config.load_subscriptions())
        except config.SubscriptionsError as exc:
            print(f"Cannot read subscriptions: {exc}", file=sys.stderr)
            return 1

    if not candidates:
        parser.print_help()
        return 1

    print(f"Checking {len(candidates)} publication(s)...", file=sys.stderr)
    results = [check(candidate, timeout=args.timeout) for candidate in candidates]
    report(results)

    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
