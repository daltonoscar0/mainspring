"""config.py — paths, environment flags, and subscription loading.

Everything configurable about a Mainspring install lives in one of two places:

  subscriptions.yaml  — the publications you read. Hand-edited.
  environment vars    — where files live and whether the model runs.

There is no config file for anything else and no hidden state. Paths are
resolved relative to the repository root so the CLI, the test suite and the
FastAPI app all agree on where ``news.db`` and ``site/`` are, regardless of
which directory you happen to launch from.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal

import yaml

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

# mainspring/config.py -> mainspring/ -> repository root.
PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parent

TEMPLATES_DIR = PACKAGE_DIR / "templates"
STATIC_DIR = PACKAGE_DIR / "static"

# Overridable so you can keep the database on an encrypted volume and the
# generated site inside a separate deploy checkout.
DB_PATH = Path(os.environ.get("DB_PATH", REPO_ROOT / "news.db"))
SITE_DIR = Path(os.environ.get("SITE_DIR", REPO_ROOT / "site"))
SUBSCRIPTIONS_PATH = Path(
    os.environ.get("SUBSCRIPTIONS_PATH", REPO_ROOT / "subscriptions.yaml")
)

#: Shipped example list, used when no ``subscriptions.yaml`` exists.
#:
#: ``subscriptions.yaml`` is gitignored — what you read is exactly what this
#: project exists to keep to yourself — so a fresh clone has no list at all.
#: Falling back to the example means ``build_edition.py`` produces a real edition
#: immediately instead of failing on a missing file.
EXAMPLE_SUBSCRIPTIONS_PATH = REPO_ROOT / "subscriptions.example.yaml"

# ---------------------------------------------------------------------------
# Environment flags
# ---------------------------------------------------------------------------

#: Masthead text on the front page. Yours, not ours.
SITE_NAME = os.environ.get("SITE_NAME", "The Mainspring")

#: Strapline under the masthead.
SITE_TAGLINE = os.environ.get(
    "SITE_TAGLINE", "Intelligent discontent is the mainspring of civilization."
)

#: Base URL path the site is served from. GitHub Pages project sites live under
#: ``/<repo>/``, so the generator emits relative links everywhere and this is
#: only used for absolute references like the RSS/canonical link if ever added.
SITE_BASE_URL = os.environ.get("SITE_BASE_URL", "")

#: Maximum number of stories to put on one front page.
FRONT_PAGE_LIMIT = int(os.environ.get("FRONT_PAGE_LIMIT", "40"))

#: How many days of the wire an edition carries, counting its own date.
#:
#: A handful of Substacks file one or two posts on a typical day, which is not a
#: front page. So an edition is dated for the day it is built but draws on the
#: preceding week, with the lead and secondary slots reserved for that day's new
#: stories where there are any. Set to 1 for strict single-day editions, which is
#: honest but usually thin.
EDITION_WINDOW_DAYS = max(1, int(os.environ.get("EDITION_WINDOW_DAYS", "7")))

#: Newest entries to take from any one feed per ingest.
#:
#: Substack feeds carry 20. News sites carry far more — Truthout and Portside
#: ship 100 — and when a feed holds only excerpts each entry costs a separate
#: page fetch, so an uncapped ingest would make hundreds of requests to a single
#: publisher. Feeds are newest-first, so the cap simply means older items wait
#: for the archive rather than being lost.
MAX_ENTRIES_PER_FEED = int(os.environ.get("MAX_ENTRIES_PER_FEED", "25"))

#: Most river slots any one publication may occupy on a front page.
#:
#: Without this, a publication that posts hourly takes every slot and the rest of
#: the paper disappears. The lead rotation already guarantees a spread across
#: headlines; this does the same for the river.
RIVER_PER_PUBLICATION = int(os.environ.get("RIVER_PER_PUBLICATION", "4"))

#: Slots reserved for each river, keyed by publication kind.
#:
#: Separate budgets are the whole point of splitting the rivers: a shared pool
#: ordered by recency always goes to whoever posted most recently, which is the
#: wire, every time. Reserving slots means a newsletter that filed three days ago
#: still reaches the front page. The two default to ``FRONT_PAGE_LIMIT`` between
#: them.
NEWSLETTER_RIVER_LIMIT = int(os.environ.get("NEWSLETTER_RIVER_LIMIT", "18"))
WIRE_RIVER_LIMIT = int(os.environ.get("WIRE_RIVER_LIMIT", "22"))


def river_limits() -> dict[str, int]:
    """Return the per-kind river budgets."""
    return {"newsletter": NEWSLETTER_RIVER_LIMIT, "wire": WIRE_RIVER_LIMIT}

#: Descriptive User-Agent so publications can identify (and contact) the bot.
USER_AGENT = os.environ.get(
    "MAINSPRING_USER_AGENT",
    "Mozilla/5.0 (compatible; MainspringBot/2.0; +https://github.com/daltonoscar0/mainspring)",
)

#: Seconds to wait on any single HTTP response.
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "20"))


def dev_mode() -> bool:
    """Return True when DEV_MODE is set to a truthy value.

    Read at call time rather than import time so tests can toggle it with
    ``monkeypatch.setenv`` without reloading modules.
    """
    return os.environ.get("DEV_MODE", "").strip().lower() in ("1", "true", "yes", "on")


Tier = Literal["free", "paid"]

#: Which river a publication files into.
#:
#: ``newsletter`` — an individual writer, filing occasionally. The premise of the
#: paper, so this is the default and its river runs first.
#: ``wire`` — a news outlet, filing many times a day. Useful, but it will bury
#: everything else if the two are pooled, which is why they are not.
Kind = Literal["newsletter", "wire"]

#: Display labels for the two rivers. Both are real newspaper terms and each says
#: something true: correspondents file to you under their own name; the wire is
#: agency copy arriving all day.
KIND_LABELS: dict[str, str] = {
    "newsletter": "Your correspondents",
    "wire": "The wire",
}

#: Order the rivers appear in. Newsletters first — that is what this is for.
KIND_ORDER: tuple[str, ...] = ("newsletter", "wire")


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Subscription:
    """One publication the user subscribes to.

    Attributes:
        name:          Display name, used in bylines and section labels.
        slug:          URL-safe identifier. Also the publication page filename.
        feed_url:      RSS endpoint. For Substacks this is ``<host>/feed``.
        tier:          ``free`` (ingested by RSS) or ``paid`` (ingested by email
                       in Milestone 2; skipped by the RSS ingester today).
        homepage_url:  Public home page, linked from the publication page.
        kind:          ``newsletter`` (an individual writer, filing occasionally)
                       or ``wire`` (a news outlet, filing all day). Decides which
                       river the publication's stories run in. Defaults to
                       ``newsletter``.
        fetch_pages:   Whether a teaser entry may trigger a fetch of the post
                       page. Set ``false`` for a publication whose feed is clean
                       but whose page layout defeats the cleaner — Democracy
                       Now's transcript pages wrap the article in sidebars of
                       guests, topics and links, and its 180-word feed summaries
                       are better copy than anything extractable from the page.
                       Also the polite setting for a publisher you would rather
                       not crawl.
    """

    name: str
    slug: str
    feed_url: str
    tier: Tier = "free"
    homepage_url: str = ""
    kind: Kind = "newsletter"
    fetch_pages: bool = True

    @property
    def is_free(self) -> bool:
        """True when this publication can be ingested over RSS."""
        return self.tier == "free"


class SubscriptionsError(RuntimeError):
    """Raised when subscriptions.yaml is missing or structurally invalid."""


def _homepage_from_feed(feed_url: str) -> str:
    """Derive a publication home page from its feed URL.

    ``https://example.substack.com/feed`` becomes
    ``https://example.substack.com``. Used when the YAML entry omits
    ``homepage_url``, which is the common case.
    """
    return feed_url.rsplit("/feed", 1)[0].rstrip("/") if "/feed" in feed_url else feed_url


def parse_subscriptions(raw: object, *, source: str = "<memory>") -> list[Subscription]:
    """Validate a decoded YAML document and return Subscription objects.

    Split out from :func:`load_subscriptions` so tests can feed dicts directly
    without writing temporary files.

    Args:
        raw:    The decoded YAML — expected to be a mapping with a
                ``publications`` list.
        source: Label used in error messages to identify where ``raw`` came from.

    Raises:
        SubscriptionsError: if the document shape is wrong, a required field is
            missing, ``tier`` is not free/paid, or two entries share a slug.
    """
    if not isinstance(raw, dict):
        raise SubscriptionsError(f"{source}: expected a mapping at the top level")

    publications = raw.get("publications")
    if not isinstance(publications, list) or not publications:
        raise SubscriptionsError(f"{source}: 'publications' must be a non-empty list")

    subs: list[Subscription] = []
    seen: set[str] = set()

    for index, entry in enumerate(publications):
        where = f"{source}: publications[{index}]"
        if not isinstance(entry, dict):
            raise SubscriptionsError(f"{where} must be a mapping")

        for field in ("name", "slug", "feed_url"):
            value = entry.get(field)
            if not isinstance(value, str) or not value.strip():
                raise SubscriptionsError(f"{where} is missing required field '{field}'")

        tier = entry.get("tier", "free")
        if tier not in ("free", "paid"):
            raise SubscriptionsError(f"{where} has invalid tier {tier!r} (use free or paid)")

        kind = entry.get("kind", "newsletter")
        if kind not in KIND_ORDER:
            raise SubscriptionsError(
                f"{where} has invalid kind {kind!r} (use newsletter or wire)"
            )

        fetch_pages = entry.get("fetch_pages", True)
        if not isinstance(fetch_pages, bool):
            raise SubscriptionsError(
                f"{where} has non-boolean fetch_pages {fetch_pages!r} (use true or false)"
            )

        slug = entry["slug"].strip()
        if slug in seen:
            raise SubscriptionsError(f"{where} reuses slug {slug!r}")
        seen.add(slug)

        feed_url = entry["feed_url"].strip()
        subs.append(
            Subscription(
                name=entry["name"].strip(),
                slug=slug,
                feed_url=feed_url,
                tier=tier,
                homepage_url=(entry.get("homepage_url") or _homepage_from_feed(feed_url)).strip(),
                kind=kind,
                fetch_pages=fetch_pages,
            )
        )

    return subs


def load_subscriptions(path: Path | str | None = None) -> list[Subscription]:
    """Read and validate subscriptions.yaml.

    Falls back to the shipped example list when no ``subscriptions.yaml`` exists,
    logging a warning — a fresh clone should build something rather than fail on a
    missing file. An explicitly passed ``path`` is never substituted: if you named
    a file, you meant that file.

    Args:
        path: Override the default location (``SUBSCRIPTIONS_PATH``).

    Returns:
        Every configured publication, free and paid, in file order.

    Raises:
        SubscriptionsError: if no list can be found, or the one found is invalid.
    """
    target = Path(path) if path is not None else SUBSCRIPTIONS_PATH

    if not target.exists() and path is None and EXAMPLE_SUBSCRIPTIONS_PATH.exists():
        logger.warning(
            "%s not found — using the example list at %s. Copy it and edit: "
            "cp subscriptions.example.yaml subscriptions.yaml",
            target.name,
            EXAMPLE_SUBSCRIPTIONS_PATH.name,
        )
        target = EXAMPLE_SUBSCRIPTIONS_PATH

    if not target.exists():
        raise SubscriptionsError(
            f"{target} not found. Mainspring reads the publications you subscribe "
            "to from subscriptions.yaml in the repository root; copy "
            "subscriptions.example.yaml to start, or set SUBSCRIPTIONS_PATH to "
            "keep the file elsewhere."
        )

    with target.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)

    return parse_subscriptions(raw, source=str(target))


def free_subscriptions(subs: Iterable[Subscription]) -> list[Subscription]:
    """Filter to the publications the RSS ingester can handle today."""
    return [s for s in subs if s.is_free]
