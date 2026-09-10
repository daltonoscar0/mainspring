"""build.py — render the whole site into ``site/``.

The output is plain files with relative links and no JavaScript. That is the
point: it opens from ``file://``, serves from any static host, drops onto GitHub
Pages under a project subpath without a base URL, and keeps working with no
server behind it.

What gets written::

    site/
    ├── index.html                     the latest edition
    ├── archive.html                   every edition, newest first
    ├── .nojekyll                      GitHub Pages serves the files verbatim
    ├── static/mainspring.css
    ├── articles/<publication>/<slug>.html
    ├── publications/index.html
    ├── publications/<slug>.html
    └── editions/<YYYY-MM-DD>/index.html

Editions are rebuilt from scratch on every run, oldest first, with lead-story
rotation accumulated as the build walks forward. Recomputing rather than reading
back the previous run's ``daily_editions`` rows is what makes the build a pure
function of the database: the same articles always produce the same site, so a
rebuild is a no-op in a diff and Milestone 3 can commit the output without noise.
"""

from __future__ import annotations

import logging
import shutil
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from sqlalchemy.orm import Session, joinedload

from .. import config
from ..models import Article, DailyEdition, Publication
from . import rank

logger = logging.getLogger(__name__)

#: Directories under ``site/`` that the generator owns outright. Cleared before
#: each build so a renamed post does not leave an orphan page behind. Anything
#: else in ``site/``, a CNAME, a .git directory, a favicon you added, is left
#: untouched, which is what makes it safe to point SITE_DIR at a deploy checkout.
OWNED_DIRS = ("articles", "publications", "editions", "static")

#: How many earlier editions to list at the foot of the front page.
RECENT_EDITIONS_ON_FRONT = 5


@dataclass
class BuildReport:
    """What one build produced.

    Attributes:
        editions:     Edition front pages written.
        articles:     Article pages written.
        publications: Publication pages written.
        site_dir:     Where the output went.
        errors:       Human-readable failures. Empty on a clean build.
    """

    editions: int = 0
    articles: int = 0
    publications: int = 0
    site_dir: Path = config.SITE_DIR
    errors: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []

    @property
    def ok(self) -> bool:
        """True when nothing failed."""
        return not self.errors

    def summary(self) -> str:
        """One-line description, for logs and the CLI."""
        return (
            f"{self.articles} articles, {self.publications} publications, "
            f"{self.editions} editions -> {self.site_dir}"
        )


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

def _parse_iso(date_iso: str) -> datetime:
    """Parse a YYYY-MM-DD string into a datetime at midnight."""
    return datetime.strptime(date_iso, "%Y-%m-%d")


def date_long(value: datetime | str) -> str:
    """Format as ``Tuesday, August 4, 2026``.

    Written out by hand rather than with ``%-d``/``%#d``, which are
    platform-specific and would render differently on macOS and Windows.
    """
    stamp = _parse_iso(value) if isinstance(value, str) else value
    return f"{stamp:%A, %B} {stamp.day}, {stamp.year}"


def date_medium(value: datetime | str) -> str:
    """Format as ``Aug 4, 2026`` — unambiguous in a list spanning years."""
    stamp = _parse_iso(value) if isinstance(value, str) else value
    return f"{stamp:%b} {stamp.day}, {stamp.year}"


def date_short(value: datetime | str) -> str:
    """Format as ``Aug 4`` — for bylines, where the year is context."""
    stamp = _parse_iso(value) if isinstance(value, str) else value
    return f"{stamp:%b} {stamp.day}"


def _window_articles(
    by_date: dict[str, list[Article]],
    date_iso: str,
    window_days: int,
) -> list[Article]:
    """Collect the articles an edition dated ``date_iso`` carries.

    The window is inclusive of the edition's own date and reaches back
    ``window_days - 1`` days. Dates are enumerated rather than filtered so a gap
    in the archive costs nothing.
    """
    end = _parse_iso(date_iso)
    collected: list[Article] = []
    for offset in range(window_days):
        day = (end - timedelta(days=offset)).date().isoformat()
        collected.extend(by_date.get(day, ()))
    return collected


# ---------------------------------------------------------------------------
# Paths and relative links
# ---------------------------------------------------------------------------

