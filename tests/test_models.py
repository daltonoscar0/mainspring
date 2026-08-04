"""Tests for the ORM layer — schema constraints and the derived properties.

The constraints here are load-bearing. ``url`` unique is what makes re-ingesting
free; ``(publication_id, slug)`` unique is what stops two posts overwriting each
other's page on disk.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy.exc import IntegrityError

from mainspring.models import Article, DailyEdition, Publication


@pytest.fixture
def publication(db) -> Publication:
    """One free publication, committed."""
    row = Publication(
        name="A Publication",
        slug="a-publication",
        feed_url="https://a.substack.com/feed",
        tier="free",
        homepage_url="https://a.substack.com",
    )
    db.add(row)
    db.commit()
    return row


def make_article(publication: Publication, **overrides) -> Article:
    """Build an Article with sensible defaults, overridable per test."""
    fields = {
        "publication_id": publication.id,
        "title": "A Post",
        "url": "https://a.substack.com/p/a-post",
        "slug": "a-post",
        "author": "A Writer",
        "published_date": datetime(2026, 8, 1, 9, 30),
        "body_html": "<p>Body.</p>",
        "body_text": "Body.",
        "word_count": 1,
        "source_kind": "rss",
    }
    fields.update(overrides)
    return Article(**fields)


class TestPublication:
    """The publications table."""

    def test_defaults(self, db, publication: Publication) -> None:
        assert publication.id is not None
        assert publication.tier == "free"
        assert publication.created_at is not None

    def test_slug_is_unique(self, db, publication: Publication) -> None:
        db.add(
            Publication(
                name="Different Name",
                slug=publication.slug,
                feed_url="https://b.substack.com/feed",
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()

    def test_articles_relationship(self, db, publication: Publication) -> None:
        db.add(make_article(publication))
        db.commit()
        db.refresh(publication)
        assert len(publication.articles) == 1
        assert publication.articles[0].publication is publication


class TestArticle:
    """The articles table."""

    def test_insert_and_defaults(self, db, publication: Publication) -> None:
        article = make_article(publication)
        db.add(article)
        db.commit()

        assert article.id is not None
        assert article.standfirst is None
        assert article.summarizing is False
        assert article.source_kind == "rss"
        assert article.created_at is not None

    def test_url_is_the_dedup_key(self, db, publication: Publication) -> None:
        """Two rows with the same URL must be impossible, whatever else differs."""
        db.add(make_article(publication))
        db.commit()

        db.add(make_article(publication, slug="a-different-slug", title="Different"))
        with pytest.raises(IntegrityError):
            db.commit()

    def test_slug_is_unique_within_a_publication(self, db, publication: Publication) -> None:
        """Slugs become filenames, so a collision would overwrite a page."""
        db.add(make_article(publication))
        db.commit()

        db.add(make_article(publication, url="https://a.substack.com/p/other"))
        with pytest.raises(IntegrityError):
            db.commit()

    def test_the_same_slug_is_fine_across_publications(self, db, publication) -> None:
        """Two publications may both post a "welcome"; the paths differ."""
        other = Publication(
            name="Other", slug="other", feed_url="https://b.substack.com/feed"
        )
        db.add(other)
        db.commit()

        db.add(make_article(publication, slug="welcome", url="https://a.substack.com/p/w"))
        db.add(make_article(other, slug="welcome", url="https://b.substack.com/p/w"))
        db.commit()

        assert db.query(Article).count() == 2

    @pytest.mark.parametrize("missing", ["title", "url", "slug"])
    def test_required_columns(self, db, publication: Publication, missing: str) -> None:
        db.add(make_article(publication, **{missing: None}))
        with pytest.raises(IntegrityError):
            db.commit()

    def test_body_may_be_empty(self, db, publication: Publication) -> None:
        """A post with no extractable body is stored, not rejected."""
        db.add(make_article(publication, body_html=None, body_text=None, word_count=0))
        db.commit()
        assert db.query(Article).one().word_count == 0


class TestEditionDate:
    """``edition_date`` decides which front page an article lands on."""

    def test_uses_the_publication_date(self, db, publication: Publication) -> None:
        article = make_article(publication, published_date=datetime(2026, 8, 1, 23, 59))
        db.add(article)
        db.commit()
        assert article.edition_date == "2026-08-01"

    def test_falls_back_to_ingest_time(self, db, publication: Publication) -> None:
        """An undated post must still land in exactly one edition."""
        article = make_article(
            publication,
            published_date=None,
            created_at=datetime(2026, 7, 15, 4, 0),
        )
        db.add(article)
        db.commit()
        assert article.edition_date == "2026-07-15"


class TestDailyEdition:
    """The daily_editions table records what was published."""

    def test_insert(self, db, publication: Publication) -> None:
        article = make_article(publication)
        db.add(article)
        db.commit()

        edition = DailyEdition(
            date="2026-08-01", lead_article_id=article.id, article_count=1
        )
        db.add(edition)
        db.commit()

        assert edition.built_at is not None
        assert edition.lead_article is article

    def test_date_is_unique(self, db) -> None:
        db.add(DailyEdition(date="2026-08-01", article_count=1))
        db.commit()
        db.add(DailyEdition(date="2026-08-01", article_count=2))
        with pytest.raises(IntegrityError):
            db.commit()

    def test_an_empty_day_needs_no_lead(self, db) -> None:
        edition = DailyEdition(date="2026-08-02", article_count=0)
        db.add(edition)
        db.commit()
        assert edition.lead_article is None


class TestDatabaseModule:
    """Engine and session plumbing."""

    def test_init_db_is_idempotent(self, db) -> None:
        from mainspring.database import init_db

        init_db()
        init_db()
        assert db.query(Publication).count() == 0

    def test_db_path_override_is_honoured(self, tmp_path, monkeypatch) -> None:
        """Tests and a real install must never share a database file."""
        from mainspring import database

        target = tmp_path / "nested" / "custom.db"
        monkeypatch.setenv("DB_PATH", str(target))
        database.reset_engine()
        database.init_db()

        assert target.exists()
        database.reset_engine()

    def test_a_missing_column_is_added_rather_than_breaking_queries(
        self, tmp_path, monkeypatch
    ) -> None:
        """An existing news.db must survive a new column, not have to be deleted.

        Feeds only carry the last twenty-odd posts, so deleting the database
        throws away every older article — it exists nowhere else.
        """
        from sqlalchemy import inspect, text

        from mainspring import database

        monkeypatch.setenv("DB_PATH", str(tmp_path / "upgrade.db"))
        database.reset_engine()
        database.init_db()

        # Simulate the older schema by dropping the column back off.
        engine = database.get_engine()
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE publications DROP COLUMN kind"))
        assert "kind" not in {
            c["name"] for c in inspect(engine).get_columns("publications")
        }

        database.init_db()

        assert "kind" in {c["name"] for c in inspect(engine).get_columns("publications")}
        # And the table is usable again.
        session = database.session()
        session.add(
            Publication(name="X", slug="x", feed_url="https://x.example/feed")
        )
        session.commit()
        assert session.query(Publication).one().kind == "newsletter"
        session.close()
        database.reset_engine()

    def test_get_db_closes_its_session(self, db) -> None:
        from mainspring.database import get_db

        generator = get_db()
        session = next(generator)
        assert session.is_active
        with pytest.raises(StopIteration):
            next(generator)
