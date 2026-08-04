"""Tests for RSS ingestion — parsing feeds and upserting articles.

Every test here runs offline. Feeds come from files, and any ingest call passes
``allow_fetch=False`` so a teaser entry never triggers a post fetch.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from mainspring.config import Subscription, SubscriptionsError, parse_subscriptions
from mainspring.ingest import rss
from mainspring.models import Article, Publication


class TestParseFeed:
    """A saved feed must become article records with the right shape."""

    def test_records_are_produced(self, subscription, construction_physics_feed) -> None:
        records = rss.parse_feed(
            subscription, feed_source=construction_physics_feed, allow_fetch=False
        )
        assert len(records) == 3

    def test_every_required_field_is_present(
        self, subscription, construction_physics_feed
    ) -> None:
        records = rss.parse_feed(
            subscription, feed_source=construction_physics_feed, allow_fetch=False
        )
        required = {
            "title", "url", "slug", "author", "published_date",
            "body_html", "body_text", "word_count",
            "hero_image_url", "hero_image_alt", "source_kind",
        }
        for record in records:
            assert required <= record.keys()

    def test_full_bodies_come_from_content_encoded(
        self, subscription, construction_physics_feed
    ) -> None:
        """Substack ships whole posts in the feed, so no fetch should be needed."""
        records = rss.parse_feed(
            subscription, feed_source=construction_physics_feed, allow_fetch=False
        )
        assert all(r["word_count"] > rss.TEASER_WORD_THRESHOLD for r in records)

    def test_titles_and_urls_are_real(self, subscription, construction_physics_feed) -> None:
        records = rss.parse_feed(
            subscription, feed_source=construction_physics_feed, allow_fetch=False
        )
        for record in records:
            assert record["title"] and record["title"] != "Untitled"
            assert record["url"].startswith("https://www.construction-physics.com/p/")
            assert record["slug"] and "/" not in record["slug"]

    def test_dates_are_naive_utc_datetimes(
        self, subscription, construction_physics_feed
    ) -> None:
        records = rss.parse_feed(
            subscription, feed_source=construction_physics_feed, allow_fetch=False
        )
        for record in records:
            published = record["published_date"]
            assert isinstance(published, datetime)
            assert published.tzinfo is None

    def test_author_comes_from_the_feed(
        self, subscription, construction_physics_feed
    ) -> None:
        records = rss.parse_feed(
            subscription, feed_source=construction_physics_feed, allow_fetch=False
        )
        assert records[0]["author"] == "Brian Potter"

    def test_author_falls_back_to_the_publication_name(self, subscription) -> None:
        feed = """<?xml version="1.0"?><rss version="2.0"><channel>
        <item><title>No byline</title><link>https://x.example/p/a</link>
        <description>{}</description></item>
        </channel></rss>""".format("word " * 200)
        records = rss.parse_feed(subscription, feed_source=feed, allow_fetch=False)
        assert records[0]["author"] == subscription.name

    def test_source_kind_is_rss(self, subscription, construction_physics_feed) -> None:
        records = rss.parse_feed(
            subscription, feed_source=construction_physics_feed, allow_fetch=False
        )
        assert {r["source_kind"] for r in records} == {"rss"}

    def test_a_second_publication_parses_too(
        self, astral_codex_ten_feed
    ) -> None:
        sub = Subscription(
            name="Astral Codex Ten",
            slug="astral-codex-ten",
            feed_url="https://www.astralcodexten.com/feed",
        )
        records = rss.parse_feed(sub, feed_source=astral_codex_ten_feed, allow_fetch=False)
        assert len(records) == 3
        assert all(r["word_count"] > 100 for r in records)

    def test_entries_without_a_link_are_skipped(self, subscription) -> None:
        feed = """<?xml version="1.0"?><rss version="2.0"><channel>
        <item><title>No link</title><description>Body text here.</description></item>
        <item><title>Has link</title><link>https://x.example/p/a</link>
        <description>{}</description></item>
        </channel></rss>""".format("word " * 200)
        records = rss.parse_feed(subscription, feed_source=feed, allow_fetch=False)
        assert len(records) == 1
        assert records[0]["title"] == "Has link"

    def test_entries_that_clean_to_nothing_are_skipped(self, subscription) -> None:
        feed = """<?xml version="1.0"?><rss version="2.0"><channel>
        <item><title>Chrome only</title><link>https://x.example/p/a</link>
        <description>&lt;div class="subscription-widget"&gt;&lt;p&gt;Subscribe now&lt;/p&gt;&lt;/div&gt;</description>
        </item></channel></rss>"""
        records = rss.parse_feed(subscription, feed_source=feed, allow_fetch=False)
        assert records == []

    def test_entries_are_capped_per_feed(self, subscription, monkeypatch) -> None:
        """An excerpt feed with 100 entries would otherwise cost 100 page fetches."""
        from mainspring import config as cfg

        monkeypatch.setattr(cfg, "MAX_ENTRIES_PER_FEED", 2)
        items = "".join(
            f"<item><title>Post {i}</title><link>https://x.example/p/{i}</link>"
            f"<description>{'word ' * 200}</description></item>"
            for i in range(10)
        )
        feed = f'<?xml version="1.0"?><rss version="2.0"><channel>{items}</channel></rss>'
        records = rss.parse_feed(subscription, feed_source=feed, allow_fetch=False)
        assert len(records) == 2

    def test_the_cap_keeps_the_newest_entries(self, subscription, monkeypatch) -> None:
        """Feeds are newest-first, so the cap must not drop today's news."""
        from mainspring import config as cfg

        monkeypatch.setattr(cfg, "MAX_ENTRIES_PER_FEED", 1)
        feed = (
            '<?xml version="1.0"?><rss version="2.0"><channel>'
            f'<item><title>Newest</title><link>https://x.example/p/new</link>'
            f'<description>{"word " * 200}</description></item>'
            f'<item><title>Older</title><link>https://x.example/p/old</link>'
            f'<description>{"word " * 200}</description></item>'
            "</channel></rss>"
        )
        records = rss.parse_feed(subscription, feed_source=feed, allow_fetch=False)
        assert [r["title"] for r in records] == ["Newest"]

    def test_unparseable_feed_raises(self, subscription) -> None:
        with pytest.raises(ValueError, match="unparseable feed"):
            rss.parse_feed(subscription, feed_source="<<< not xml", allow_fetch=False)

    def test_a_teaser_is_kept_when_fetching_is_disabled(self, subscription) -> None:
        """Offline, a short body is still better than dropping the story."""
        feed = """<?xml version="1.0"?><rss version="2.0"><channel>
        <item><title>Teaser</title><link>https://x.example/p/a</link>
        <description>A short teaser paragraph with enough words to survive cleaning
        but far fewer than a whole post would carry.</description></item>
        </channel></rss>"""
        records = rss.parse_feed(subscription, feed_source=feed, allow_fetch=False)
        assert len(records) == 1
        assert records[0]["word_count"] < rss.TEASER_WORD_THRESHOLD