def article_href(article: Article) -> str:
    """Root-relative path to an article's page."""
    return f"articles/{article.publication.slug}/{article.slug}.html"


def publication_href(slug: str) -> str:
    """Root-relative path to a publication's page."""
    return f"publications/{slug}.html"


def edition_href(date_iso: str) -> str:
    """Root-relative path to a dated edition."""
    return f"editions/{date_iso}/index.html"


def rel_prefix(href: str) -> str:
    """Return the ``../`` prefix that gets from ``href`` back to the site root.

    ``index.html`` yields ``""``; ``articles/pub/post.html`` yields ``../../``.
    Every link in every template is built from this, which is the whole reason
    the site works unchanged under a GitHub Pages project subpath.
    """
    depth = href.count("/")
    return "../" * depth


# ---------------------------------------------------------------------------
# View models
#
# Templates never touch ORM objects. Building plain dicts here means a template
# cannot trigger a lazy load, cannot see a column that is not meant for display,
# and can be rendered in a test from a literal dict.
# ---------------------------------------------------------------------------

#: Paragraphs of the lead story to run on the front page, and the character
#: budget across them.
LEAD_EXCERPT_PARAGRAPHS = 3
LEAD_EXCERPT_CHARS = 520


def _echoes(paragraph: str, standfirst: str | None, *, window: int = 60) -> bool:
    """True when a paragraph merely restates the standfirst.

    Summarization models are often extractive — DistilBART frequently lifts a
    story's opening almost verbatim, and the dev-mode placeholder does so by
    construction. Either way, printing the dek and then immediately reprinting it
    as the lede looks like a bug.

    Compared as a common prefix rather than a substring, over at most ``window``
    characters. Both sides are whitespace- and case-normalized, the ``[DEV]``
    marker is removed, and trailing ellipses are stripped — a truncated dek ends
    in one and the paragraph it was cut from does not, which would defeat a
    substring test.
    """
    if not standfirst:
        return False

    def normalize(text: str) -> str:
        flat = " ".join(text.split()).casefold()
        return flat.removeprefix("[dev] ").rstrip("… .").strip()

    body = normalize(paragraph)
    dek = normalize(standfirst)

    # Compare only as far as the shorter of the two reaches. Below ~25 characters
    # a shared opening is coincidence, not a repeat.
    length = min(len(body), len(dek), window)
    if length < 25:
        return False
    return body[:length] == dek[:length]


def lead_excerpt(
    body_text: str | None,
    *,
    standfirst: str | None = None,
    paragraphs: int = LEAD_EXCERPT_PARAGRAPHS,
    budget: int = LEAD_EXCERPT_CHARS,
) -> list[str]:
    """Return the opening paragraphs of an article, for the front page lead.

    A broadsheet runs the start of its lead story on the front page rather than
    only a headline and a dek. It also solves a layout problem: when the lead has
    no art its column collapses to a few hundred pixels while the secondary rail
    runs much taller, leaving a void in the most important part of the page.

    Paragraphs are taken from ``body_text`` — one block per line, which is what
    the cleaner produces — so the result is plain text and gets autoescaped.
    Very short opening lines are skipped: a Substack post often begins with a
    one-line kicker or a date stamp, which is not a lede.

    Args:
        body_text:  The article's plain-text body.
        standfirst: The dek, if any. Leading paragraphs that merely restate it
                    are skipped rather than printed twice.
        paragraphs: Maximum blocks to return.
        budget:     Maximum total characters. The last paragraph is dropped
                    rather than truncated mid-sentence.
    """
    if not body_text:
        return []

    blocks = [line.strip() for line in body_text.split("\n") if len(line.strip()) > 60]

    # Drop leading blocks the dek already said. Only leading ones: the same
    # sentence recurring later in the piece is the author's business.
    while blocks and _echoes(blocks[0], standfirst):
        blocks.pop(0)

    chosen: list[str] = []
    used = 0
    for block in blocks[:paragraphs]:
        if chosen and used + len(block) > budget:
            break
        chosen.append(block)
        used += len(block)
    return chosen


