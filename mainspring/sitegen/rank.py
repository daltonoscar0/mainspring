"""rank.py — decide the front page's headline hierarchy.

A newspaper front page is not a list. One story gets the big headline, two or
three get medium ones, and the rest run as a river of shorter items. This module
makes that assignment.

Version 1 is deliberately simple and completely deterministic:

  **Lead** — the longest article from whichever publication has gone longest
  without leading. Length stands in for ambition, and the rotation is what stops
  the most prolific publication from owning the front page every day. A reader
  subscribed to six writers should see six writers over a week.

  **Secondaries** — the longest remaining articles, preferring one per
  publication so the top of the page shows a spread of voices rather than three
  more pieces from the lead's author.

  **River** — everything else, most recent first.

Fresh versus held
-----------------
Six Substacks do not file six stories a day. On most days one or two posts
arrive, which is not a front page. So an edition is built from a window of the
recent wire — see ``EDITION_WINDOW_DAYS`` — while the ``fresh`` argument marks
the articles that are actually new that day. The lead and the secondaries come
from the fresh set whenever it can supply them, so the top of the page is always
news; the held stories fill the slots and the river below. That is what a real
paper does on a slow day, and it is why a front page is never one story wide.

Everything here is a pure function of its arguments. No database, no clock, no
filesystem: given the same articles and the same lead history it produces the
same ranking, so a rebuild yields no diff. That also makes it cheap to replace —
swap :func:`rank_articles` for something that reads engagement or topic
clustering and nothing else in the codebase changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, Sequence, runtime_checkable

#: How many medium-headline stories sit under the lead by default.
DEFAULT_SECONDARY_COUNT = 3


@runtime_checkable
class Rankable(Protocol):
    """The subset of an article the ranker looks at.

    :class:`mainspring.models.Article` satisfies this, and so does any plain
    object with these attributes — which is how the tests rank fixtures without
    a database.
    """

    url: str
    title: str
    word_count: int
    published_date: datetime | None


def publication_slug(article: Any) -> str:
    """Return an article's publication slug.

    Accepts either a loaded ORM object (``article.publication.slug``) or a plain
    object carrying ``publication_slug`` directly, so ranking never forces a
    relationship load and never requires a real ``Publication`` row.
    """
    publication = getattr(article, "publication", None)
    if publication is not None:
        slug = getattr(publication, "slug", None)
        if slug:
            return str(slug)
    return str(getattr(article, "publication_slug", "") or "unknown")


@dataclass(frozen=True)
class Ranking:
    """The front page layout for one edition.

    Attributes:
        lead:        The single largest story, or None if there are no articles.
        secondaries: Medium-headline stories under the lead, longest first.
        river:       Everything remaining, most recent first.
    """

    lead: Any | None = None
    secondaries: list[Any] = field(default_factory=list)
    river: list[Any] = field(default_factory=list)

    @property
    def all_articles(self) -> list[Any]:
        """Every ranked article in page order. Useful for counts and archives."""
        return ([self.lead] if self.lead is not None else []) + self.secondaries + self.river

    @property
    def is_empty(self) -> bool:
        """True when there was nothing to publish."""
        return self.lead is None and not self.secondaries and not self.river


# ---------------------------------------------------------------------------
# Sort keys
# ---------------------------------------------------------------------------

def _seconds(stamp: datetime | None) -> float:
    """Return a sortable number of seconds for a naive datetime.

    Computed from the proleptic ordinal rather than via
    :meth:`datetime.timestamp`, which converts through local time and raises
    ``ValueError`` for dates outside the platform's epoch range — including the
    sentinel a missing date would otherwise need.

    A missing date returns negative infinity, so undated articles sort to the
    bottom of a newest-first list rather than the top.
    """
    if stamp is None:
        return float("-inf")
    return (
        stamp.toordinal() * 86400.0
        + stamp.hour * 3600
        + stamp.minute * 60
        + stamp.second
        + stamp.microsecond / 1_000_000
    )


def _by_length(article: Any) -> tuple:
    """Sort key: longest first, then most recent, then URL.

    The URL tiebreak is what makes the ordering total. Two articles with the
    same word count and timestamp would otherwise sort by list position, which
    depends on the order the database happened to return them in.
    """
    return (
        -int(getattr(article, "word_count", 0) or 0),
        -_seconds(article.published_date),
        str(article.url),
    )


def _by_recency(article: Any) -> tuple:
    """Sort key: most recent first, then longest, then URL."""
    return (
        -_seconds(article.published_date),
        -int(getattr(article, "word_count", 0) or 0),
        str(article.url),
    )


# ---------------------------------------------------------------------------
# Lead selection
# ---------------------------------------------------------------------------

def _lead_priority(slug: str, lead_history: dict[str, str]) -> tuple:
    """Sort key for choosing which publication leads.

    A publication that has never led sorts first — a new subscription gets the
    front page immediately rather than waiting out the rotation. After that,
    whoever led longest ago goes next. The slug breaks ties so two publications
    that last led on the same date always resolve the same way.
    """
    last_led = lead_history.get(slug)
    return (last_led is not None, last_led or "", slug)


def choose_lead(
    articles: Sequence[Any],
    lead_history: dict[str, str] | None = None,
) -> Any | None:
    """Pick the lead story.

    Args:
        articles:      Candidates for this edition.
        lead_history:  Maps publication slug to the ISO date that publication
                       most recently led. Publications absent from the mapping
                       are treated as never having led. Pass ``{}`` or None to
                       disable rotation, in which case the longest article
                       overall leads.

    Returns:
        The chosen article, or None if ``articles`` is empty.
    """
    if not articles:
        return None

    history = lead_history or {}

    by_publication: dict[str, list[Any]] = {}
    for article in articles:
        by_publication.setdefault(publication_slug(article), []).append(article)

    winner = min(by_publication, key=lambda slug: _lead_priority(slug, history))
    return min(by_publication[winner], key=_by_length)


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def rank_articles(
    articles: Sequence[Any],
    *,
    fresh: Sequence[Any] | None = None,
    lead_history: dict[str, str] | None = None,
    secondary_count: int = DEFAULT_SECONDARY_COUNT,
    per_publication: int | None = None,
    limit: int | None = None,
) -> Ranking:
    """Assign articles to front page slots.

    Args:
        articles:        Every article eligible for this edition — the whole
                         window. Not mutated.
        fresh:           The subset published on the edition's own date. The
                         lead and secondaries are drawn from here first, so the
                         top of the page is news rather than the longest thing in
                         the window. None means treat everything as fresh.
        lead_history:    Publication slug to the ISO date it last led, driving
                         lead rotation. See :func:`choose_lead`.
        secondary_count: How many medium-headline slots to fill. Fewer are used
                         if there are not enough articles.
        per_publication: Most river slots any one publication may take. None for
                         no cap. A wire service posting hourly will otherwise
                         fill the river and push everything else off the page —
                         the lead rotation guarantees a spread across headlines,
                         and this does the same below them. Each publication
                         keeps its most recent items.
        limit:           Cap on total articles placed. Extras are dropped from
                         the tail of the river, keeping the front page a front
                         page — the archive and publication pages carry the rest.

    Returns:
        A :class:`Ranking`. Every input article appears in exactly one slot
        unless dropped by ``limit``.
    """
    remaining = list(articles)
    if not remaining:
        return Ranking()

    fresh_urls = {str(a.url) for a in fresh} if fresh is not None else None

    def is_fresh(article: Any) -> bool:
        return fresh_urls is None or str(article.url) in fresh_urls

    fresh_pool = [a for a in remaining if is_fresh(a)]
    lead = choose_lead(fresh_pool or remaining, lead_history)
    remaining = [a for a in remaining if a is not lead]

    # Secondaries prefer one story per publication. Without this, a publication
    # that posted three long essays in a day takes the lead and every secondary
    # slot, and the top of the page stops looking like a front page.
    #
    # Fresh stories are offered every slot before any held story is considered,
    # so a busy news day never buries today's writing under last week's.
    secondaries: list[Any] = []
    if secondary_count > 0:
        placed: set[int] = set()

        def fill(candidates: list[Any], *, distinct: bool) -> None:
            """Take from ``candidates``, longest first, until the slots are full."""
            for article in candidates:
                if len(secondaries) >= secondary_count:
                    return
                if id(article) in placed:
                    continue
                if distinct and publication_slug(article) in spent:
                    continue
                secondaries.append(article)
                placed.add(id(article))
                spent.add(publication_slug(article))

        spent = {publication_slug(lead)} if lead is not None else set()
        by_length = sorted(remaining, key=_by_length)
        fresh_by_length = [a for a in by_length if is_fresh(a)]
        held_by_length = [a for a in by_length if not is_fresh(a)]

        # One per publication first, fresh before held. Then relax the
        # one-per-publication rule rather than leave a hole in the layout.
        fill(fresh_by_length, distinct=True)
        fill(held_by_length, distinct=True)
        fill(fresh_by_length, distinct=False)
        fill(held_by_length, distinct=False)

        secondaries.sort(key=_by_length)
        remaining = [a for a in remaining if id(a) not in placed]

    river = sorted(remaining, key=_by_recency)

    if per_publication is not None and per_publication > 0:
        # Newest-first already, so taking the first N per publication keeps each
        # one's most recent items and preserves the overall recency order.
        seen: dict[str, int] = {}
        capped: list[Any] = []
        for article in river:
            slug = publication_slug(article)
            if seen.get(slug, 0) >= per_publication:
                continue
            seen[slug] = seen.get(slug, 0) + 1
            capped.append(article)
        river = capped

    if limit is not None:
        headline_count = (1 if lead is not None else 0) + len(secondaries)
        river = river[: max(0, limit - headline_count)]

    return Ranking(lead=lead, secondaries=secondaries, river=river)


def group_by_publication(articles: Sequence[Any]) -> list[tuple[str, list[Any]]]:
    """Group river articles under their publication, for the multi-column river.

    Publications are ordered by their most recent article so the freshest
    section heads the river; articles within a publication are ordered by
    recency. Deterministic throughout — ties break on slug, then URL.

    Returns:
        A list of ``(publication_slug, articles)`` pairs.
    """
    grouped: dict[str, list[Any]] = {}
    for article in articles:
        grouped.setdefault(publication_slug(article), []).append(article)

    for group in grouped.values():
        group.sort(key=_by_recency)

    def group_key(item: tuple[str, list[Any]]) -> tuple:
        slug, group = item
        return (-_seconds(group[0].published_date), slug)

    return sorted(grouped.items(), key=group_key)
