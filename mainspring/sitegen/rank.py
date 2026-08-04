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

#: Stand-in for a missing publication date when sorting newest-first. Any real
#: date sorts above it, so undated articles fall to the bottom of the river
#: rather than the top.
_NO_DATE = datetime.min


def _by_length(article: Any) -> tuple:
    """Sort key: longest first, then most recent, then URL.

    The URL tiebreak is what makes the ordering total. Two articles with the
    same word count and timestamp would otherwise sort by list position, which
    depends on the order the database happened to return them in.
    """
    return (
        -int(getattr(article, "word_count", 0) or 0),
        -(article.published_date or _NO_DATE).timestamp(),
        str(article.url),
    )


def _by_recency(article: Any) -> tuple:
    """Sort key: most recent first, then longest, then URL."""
    return (
        -(article.published_date or _NO_DATE).timestamp(),
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
    lead_history: dict[str, str] | None = None,
    secondary_count: int = DEFAULT_SECONDARY_COUNT,
    limit: int | None = None,
) -> Ranking:
    """Assign articles to front page slots.

    Args:
        articles:        Every article eligible for this edition. Not mutated.
        lead_history:    Publication slug to the ISO date it last led, driving
                         lead rotation. See :func:`choose_lead`.
        secondary_count: How many medium-headline slots to fill. Fewer are used
                         if there are not enough articles.
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

    lead = choose_lead(remaining, lead_history)
    remaining = [a for a in remaining if a is not lead]

    # Secondaries prefer one story per publication. Without this, a publication
    # that posted three long essays in a day takes the lead and every secondary
    # slot, and the top of the page stops looking like a front page.
    secondaries: list[Any] = []
    if secondary_count > 0:
        spent = {publication_slug(lead)} if lead is not None else set()
        for article in sorted(remaining, key=_by_length):
            if len(secondaries) >= secondary_count:
                break
            slug = publication_slug(article)
            if slug in spent:
                continue
            secondaries.append(article)
            spent.add(slug)

        # Not enough distinct publications to fill the slots — fall back to raw
        # length so the layout is never left with a hole.
        if len(secondaries) < secondary_count:
            placed = {id(a) for a in secondaries}
            for article in sorted(remaining, key=_by_length):
                if len(secondaries) >= secondary_count:
                    break
                if id(article) not in placed:
                    secondaries.append(article)
                    placed.add(id(article))
            secondaries.sort(key=_by_length)

        chosen = {id(a) for a in secondaries}
        remaining = [a for a in remaining if id(a) not in chosen]

    river = sorted(remaining, key=_by_recency)

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
        newest = group[0].published_date or _NO_DATE
        return (-newest.timestamp(), slug)

    return sorted(grouped.items(), key=group_key)