def article_view(article: Article, *, include_body: bool = False) -> dict:
    """Build the template view of one article.

    Args:
        article:      A loaded Article with its ``publication`` available.
        include_body: Include the sanitized body HTML. Only the article page
                      needs it, and it is by far the largest column.

    Returns:
        A dict of display-ready values. Dates are pre-formatted, so no template
        does date arithmetic.
    """
    stamp = article.published_date
    view = {
        "id": article.id,
        "title": article.title,
        "url": article.url,
        "slug": article.slug,
        "author": article.author,
        "standfirst": article.standfirst,
        "word_count": article.word_count or 0,
        "hero_image_url": article.hero_image_url,
        "hero_image_alt": article.hero_image_alt,
        "publication_name": article.publication.name,
        "publication_slug": article.publication.slug,
        "publication_href": publication_href(article.publication.slug),
        "href": article_href(article),
        "edition_href": edition_href(article.edition_date),
        "date_iso": article.edition_date,
        "date_short": date_short(stamp) if stamp else "",
        "date_medium": date_medium(stamp) if stamp else "",
        "date_long": date_long(stamp) if stamp else "",
    }
    if include_body:
        view["body_html"] = article.body_html or ""
    return view


def ranking_view(ranking: rank.Ranking, names: dict[str, str]) -> dict:
    """Turn a :class:`~mainspring.sitegen.rank.Ranking` into front page slots.

    The river is grouped by publication so it reads as a set of desks rather
    than an undifferentiated list.

    Args:
        ranking: The computed hierarchy.
        names:   Publication slug to display name.
    """
    lead = None
    if ranking.lead is not None:
        lead = article_view(ranking.lead)
        # Only the lead runs body text on the front page.
        lead["excerpt"] = lead_excerpt(
            ranking.lead.body_text, standfirst=ranking.lead.standfirst
        )

    def desks(articles: list) -> list[dict]:
        """Group a river's articles under their publication's standing head."""
        return [
            {
                "slug": slug,
                "name": names.get(slug, slug),
                "href": publication_href(slug),
                "articles": [article_view(a) for a in group],
            }
            for slug, group in rank.group_by_publication(articles)
        ]

    rivers = [
        {
            "key": section.key,
            "label": section.label,
            "count": len(section.articles),
            "groups": desks(section.articles),
        }
        for section in ranking.sections
        # A river with nothing in it gets no heading, an empty section rule
        # reads as a rendering fault rather than a quiet news day.
        if section.articles
    ]

    return {
        "lead": lead,
        "secondaries": [article_view(a) for a in ranking.secondaries],
        "rivers": rivers,
        "river_count": len(ranking.river),
    }


# ---------------------------------------------------------------------------
# Jinja environment
# ---------------------------------------------------------------------------