class TestSyncPublications:
    """subscriptions.yaml is mirrored into the publications table."""

    def test_publications_are_created(self, db, subscription, paid_subscription) -> None:
        result = rss.sync_publications(db, [subscription, paid_subscription])
        assert set(result) == {"construction-physics", "a-paid-publication"}
        assert db.query(Publication).count() == 2

    def test_repeat_sync_updates_rather_than_duplicates(self, db, subscription) -> None:
        rss.sync_publications(db, [subscription])
        renamed = Subscription(
            name="Construction Physics Renamed",
            slug=subscription.slug,
            feed_url="https://new.example.com/feed",
            tier="paid",
        )
        rss.sync_publications(db, [renamed])

        assert db.query(Publication).count() == 1
        row = db.query(Publication).one()
        assert row.name == "Construction Physics Renamed"
        assert row.feed_url == "https://new.example.com/feed"
        assert row.tier == "paid"

    def test_removing_a_subscription_leaves_its_row(self, db, subscription) -> None:
        """The archive must not develop holes when the YAML changes."""
        rss.sync_publications(db, [subscription])
        rss.sync_publications(db, [])
        assert db.query(Publication).count() == 1


class TestUpsertArticle:
    """URL is the deduplication key across every ingest path."""

    @pytest.fixture
    def publication(self, db, subscription) -> Publication:
        return rss.sync_publications(db, [subscription])[subscription.slug]

    @pytest.fixture
    def record(self) -> dict:
        return {
            "title": "A Post",
            "url": "https://x.example/p/a-post",
            "slug": "a-post",
            "author": "A Writer",
            "published_date": datetime(2026, 8, 1, 12, 0),
            "body_html": "<p>Body.</p>",
            "body_text": "Body.",
            "word_count": 1,
            "hero_image_url": None,
            "hero_image_alt": None,
            "source_kind": "rss",
        }

    def test_first_sighting_inserts(self, db, publication, record) -> None:
        assert rss.upsert_article(db, publication, record) == "inserted"
        db.commit()
        assert db.query(Article).count() == 1

    def test_second_sighting_of_unchanged_post_is_skipped(
        self, db, publication, record
    ) -> None:
        rss.upsert_article(db, publication, record)
        db.commit()
        assert rss.upsert_article(db, publication, record) == "skipped"
        db.commit()
        assert db.query(Article).count() == 1

    def test_an_existing_standfirst_is_not_discarded(self, db, publication, record) -> None:
        rss.upsert_article(db, publication, record)
        db.commit()
        article = db.query(Article).one()
        article.standfirst = "A dek that cost real compute."
        db.commit()

        rss.upsert_article(db, publication, record)
        db.commit()
        assert db.query(Article).one().standfirst == "A dek that cost real compute."

    def test_an_edited_post_is_refreshed_and_its_standfirst_cleared(
        self, db, publication, record
    ) -> None:
        """A rewritten post needs a new dek, because the old one describes the old text."""
        rss.upsert_article(db, publication, record)
        db.commit()
        article = db.query(Article).one()
        article.standfirst = "Describes the original."
        db.commit()

        edited = {**record, "body_text": "Rewritten body.", "body_html": "<p>Rewritten.</p>"}
        assert rss.upsert_article(db, publication, edited) == "updated"
        db.commit()

        article = db.query(Article).one()
        assert article.body_text == "Rewritten body."
        assert article.standfirst is None

    def test_metadata_is_refreshed_without_a_body_change(
        self, db, publication, record
    ) -> None:
        rss.upsert_article(db, publication, record)
        db.commit()
        rss.upsert_article(db, publication, {**record, "title": "A Retitled Post"})
        db.commit()
        assert db.query(Article).one().title == "A Retitled Post"

    def test_colliding_slugs_are_disambiguated(self, db, publication, record) -> None:
        """Substack truncates long titles, so two posts can share a path segment."""
        rss.upsert_article(db, publication, record)
        second = {**record, "url": "https://x.example/p/a-post-2", "title": "Another"}
        rss.upsert_article(db, publication, second)
        db.commit()

        slugs = {a.slug for a in db.query(Article).all()}
        assert slugs == {"a-post", "a-post-2"}

    def test_a_repeat_sighting_keeps_its_slug(self, db, publication, record) -> None:
        rss.upsert_article(db, publication, record)
        db.commit()
        rss.upsert_article(db, publication, record)
        db.commit()
        assert db.query(Article).one().slug == "a-post"


