"""models.py — SQLAlchemy ORM models.

Three tables:

  publications    — one row per subscription, mirrored from subscriptions.yaml
                    so articles keep a stable foreign key even if the YAML is
                    reordered or a publication is temporarily removed.
  articles        — one row per post. ``url`` is the deduplication key.
  daily_editions  — one row per published edition, recording which article led
                    the front page. Replaces the old ``daily_digests`` table:
                    the digest concept became the built edition.

Schema notes for anyone coming from the_mainspring 1.x:

  * ``Article.summary`` is now ``Article.standfirst`` — the newspaper term for
    the line of context under a headline, which is what it always was.
  * ``Article.full_text`` split into ``body_html`` (sanitized, rendered on the
    article page) and ``body_text`` (plain, fed to the summarizer).
  * ``Article.source`` (a free-text outlet name) became a real foreign key to
    ``publications``.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, relationship

from .database import Base


def _utcnow() -> datetime:
    """Naive UTC timestamp.

    SQLite has no timezone-aware type, so everything is stored naive-UTC and
    compared as such. Keeping one helper means there is a single place where
    that convention is applied.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Publication(Base):
    """A Substack (or any RSS publication) the user subscribes to.

    Rows are upserted from ``subscriptions.yaml`` at the start of every ingest
    by :func:`mainspring.ingest.rss.sync_publications`. Deleting a line from the
    YAML stops future ingestion but leaves the row and its articles in place, so
    the archive never develops holes.
    """

    __tablename__ = "publications"

    id = Column(Integer, primary_key=True, index=True)

    #: Display name, e.g. "Astral Codex Ten". Shown in bylines and section rules.
    name = Column(String(256), nullable=False)

    #: URL-safe identifier. Also the publication page filename
    #: (``publications/<slug>.html``), so it must stay stable.
    slug = Column(String(128), unique=True, nullable=False, index=True)

    #: RSS endpoint. For Substacks, ``<host>/feed``.
    feed_url = Column(String(1024), nullable=False)

    #: "free" (RSS ingestion) or "paid" (email ingestion, Milestone 2).
    tier = Column(String(16), nullable=False, default="free")

    #: Public home page, linked from the publication page.
    homepage_url = Column(String(1024), nullable=True)

    created_at = Column(DateTime, nullable=False, default=_utcnow)

    articles: Mapped[list["Article"]] = relationship(
        "Article",
        back_populates="publication",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return f"<Publication {self.slug!r} tier={self.tier!r}>"


class Article(Base):
    """A single post from one publication.

    Lifecycle:
      1. :mod:`mainspring.ingest.rss` inserts the row with ``body_html`` and
         ``body_text`` populated from the feed's ``content:encoded`` payload
         (Substack ships the whole post in its feed) and ``standfirst=None``.
      2. :func:`mainspring.summarize.generate_standfirsts` fills in
         ``standfirst`` from ``body_text``.
      3. :mod:`mainspring.sitegen.build` renders the row into static HTML.

    Re-ingesting is idempotent: ``url`` is unique, and an existing row is
    refreshed in place rather than duplicated, so a standfirst is never
    regenerated for a post that already has one.
    """

    __tablename__ = "articles"

    id = Column(Integer, primary_key=True, index=True)

    publication_id = Column(
        Integer, ForeignKey("publications.id"), nullable=False, index=True
    )

    #: Headline as published.
    title = Column(String(512), nullable=False)

    #: Canonical post URL. The deduplication key across every ingest path —
    #: RSS today, email tomorrow.
    url = Column(String(1024), unique=True, nullable=False, index=True)

    #: URL-safe identifier derived from the post URL's last path segment. Used
    #: for the article page filename. Unique per publication, not globally:
    #: two publications may both have a post called "welcome".
    slug = Column(String(256), nullable=False, index=True)

    #: Byline from the feed, falling back to the publication name.
    author = Column(String(256), nullable=True)

    #: Publication timestamp, normalized to naive UTC. Nullable because some
    #: feeds omit it.
    published_date = Column(DateTime, nullable=True, index=True)

    #: Sanitized post body — the cleaner's output, rendered on the article page.
    body_html = Column(Text, nullable=True)

    #: Plain-text rendering of the same body, fed to the summarizer.
    body_text = Column(Text, nullable=True)

    #: Word count of ``body_text``. Denormalized because the ranker sorts on it
    #: and recomputing it per build would mean loading every body into memory.
    word_count = Column(Integer, nullable=False, default=0)

    #: Lead image URL, if the post had one. Drives the front page's lead slot.
    hero_image_url = Column(String(2048), nullable=True)
    hero_image_alt = Column(String(512), nullable=True)

    #: The dek under the headline: locally generated, never from a remote API.
    standfirst = Column(Text, nullable=True)

    #: True while a worker is generating this article's standfirst. Prevents two
    #: workers picking up the same row and lets the API report progress.
    summarizing = Column(Boolean, nullable=False, default=False)

    #: Which ingest path produced this row: "rss" or "email".
    source_kind = Column(String(16), nullable=False, default="rss")

    created_at = Column(DateTime, nullable=False, default=_utcnow)

    publication: Mapped[Publication] = relationship(
        "Publication", back_populates="articles"
    )

    __table_args__ = (
        UniqueConstraint("publication_id", "slug", name="uq_article_publication_slug"),
    )

    @property
    def edition_date(self) -> str:
        """The ISO date this article belongs to, for archive grouping.

        Falls back to ``created_at`` when the feed gave no publication date, so
        every article lands in exactly one edition.
        """
        stamp = self.published_date or self.created_at
        return stamp.date().isoformat()

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return f"<Article {self.id} {self.title[:40]!r}>"


class DailyEdition(Base):
    """A built edition: one calendar day's front page.

    Written by :mod:`mainspring.sitegen.build` after rendering. The
    ``lead_article_id`` history is what makes lead-story rotation possible —
    the ranker reads it to find the publication that has gone longest without
    leading.
    """

    __tablename__ = "daily_editions"

    id = Column(Integer, primary_key=True, index=True)

    #: ISO calendar date (YYYY-MM-DD). One edition per day.
    date = Column(String(10), unique=True, nullable=False, index=True)

    #: The article given the lead slot. Nullable for an empty news day.
    lead_article_id = Column(
        Integer, ForeignKey("articles.id"), nullable=True, index=True
    )

    #: How many articles this edition carried.
    article_count = Column(Integer, nullable=False, default=0)

    #: When the edition was last rendered. Updated on every rebuild.
    built_at = Column(DateTime, nullable=False, default=_utcnow)

    lead_article: Mapped[Article | None] = relationship("Article")

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return f"<DailyEdition {self.date} n={self.article_count}>"