def make_environment(templates_dir: Path | None = None) -> Environment:
    """Build the Jinja environment.

    Autoescaping is on, and article bodies are marked safe explicitly at the one
    point they are inserted — they have already been through the cleaner's
    allowlist, and nothing else in a template is trusted.

    ``StrictUndefined`` turns a typo in a template into a build failure instead
    of a silently blank front page.
    """
    env = Environment(
        loader=FileSystemLoader(str(templates_dir or config.TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    env.filters["date_long"] = date_long
    env.filters["date_medium"] = date_medium
    env.filters["date_short"] = date_short
    return env


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _prepare_site_dir(site_dir: Path) -> None:
    """Create ``site/`` and clear the directories the generator owns.

    Only :data:`OWNED_DIRS` are removed. A CNAME file, a ``.git`` directory or
    anything else already in ``site/`` survives, so this is safe to run against
    a deploy checkout.
    """
    site_dir.mkdir(parents=True, exist_ok=True)
    for name in OWNED_DIRS:
        target = site_dir / name
        if target.is_dir():
            shutil.rmtree(target)


def _write(path: Path, html: str) -> None:
    """Write one page, creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def build_site(
    db: Session,
    *,
    site_dir: Path | None = None,
    templates_dir: Path | None = None,
) -> BuildReport:
    """Render every page and return a report.

    Args:
        db:            Open session. Edition records are written and committed.
        site_dir:      Output directory. Defaults to ``SITE_DIR``.
        templates_dir: Template directory. Defaults to the packaged templates.

    Returns:
        A :class:`BuildReport`. An empty database still produces a valid site —
        the front page renders its empty state rather than failing, because a
        broken page is harder to diagnose than an honest one.
    """
    out = Path(site_dir) if site_dir is not None else config.SITE_DIR
    env = make_environment(templates_dir)
    report = BuildReport(site_dir=out)

    publications = db.query(Publication).order_by(Publication.name).all()
    names = {p.slug: p.name for p in publications}
    kinds = {p.slug: (p.kind or "newsletter") for p in publications}

    def section_of(article: Article) -> str:
        """Which river an article files into."""
        return kinds.get(rank.publication_slug(article), "newsletter")

    limits = config.river_limits()
    section_specs = [
        rank.SectionSpec(
            key=key,
            label=config.KIND_LABELS.get(key, key.title()),
            limit=limits.get(key),
        )
        for key in config.KIND_ORDER
    ]

    articles = (
        db.query(Article)
        .options(joinedload(Article.publication))
        .order_by(Article.published_date.desc().nullslast(), Article.id.desc())
        .all()
    )

    _prepare_site_dir(out)

    shared = {
        "site_name": config.SITE_NAME,
        "site_tagline": config.SITE_TAGLINE,
        "version": __import__("mainspring").__version__,
        "dev_mode": config.dev_mode(),
        "built_at": date_medium(datetime.now(timezone.utc)),
    }

    # --- static assets -----------------------------------------------------
    static_out = out / "static"
    if config.STATIC_DIR.is_dir():
        shutil.copytree(config.STATIC_DIR, static_out, dirs_exist_ok=True)
    else:  # pragma: no cover, only if the package is installed incompletely
        static_out.mkdir(parents=True, exist_ok=True)
        report.errors.append(f"static directory missing: {config.STATIC_DIR}")

    # GitHub Pages runs Jekyll over the output unless told not to, which would
    # drop any path beginning with an underscore.
    (out / ".nojekyll").write_text("", encoding="utf-8")

    # --- article pages -----------------------------------------------------
    template = env.get_template("article.html")
    for article in articles:
        href = article_href(article)
        view = article_view(article, include_body=True)
        # The body has been through the cleaner's tag allowlist; marking it safe
        # here is the single, deliberate exception to autoescaping.
        from markupsafe import Markup

        view["body_html"] = Markup(view["body_html"])
        _write(out / href, template.render(article=view, rel=rel_prefix(href), **shared))
        report.articles += 1

    # --- editions ----------------------------------------------------------
    by_date: dict[str, list[Article]] = defaultdict(list)
    for article in articles:
        by_date[article.edition_date].append(article)

    # Oldest first, so lead rotation accumulates forward through the archive.
    edition_dates = sorted(by_date)
    lead_history: dict[str, str] = {}
    editions: list[dict] = []
    rankings: dict[str, rank.Ranking] = {}
    window_days = config.EDITION_WINDOW_DAYS

    for number, date_iso in enumerate(edition_dates, start=1):
        fresh = by_date[date_iso]
        window = _window_articles(by_date, date_iso, window_days)
        ranking = rank.rank_articles(
            window,
            fresh=fresh,
            lead_history=lead_history,
            per_publication=config.RIVER_PER_PUBLICATION,
            limit=config.FRONT_PAGE_LIMIT,
            section_of=section_of,
            sections=section_specs,
        )
        rankings[date_iso] = ranking

        if ranking.lead is not None:
            lead_history[rank.publication_slug(ranking.lead)] = date_iso

        placed = ranking.all_articles
        editions.append(
            {
                "number": number,
                "date_iso": date_iso,
                "date_long": date_long(date_iso),
                "date_medium": date_medium(date_iso),
                "href": edition_href(date_iso),
                "article_count": len(placed),
                "fresh_count": len(fresh),
                "window_days": window_days,
                "publication_count": len({rank.publication_slug(a) for a in placed}),
                "lead_title": ranking.lead.title if ranking.lead is not None else None,
                "lead_publication": (
                    ranking.lead.publication.name if ranking.lead is not None else None
                ),
                "lead_article_id": ranking.lead.id if ranking.lead is not None else None,
            }
        )

    template = env.get_template("front_page.html")
    for index, edition in enumerate(editions):
        date_iso = edition["date_iso"]
        # Earlier editions, newest of them first.
        previous = list(reversed(editions[max(0, index - RECENT_EDITIONS_ON_FRONT):index]))
        payload = {
            "edition": {**edition, "previous": previous},
            "ranking": ranking_view(rankings[date_iso], names),
        }
        href = edition_href(date_iso)
        _write(out / href, template.render(rel=rel_prefix(href), **payload, **shared))
        report.editions += 1

    # The newest edition is also the site root. Rendered a second time rather
    # than copied, because its relative link depth differs.
    if editions:
        newest = editions[-1]
        payload = {
            "edition": {
                **newest,
                "previous": list(reversed(editions[-1 - RECENT_EDITIONS_ON_FRONT : -1])),
            },
            "ranking": ranking_view(rankings[newest["date_iso"]], names),
        }
    else:
        payload = {
            "edition": {
                "number": 1,
                "date_iso": datetime.now(timezone.utc).date().isoformat(),
                "date_long": date_long(datetime.now(timezone.utc)),
                "date_medium": date_medium(datetime.now(timezone.utc)),
                "href": "index.html",
                "article_count": 0,
                "fresh_count": 0,
                "window_days": window_days,
                "publication_count": len(publications),
                "lead_title": None,
                "lead_publication": None,
                "lead_article_id": None,
                "previous": [],
            },
            "ranking": {"lead": None, "secondaries": [], "rivers": [], "river_count": 0},
        }
    _write(out / "index.html", template.render(rel="", **payload, **shared))

    # --- publication pages -------------------------------------------------
    per_publication: dict[str, list[Article]] = defaultdict(list)
    for article in articles:
        per_publication[article.publication.slug].append(article)

    lead_counts: dict[str, int] = defaultdict(int)
    for edition in editions:
        if edition["lead_article_id"] is not None:
            ranking = rankings[edition["date_iso"]]
            lead_counts[rank.publication_slug(ranking.lead)] += 1

    template = env.get_template("publication.html")
    directory: list[dict] = []
    for publication in publications:
        own = per_publication.get(publication.slug, [])
        href = publication_href(publication.slug)
        view = {
            "name": publication.name,
            "slug": publication.slug,
            "tier": publication.tier,
            "homepage_url": publication.homepage_url,
            "href": href,
            "article_count": len(own),
            "lead_count": lead_counts.get(publication.slug, 0),
            "latest_date_short": date_medium(own[0].published_date)
            if own and own[0].published_date
            else "",
            "articles": [article_view(a) for a in own],
        }
        _write(out / href, template.render(publication=view, rel=rel_prefix(href), **shared))
        report.publications += 1
        directory.append({k: v for k, v in view.items() if k != "articles"})

    template = env.get_template("publications.html")
    href = "publications/index.html"
    _write(
        out / href,
        template.render(publications=directory, rel=rel_prefix(href), **shared),
    )

    # --- archive -----------------------------------------------------------
    template = env.get_template("archive.html")
    _write(
        out / "archive.html",
        template.render(editions=list(reversed(editions)), rel="", **shared),
    )

    # --- record what was published ----------------------------------------
    _record_editions(db, editions)

    logger.info("Build complete — %s", report.summary())
    return report


def _record_editions(db: Session, editions: list[dict]) -> None:
    """Upsert ``daily_editions`` rows for the editions just rendered.

    These rows are a record of what was published, not an input to the next
    build — the ranker's history is recomputed from scratch each time. They exist
    so the JSON API can report editions without re-running the generator.
    """
    existing = {row.date: row for row in db.query(DailyEdition).all()}
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    for edition in editions:
        row = existing.get(edition["date_iso"])
        if row is None:
            row = DailyEdition(date=edition["date_iso"])
            db.add(row)
        row.lead_article_id = edition["lead_article_id"]
        row.article_count = edition["article_count"]
        row.built_at = now

    db.commit()


def build(
    *,
    site_dir: Path | None = None,
    templates_dir: Path | None = None,
) -> BuildReport:
    """Open a session, build the site, close the session.

    The convenience entry point used by the CLI, the API and the scheduler.
    """
    from ..database import init_db, session

    init_db()
    db = session()
    try:
        return build_site(db, site_dir=site_dir, templates_dir=templates_dir)
    finally:
        db.close()