class TestIngest:
    """The full ingest path, driven from saved feeds."""

    @pytest.fixture
    def offline_feeds(self, monkeypatch, construction_physics_feed, astral_codex_ten_feed):
        """Serve each subscription its fixture instead of making a request."""
        feeds = {
            "https://www.construction-physics.com/feed": construction_physics_feed,
            "https://www.astralcodexten.com/feed": astral_codex_ten_feed,
        }
        original = rss.parse_feed

        def patched(subscription, *, feed_source=None, allow_fetch=True):
            return original(
                subscription,
                feed_source=feeds[subscription.feed_url],
                allow_fetch=False,
            )

        monkeypatch.setattr(rss, "parse_feed", patched)
        return [
            Subscription(
                name="Construction Physics",
                slug="construction-physics",
                feed_url="https://www.construction-physics.com/feed",
            ),
            Subscription(
                name="Astral Codex Ten",
                slug="astral-codex-ten",
                feed_url="https://www.astralcodexten.com/feed",
            ),
        ]

    def test_ingest_stores_articles(self, db, offline_feeds) -> None:
        report = rss.ingest(offline_feeds, allow_fetch=False)
        assert report.ok
        assert report.publications == 2
        assert report.inserted == 6
        assert db.query(Article).count() == 6

    def test_re_ingest_creates_no_duplicates(self, db, offline_feeds) -> None:
        rss.ingest(offline_feeds, allow_fetch=False)
        before = db.query(Article).count()

        second = rss.ingest(offline_feeds, allow_fetch=False)
        assert second.inserted == 0
        assert second.skipped == before
        assert db.query(Article).count() == before

    def test_articles_are_linked_to_their_publication(self, db, offline_feeds) -> None:
        rss.ingest(offline_feeds, allow_fetch=False)
        articles = db.query(Article).all()
        assert articles
        for article in articles:
            assert article.publication is not None
            assert article.publication.slug in {"construction-physics", "astral-codex-ten"}
            # The post URL must belong to the publication it was filed under.
            assert article.publication.feed_url.startswith(
                article.url.split("/p/")[0]
            )

    def test_urls_are_unique_across_publications(self, db, offline_feeds) -> None:
        rss.ingest(offline_feeds, allow_fetch=False)
        urls = [a.url for a in db.query(Article).all()]
        assert len(urls) == len(set(urls))

    def test_paid_publications_are_skipped(self, db, offline_feeds, paid_subscription) -> None:
        report = rss.ingest([*offline_feeds, paid_subscription], allow_fetch=False)
        assert report.publications == 2  # only the free ones were polled
        assert report.ok
        # The row still exists so the site can list it as awaiting email ingest.
        assert db.query(Publication).filter_by(slug="a-paid-publication").count() == 1

    def test_one_broken_feed_does_not_stop_the_others(
        self, db, monkeypatch, construction_physics_feed
    ) -> None:
        good = Subscription(name="Good", slug="good", feed_url="https://good.example/feed")
        bad = Subscription(name="Bad", slug="bad", feed_url="https://bad.example/feed")

        original = rss.parse_feed

        def patched(subscription, *, feed_source=None, allow_fetch=True):
            if subscription.slug == "bad":
                raise ValueError("Bad: unparseable feed")
            return original(
                subscription, feed_source=construction_physics_feed, allow_fetch=False
            )

        monkeypatch.setattr(rss, "parse_feed", patched)

        report = rss.ingest([good, bad], allow_fetch=False)
        assert not report.ok
        assert len(report.errors) == 1
        assert "Bad" in report.errors[0]
        assert report.inserted == 3  # the good feed still landed

    def test_no_subscriptions_is_not_an_error(self, db) -> None:
        report = rss.ingest([], allow_fetch=False)
        assert report.ok
        assert report.inserted == 0

    def test_report_summary_reads_cleanly(self, db, offline_feeds) -> None:
        report = rss.ingest(offline_feeds, allow_fetch=False)
        assert "2 publications" in report.summary()
        assert "6 new" in report.summary()


