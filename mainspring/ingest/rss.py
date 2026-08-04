"""rss.py — ingest free Substack publications over RSS.

Substack puts the entire post inside ``<content:encoded>`` for free
publications, which means one feed request per publication is usually all it
takes: no crawling, no rate-limit dance, no scraping. The post URL is only
fetched when a feed entry turns out to carry a teaser instead of a body.

Every HTML payload — feed content and fetched page alike — goes through
:mod:`mainspring.ingest.substack_html`, so the email ingester added in
Milestone 2 produces byte-identical article bodies for the same post.

Deduplication is by ``Article.url``. Re-ingesting the same feed is therefore
free and idempotent: existing rows are refreshed in place (a post edited after
publication picks up its edits) but an existing standfirst is never discarded,
so no article is ever summarized twice.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone

import feedparser
import requests
from dateutil import parser as date_parser
from sqlalchemy.orm import Session

from .. import config
from ..config import Subscription
from ..models import Article, Publication
from .substack_html import CleanedBody, clean_body_html, extract_post_body, slug_from_url

logger = logging.getLogger(__name__)

#: Below this word count a feed body is treated as a teaser and the post URL is
#: fetched instead. Substack's own excerpts run 30–60 words; a real short post
#: rarely falls under 120.
TEASER_WORD_THRESHOLD = 120

#: How many publications to fetch at once. Feeds are I/O bound and there are
#: only a handful, so a small pool is plenty and keeps us a polite visitor.
FETCH_WORKERS = 4


@dataclass
class IngestReport:
    """What one ingest run did.

    Attributes:
        publications: How many publications were polled.
        inserted:     New articles written.
        updated:      Existing articles refreshed in place.
        skipped:      Entries rejected — no URL, or no usable body.
        errors:       Human-readable failures, one per publication that broke.
    """

    publications: int = 0
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []

    @property
    def ok(self) -> bool:
        """True when every publication was polled without error."""
        return not self.errors

    def summary(self) -> str:
        """One-line description, for logs and the CLI."""
        return (
            f"{self.publications} publications: "
            f"{self.inserted} new, {self.updated} updated, {self.skipped} skipped"
            + (f", {len(self.errors)} errors" if self.errors else "")
        )


# ---------------------------------------------------------------------------
# Publication table sync
# ---------------------------------------------------------------------------

def sync_publications(db: Session, subs: list[Subscription]) -> dict[str, Publication]:
    """Mirror subscriptions.yaml into the ``publications`` table.

    Publications are matched on ``slug``; name, feed URL, tier and homepage are
    updated in place so editing the YAML does not orphan existing articles.
    Removing an entry from the YAML leaves its row alone — the archive keeps
    working, that publication simply stops being polled.

    Args:
        db:   Open session. Committed before returning.
        subs: Every configured subscription, free and paid.

    Returns:
        Mapping of slug to the persisted :class:`~mainspring.models.Publication`.
    """
    existing = {p.slug: p for p in db.query(Publication).all()}
    result: dict[str, Publication] = {}

    for sub in subs:
        row = existing.get(sub.slug)
        if row is None:
            row = Publication(slug=sub.slug)
            db.add(row)
        row.name = sub.name
        row.feed_url = sub.feed_url
        row.tier = sub.tier
        row.homepage_url = sub.homepage_url
        result[sub.slug] = row

    db.commit()
    for row in result.values():
        db.refresh(row)
    return result


# ---------------------------------------------------------------------------
# Feed parsing
# ---------------------------------------------------------------------------

def _parse_date(entry: object) -> datetime | None:
    """Pull a naive-UTC publication timestamp out of a feedparser entry.

    feedparser exposes both pre-parsed ``time.struct_time`` values and the raw
    strings. The structs are tried first because they need no guessing; the
    strings are a fallback for feeds feedparser could not parse itself.

    Returns None when no field yields a usable date, which the caller treats as
    "file this under the day it was ingested".
    """
    for field in ("published_parsed", "updated_parsed"):
        value = getattr(entry, field, None)
        if value:
            try:
                return datetime(*value[:6])
            except (TypeError, ValueError):
                pass

    for field in ("published", "updated"):
        value = getattr(entry, field, None)
        if value:
            try:
                parsed = date_parser.parse(value)
            except (ValueError, OverflowError, TypeError):
                continue
            if parsed.tzinfo is None:
                return parsed
            return parsed.astimezone(timezone.utc).replace(tzinfo=None)

    return None


def _entry_body_html(entry: object) -> str:
    """Return the richest HTML body a feed entry offers.

    Preference order is ``content:encoded`` (the whole post, which is what
    Substack sends for free publications), then ``summary``/``description``
    (a teaser). feedparser normalizes ``content:encoded`` into ``entry.content``
    as a list of dicts.
    """
    content = getattr(entry, "content", None)
    if isinstance(content, list) and content:
        # Prefer the longest candidate: some feeds ship both a text/plain and a
        # text/html representation.
        best = max(content, key=lambda part: len(part.get("value") or ""))
        if best.get("value"):
            return str(best["value"])

    for field in ("summary", "description"):
        value = getattr(entry, field, None)
        if isinstance(value, str) and value.strip():
            return value

    return ""


def _entry_author(entry: object, fallback: str) -> str:
    """Return the byline, falling back to the publication name.

    Substack sets ``dc:creator`` to the post's author, which feedparser surfaces
    as ``entry.author``. Guest posts differ from the publication name, which is
    exactly why the byline is stored per article rather than looked up.
    """
    author = getattr(entry, "author", None)
    if isinstance(author, str) and author.strip():
        return author.strip()[:256]
    return fallback[:256]


def _fetch_post_page(url: str) -> str:
    """GET a post URL and return its HTML, or "" on any failure.

    Failure is expected and non-fatal: the post may be paywalled, moved, or the
    network may be down. The caller falls back to the teaser body.
    """
    try:
        response = requests.get(
            url,
            headers={"User-Agent": config.USER_AGENT},
            timeout=config.REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        return response.text
    except requests.RequestException as exc:
        logger.warning("Could not fetch %s: %s", url, exc)
        return ""


def _resolve_body(entry_html: str, url: str, *, allow_fetch: bool) -> CleanedBody:
    """Clean a feed entry's body, fetching the post itself if it was a teaser.

    Args:
        entry_html:  Raw HTML from the feed entry.
        url:         Canonical post URL, used as the base for relative links and
                     as the fetch target.
        allow_fetch: False disables the network entirely, which is how the test
                     suite and any offline rebuild stay offline.

    Returns:
        The best :class:`~mainspring.ingest.substack_html.CleanedBody` available.
        If fetching produced less text than the feed did, the feed body wins —
        a paywall interstitial is worse than a teaser.
    """
    from_feed = clean_body_html(entry_html, base_url=url)

    if from_feed.word_count >= TEASER_WORD_THRESHOLD or not allow_fetch:
        return from_feed

    page = _fetch_post_page(url)
    if not page:
        return from_feed

    from_page = extract_post_body(page, base_url=url)
    return from_page if from_page.word_count > from_feed.word_count else from_feed


def parse_feed(
    subscription: Subscription,
    *,
    feed_source: str | bytes | None = None,
    allow_fetch: bool = True,
) -> list[dict]:
    """Parse one publication's feed into article-shaped dicts.

    Pure with respect to the database — nothing is written here, which is what
    makes the parse testable against a saved feed without a DB at all.

    Args:
        subscription: The publication to parse.
        feed_source:  Raw feed bytes or XML string. When None the feed is
                      fetched from ``subscription.feed_url``. Tests pass saved
                      fixture bytes.
        allow_fetch:  Whether teaser entries may trigger a post-page fetch.

    Returns:
        One dict per usable entry, with the keys consumed by
        :func:`upsert_article`. Entries without a URL, or whose body cleaned
        down to nothing, are dropped.
    """
    source = feed_source if feed_source is not None else subscription.feed_url
    parsed = feedparser.parse(source)

    if parsed.bozo and not parsed.entries:
        raise ValueError(f"{subscription.name}: unparseable feed ({parsed.bozo_exception})")

    # Newest first, capped: see config.MAX_ENTRIES_PER_FEED. Uncapped, a
    # 100-entry excerpt feed costs 100 page fetches against one publisher.
    entries = parsed.entries[: config.MAX_ENTRIES_PER_FEED]
    if len(parsed.entries) > len(entries):
        logger.info(
            "%s: feed carries %d entries, taking the newest %d",
            subscription.name,
            len(parsed.entries),
            len(entries),
        )

    records: list[dict] = []
    for entry in entries:
        url = str(getattr(entry, "link", "") or "").strip()
        if not url:
            continue

        title = str(getattr(entry, "title", "") or "").strip() or "Untitled"
        # Both switches must be on: the run-wide one and the publication's own.
        body = _resolve_body(
            _entry_body_html(entry),
            url,
            allow_fetch=allow_fetch and subscription.fetch_pages,
        )
        if body.is_empty:
            logger.debug("%s: no usable body for %s", subscription.name, url)
            continue

        records.append(
            {
                "title": title[:512],
                "url": url[:1024],
                "slug": slug_from_url(url),
                "author": _entry_author(entry, subscription.name),
                "published_date": _parse_date(entry),
                "body_html": body.html,
                "body_text": body.text,
                "word_count": body.word_count,
                "hero_image_url": body.hero_image_url,
                "hero_image_alt": body.hero_image_alt,
                "source_kind": "rss",
            }
        )

    return records


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _unique_slug(db: Session, publication_id: int, slug: str, url: str) -> str:
    """Return a slug free of collisions within one publication.

    Substack truncates long titles when it builds a post URL, so two posts can
    genuinely arrive with the same final path segment. A numeric suffix is
    appended until the ``(publication_id, slug)`` pair is unused. Only URLs
    differing from ``url`` count as collisions, so re-ingesting a post keeps the
    slug it already has.

    The session flushes first. Sessions here are created with ``autoflush=False``
    and a whole feed is upserted before a single commit, so without the flush a
    collision between two entries of the same feed would be invisible here and
    would instead surface as an IntegrityError at commit — losing the entire
    batch rather than renaming one page.
    """
    db.flush()

    candidate = slug
    suffix = 2
    while True:
        clash = (
            db.query(Article)
            .filter(
                Article.publication_id == publication_id,
                Article.slug == candidate,
                Article.url != url,
            )
            .first()
        )
        if clash is None:
            return candidate
        candidate = f"{slug}-{suffix}"
        suffix += 1


def upsert_article(db: Session, publication: Publication, record: dict) -> str:
    """Insert or refresh one article. Returns "inserted", "updated" or "skipped".

    ``url`` is the deduplication key, shared by every ingest path. On a repeat
    sighting the body, title and byline are refreshed — posts do get edited —
    but ``standfirst`` is left alone unless the body actually changed, so a
    rebuild does not re-run the model over the whole archive.

    The caller is responsible for committing.
    """
    # Flush so rows added earlier in this batch are visible to the lookup — the
    # session does not autoflush. See _unique_slug for why that matters.
    db.flush()
    existing = db.query(Article).filter(Article.url == record["url"]).first()

    if existing is None:
        article = Article(
            publication_id=publication.id,
            title=record["title"],
            url=record["url"],
            slug=_unique_slug(db, publication.id, record["slug"], record["url"]),
            author=record["author"],
            published_date=record["published_date"],
            body_html=record["body_html"],
            body_text=record["body_text"],
            word_count=record["word_count"],
            hero_image_url=record["hero_image_url"],
            hero_image_alt=record["hero_image_alt"],
            source_kind=record["source_kind"],
            standfirst=None,
            summarizing=False,
        )
        db.add(article)
        return "inserted"

    body_changed = existing.body_text != record["body_text"]

    existing.title = record["title"]
    existing.author = record["author"]
    existing.published_date = record["published_date"] or existing.published_date
    existing.hero_image_url = record["hero_image_url"] or existing.hero_image_url
    existing.hero_image_alt = record["hero_image_alt"] or existing.hero_image_alt

    if body_changed:
        existing.body_html = record["body_html"]
        existing.body_text = record["body_text"]
        existing.word_count = record["word_count"]
        # The post was rewritten, so its standfirst no longer describes it.
        existing.standfirst = None
        existing.summarizing = False
        return "updated"

    return "skipped"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def ingest(
    subs: list[Subscription] | None = None,
    *,
    allow_fetch: bool = True,
) -> IngestReport:
    """Poll every free publication and write new articles to the database.

    Paid publications are skipped: their feeds carry only a teaser, and the
    full post arrives by email instead. :mod:`mainspring.ingest.email_imap`
    handles those in Milestone 2.

    Feeds are fetched concurrently but written serially — SQLite takes one
    writer at a time, and the write is microseconds against a network fetch, so
    there is nothing to gain from contending for it.

    Args:
        subs:        Subscriptions to ingest. Defaults to loading
                     subscriptions.yaml.
        allow_fetch: False keeps the whole run offline, including teaser
                     follow-ups.

    Returns:
        An :class:`IngestReport`. A publication that fails is recorded in
        ``errors`` and does not stop the others.
    """
    from ..database import init_db, session

    subscriptions = subs if subs is not None else config.load_subscriptions()
    free = config.free_subscriptions(subscriptions)
    deferred = [s for s in subscriptions if not s.is_free]
    if deferred:
        logger.info(
            "Skipping %d paid publication(s) — email ingestion is not wired up yet: %s",
            len(deferred),
            ", ".join(s.name for s in deferred),
        )

    init_db()
    report = IngestReport(publications=len(free))
    if not free:
        return report

    db = session()
    try:
        publications = sync_publications(db, subscriptions)

        def parse_one(sub: Subscription) -> tuple[Subscription, list[dict] | Exception]:
            try:
                return sub, parse_feed(sub, allow_fetch=allow_fetch)
            except Exception as exc:  # noqa: BLE001 — one bad feed must not end the run
                return sub, exc

        with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
            results = list(pool.map(parse_one, free))

        for sub, outcome in results:
            if isinstance(outcome, Exception):
                logger.error("%s: ingest failed: %s", sub.name, outcome)
                report.errors.append(f"{sub.name}: {outcome}")
                continue

            publication = publications[sub.slug]
            for record in outcome:
                try:
                    action = upsert_article(db, publication, record)
                except Exception as exc:  # noqa: BLE001 — skip the row, keep the feed
                    logger.warning("%s: could not store %s: %s", sub.name, record["url"], exc)
                    db.rollback()
                    report.skipped += 1
                    continue
                setattr(report, action, getattr(report, action) + 1)

            try:
                db.commit()
            except Exception as exc:  # noqa: BLE001 — one feed's batch, not the run
                logger.error("%s: could not commit batch: %s", sub.name, exc)
                db.rollback()
                report.errors.append(f"{sub.name}: commit failed: {exc}")
                continue

            logger.info("%s: %d entries processed", sub.name, len(outcome))
    finally:
        db.close()

    logger.info("Ingest complete — %s", report.summary())
    return report
