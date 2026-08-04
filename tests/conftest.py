"""Shared fixtures.

Two rules the whole suite obeys:

  * **No network.** Feeds come from files under ``fixtures/``, and any ingest
    call passes ``allow_fetch=False``. The ``no_network`` fixture is autouse and
    fails a test that tries to open a socket anyway, so a regression that adds a
    request cannot pass quietly.
  * **No model.** ``DEV_MODE=1`` is set for every test, so ``transformers`` and
    ``torch`` are never imported. The suite runs on a machine with neither
    installed and no GPU.
"""

from __future__ import annotations

import os
import socket
from datetime import datetime
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def dev_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force dev mode so no test can reach for the model."""
    monkeypatch.setenv("DEV_MODE", "1")


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any outbound socket connection a test failure.

    Blunt on purpose. If a code path grows a network call, the test that covers
    it should say so rather than quietly depending on the machine being online.
    """

    def blocked(*args: object, **kwargs: object):
        raise AssertionError(
            "This test attempted a network connection. Tests must run offline — "
            "use a fixture under tests/fixtures/ or pass allow_fetch=False."
        )

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """An empty database in a temporary directory, torn down afterwards."""
    from mainspring import database

    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    database.reset_engine()
    database.init_db()

    session = database.session()
    try:
        yield session
    finally:
        session.close()
        database.reset_engine()


# ---------------------------------------------------------------------------
# Fixture files
# ---------------------------------------------------------------------------

@pytest.fixture
def construction_physics_feed() -> bytes:
    """A real Substack RSS feed, trimmed to three entries.

    Captured from https://www.construction-physics.com/feed. Full posts in
    ``content:encoded``, which is the case the RSS ingester is built around.
    """
    return (FIXTURES / "construction_physics_feed.xml").read_bytes()


@pytest.fixture
def astral_codex_ten_feed() -> bytes:
    """A second real Substack feed, for cross-publication tests."""
    return (FIXTURES / "astral_codex_ten_feed.xml").read_bytes()


@pytest.fixture
def substack_post_page() -> str:
    """A real Substack post page, for the fetch-the-post fallback path."""
    return (FIXTURES / "substack_post_page.html").read_text(encoding="utf-8")


@pytest.fixture
def substack_chrome_body() -> str:
    """A post body carrying every kind of Substack furniture.

    Hand-written from observed markup, because no single real post has a
    paywall, an email footer and a poll embed at once. See the comment at the
    top of the file.
    """
    return (FIXTURES / "substack_chrome_body.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Ranking fixtures
# ---------------------------------------------------------------------------

class FakeArticle:
    """A minimal stand-in for an Article, for ranking tests.

    :mod:`mainspring.sitegen.rank` reads only ``url``, ``title``, ``word_count``,
    ``published_date`` and a publication slug, so ranking can be tested without
    a database at all. That is the point of keeping it a pure function.
    """

    def __init__(
        self,
        url: str,
        publication_slug: str,
        word_count: int,
        published: str | None = "2026-08-04",
        title: str | None = None,
    ) -> None:
        self.url = url
        self.publication_slug = publication_slug
        self.word_count = word_count
        self.published_date = (
            datetime.strptime(published, "%Y-%m-%d") if published else None
        )
        self.title = title or url.rsplit("/", 1)[-1].replace("-", " ").title()

    def __repr__(self) -> str:
        return f"<FakeArticle {self.url} {self.publication_slug} {self.word_count}w>"


@pytest.fixture
def fake_articles() -> list[FakeArticle]:
    """Nine articles across three publications with distinct lengths and dates.

    Every word count is unique so an assertion about ordering cannot be satisfied
    by accident.
    """
    return [
        FakeArticle("https://a.example/p/one", "alpha", 5000, "2026-08-04"),
        FakeArticle("https://a.example/p/two", "alpha", 3000, "2026-08-03"),
        FakeArticle("https://a.example/p/three", "alpha", 1000, "2026-08-02"),
        FakeArticle("https://b.example/p/one", "beta", 4000, "2026-08-04"),
        FakeArticle("https://b.example/p/two", "beta", 2500, "2026-08-01"),
        FakeArticle("https://c.example/p/one", "gamma", 4500, "2026-08-03"),
        FakeArticle("https://c.example/p/two", "gamma", 2000, "2026-08-02"),
        FakeArticle("https://c.example/p/three", "gamma", 1500, None),
        FakeArticle("https://c.example/p/four", "gamma", 500, "2026-07-30"),
    ]


@pytest.fixture
def subscription():
    """A free subscription pointing at the Construction Physics fixture feed."""
    from mainspring.config import Subscription

    return Subscription(
        name="Construction Physics",
        slug="construction-physics",
        feed_url="https://www.construction-physics.com/feed",
        tier="free",
        homepage_url="https://www.construction-physics.com",
    )


@pytest.fixture
def paid_subscription():
    """A paid subscription, which the RSS ingester must skip."""
    from mainspring.config import Subscription

    return Subscription(
        name="A Paid Publication",
        slug="a-paid-publication",
        feed_url="https://paid.example.com/feed",
        tier="paid",
    )