class TestSubscriptionsConfig:
    """subscriptions.yaml is user-edited, so validation errors must be legible."""

    def test_a_valid_document_parses(self) -> None:
        subs = parse_subscriptions(
            {
                "publications": [
                    {
                        "name": "A Publication",
                        "slug": "a-publication",
                        "feed_url": "https://a.substack.com/feed",
                    }
                ]
            }
        )
        assert len(subs) == 1
        assert subs[0].tier == "free"
        assert subs[0].is_free
        # Homepage defaults to the feed URL with /feed removed.
        assert subs[0].homepage_url == "https://a.substack.com"

    def test_tier_defaults_to_free_and_paid_is_accepted(self) -> None:
        subs = parse_subscriptions(
            {
                "publications": [
                    {"name": "F", "slug": "f", "feed_url": "https://f.example/feed"},
                    {
                        "name": "P",
                        "slug": "p",
                        "feed_url": "https://p.example/feed",
                        "tier": "paid",
                    },
                ]
            }
        )
        assert [s.tier for s in subs] == ["free", "paid"]
        assert [s.is_free for s in subs] == [True, False]

    @pytest.mark.parametrize(
        ("document", "message"),
        [
            ([], "mapping at the top level"),
            ({}, "non-empty list"),
            ({"publications": []}, "non-empty list"),
            ({"publications": ["not a mapping"]}, "must be a mapping"),
            ({"publications": [{"slug": "s", "feed_url": "u"}]}, "'name'"),
            ({"publications": [{"name": "n", "feed_url": "u"}]}, "'slug'"),
            ({"publications": [{"name": "n", "slug": "s"}]}, "'feed_url'"),
            (
                {"publications": [{"name": "n", "slug": "s", "feed_url": "u", "tier": "x"}]},
                "invalid tier",
            ),
        ],
    )
    def test_invalid_documents_are_rejected(self, document, message) -> None:
        with pytest.raises(SubscriptionsError, match=message):
            parse_subscriptions(document)

    def test_duplicate_slugs_are_rejected(self) -> None:
        """Two publications sharing a slug would overwrite each other's page."""
        with pytest.raises(SubscriptionsError, match="reuses slug"):
            parse_subscriptions(
                {
                    "publications": [
                        {"name": "One", "slug": "same", "feed_url": "https://a.example/feed"},
                        {"name": "Two", "slug": "same", "feed_url": "https://b.example/feed"},
                    ]
                }
            )

    def test_fetch_pages_defaults_to_true_and_accepts_false(self) -> None:
        subs = parse_subscriptions(
            {
                "publications": [
                    {"name": "A", "slug": "a", "feed_url": "https://a.example/feed"},
                    {
                        "name": "B",
                        "slug": "b",
                        "feed_url": "https://b.example/feed",
                        "fetch_pages": False,
                    },
                ]
            }
        )
        assert [s.fetch_pages for s in subs] == [True, False]

    def test_non_boolean_fetch_pages_is_rejected(self) -> None:
        with pytest.raises(SubscriptionsError, match="non-boolean fetch_pages"):
            parse_subscriptions(
                {
                    "publications": [
                        {
                            "name": "A",
                            "slug": "a",
                            "feed_url": "https://a.example/feed",
                            "fetch_pages": "no",
                        }
                    ]
                }
            )

    def test_fetch_pages_false_keeps_a_teaser_instead_of_fetching(self) -> None:
        """The publication's own switch must hold even when the run allows fetching.

        The network is blocked by an autouse fixture, so a fetch attempt here
        fails the test outright — which is exactly the assertion.
        """
        sub = Subscription(
            name="Feed Only",
            slug="feed-only",
            feed_url="https://f.example/feed",
            fetch_pages=False,
        )
        feed = """<?xml version="1.0"?><rss version="2.0"><channel>
        <item><title>Teaser</title><link>https://f.example/p/a</link>
        <description>A short teaser paragraph that is well under the threshold at
        which the ingester would normally go and fetch the post page.</description>
        </item></channel></rss>"""
        records = rss.parse_feed(sub, feed_source=feed, allow_fetch=True)
        assert len(records) == 1
        assert records[0]["word_count"] < rss.TEASER_WORD_THRESHOLD

    def test_the_shipped_subscriptions_file_is_valid(self) -> None:
        """The seed list committed to the repo must actually load."""
        from mainspring import config

        subs = config.load_subscriptions()
        assert len(subs) >= 4
        assert all(s.feed_url.startswith("https://") for s in subs)
        assert len({s.slug for s in subs}) == len(subs)


class TestEmailStub:
    """The Milestone 2 module must be importable and honest about its state."""

    def test_import_does_not_require_imaplib_configuration(self) -> None:
        from mainspring.ingest import email_imap

        assert email_imap.ENV_HOST == "MAINSPRING_IMAP_HOST"

    def test_ingest_raises_not_implemented(self) -> None:
        from mainspring.ingest import email_imap

        with pytest.raises(NotImplementedError, match="Milestone 2"):
            email_imap.ingest()

    def test_parse_raises_not_implemented(self) -> None:
        from mainspring.ingest import email_imap

        message = email_imap.RawMessage(message_id="<a@b>", uid=1, raw=b"")
        with pytest.raises(NotImplementedError, match="Milestone 2"):
            email_imap.parse_substack_email(message)

    def test_mailbox_source_raises_not_implemented(self) -> None:
        from mainspring.ingest import email_imap

        source = email_imap.ImapMailboxSource(email_imap.MailboxConfig(host="imap.example.com"))
        with pytest.raises(NotImplementedError, match="Milestone 2"):
            list(source.fetch_since())

    def test_mailbox_config_defaults_to_read_only_tls(self) -> None:
        from mainspring.ingest import email_imap

        config = email_imap.MailboxConfig(host="imap.example.com")
        assert config.read_only is True
        assert config.port == 993
