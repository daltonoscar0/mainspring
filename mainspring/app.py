"""app.py — FastAPI: serve the built site, expose a small JSON API, run the clock.

The site itself is static, so this server is optional — ``site/`` opens from
``file://`` and deploys to any static host. What the server adds is the ability
to trigger work: poll feeds, rebuild the site, and do both on a schedule.

Run it::

    DEV_MODE=1 uvicorn mainspring.app:app
    python -m mainspring.app --dev          # equivalent, with argument parsing

Endpoints:

    GET  /                     the built site, ``site/index.html`` at the root
    GET  /api/health           liveness, dev mode, and whether a site exists
    GET  /api/publications     every configured publication with article counts
    GET  /api/articles         articles, filterable by date and publication
    GET  /api/editions         every built edition, newest first
    POST /api/refresh          ingest, then rebuild. Returns immediately.
    POST /api/build            rebuild from what is already stored.

There is no authentication. Bind to ``127.0.0.1`` unless the machine is
trusted — anyone who can reach the port can trigger a refresh.
"""

from __future__ import annotations

import logging
import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from . import config
from .database import get_db, init_db, session
from .models import Article, DailyEdition, Publication
from .sitegen import build as sitegen
from .summarize import generate_standfirsts

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

#: Hours between scheduled ingests. Each one is followed by a rebuild.
REFRESH_INTERVAL_HOURS = int(os.environ.get("REFRESH_INTERVAL_HOURS", "6"))

#: Guards the pipeline so a scheduled run and a POSTed one cannot overlap. SQLite
#: takes one writer at a time and two concurrent ingests would just contend.
_pipeline_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def run_pipeline(*, ingest: bool = True) -> None:
    """Ingest, generate standfirsts, and rebuild the site.

    Runs on a background thread — from startup, from APScheduler, and from the
    POST endpoints. If a run is already in progress this returns immediately
    rather than queueing, because the next scheduled tick will pick up anything
    missed.

    Args:
        ingest: False skips feed polling and only regenerates the site.
    """
    if not _pipeline_lock.acquire(blocking=False):
        logger.info("Pipeline already running — skipping this trigger.")
        return

    try:
        if ingest:
            from .ingest import rss

            try:
                subscriptions = config.load_subscriptions()
            except config.SubscriptionsError as exc:
                logger.error("Cannot read subscriptions: %s", exc)
                return
            report = rss.ingest(subscriptions)
            logger.info("Ingest: %s", report.summary())

        db = session()
        try:
            generate_standfirsts(db)
        except Exception as exc:  # noqa: BLE001, build anyway, with fewer deks
            logger.error("Standfirst generation failed: %s", exc)
        finally:
            db.close()

        result = sitegen.build()
        logger.info("Build: %s", result.summary())

    except Exception as exc:  # noqa: BLE001, a background thread must not die silently
        logger.exception("Pipeline failed: %s", exc)
    finally:
        _pipeline_lock.release()


def _spawn(*, ingest: bool) -> None:
    """Start :func:`run_pipeline` on a daemon thread."""
    threading.Thread(
        target=run_pipeline,
        kwargs={"ingest": ingest},
        daemon=True,
        name="mainspring-pipeline",
    ).start()


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize the database, ensure a site exists, and start the scheduler.

    The first build runs on a background thread so the server accepts requests
    immediately rather than waiting on feed fetches. If ``site/`` is empty a
    placeholder front page is written synchronously first, so the root URL
    serves something explanatory instead of a 404 while that happens.
    """
    init_db()

    if not (config.SITE_DIR / "index.html").exists():
        logger.info("No site yet — writing a placeholder front page.")
        try:
            sitegen.build()
        except Exception as exc:  # noqa: BLE001, the API is still useful
            logger.error("Initial build failed: %s", exc)

    # Mounted here rather than at import time: the directory has to exist before
    # StaticFiles will accept it, and the build above is what guarantees that.
    if config.SITE_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(config.SITE_DIR), html=True), name="site")
    else:  # pragma: no cover, only if the build failed outright
        logger.error("Site directory %s does not exist; / will 404.", config.SITE_DIR)

    _spawn(ingest=True)

    from apscheduler.schedulers.background import BackgroundScheduler

    scheduler = BackgroundScheduler()
    scheduler.add_job(
        run_pipeline,
        "interval",
        hours=REFRESH_INTERVAL_HOURS,
        id="refresh",
        # A build follows every ingest inside run_pipeline, so one job covers
        # both and they can never interleave.
        kwargs={"ingest": True},
    )
    scheduler.start()
    app.state.scheduler = scheduler
    logger.info("Scheduled refresh every %d hours.", REFRESH_INTERVAL_HOURS)

    yield

    scheduler.shutdown(wait=False)


app = FastAPI(
    title="Mainspring",
    version="2.0.0",
    summary="A newspaper built from your own subscriptions.",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _day_bounds(date_iso: str) -> tuple[datetime, datetime]:
    """Return midnight-to-midnight bounds for an ISO date.

    Raises:
        HTTPException: 400 if the date is not YYYY-MM-DD.
    """
    try:
        start = datetime.strptime(date_iso, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date. Use YYYY-MM-DD.") from None
    return start, start + timedelta(days=1)


def _article_json(article: Article, *, include_body: bool = False) -> dict:
    """Serialize an article for the API.

    Body HTML is opt-in: it is large, and most callers want the index rather
    than the text.
    """
    payload = {
        "id": article.id,
        "title": article.title,
        "url": article.url,
        "slug": article.slug,
        "author": article.author,
        "publication": article.publication.name,
        "publication_slug": article.publication.slug,
        "published_date": article.published_date.isoformat() + "Z"
        if article.published_date
        else None,
        "edition_date": article.edition_date,
        "standfirst": article.standfirst,
        "summarizing": article.summarizing,
        "word_count": article.word_count,
        "hero_image_url": article.hero_image_url,
        "source_kind": article.source_kind,
        "page": sitegen.article_href(article),
    }
    if include_body:
        payload["body_html"] = article.body_html
    return payload


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health(db: Session = Depends(get_db)) -> dict:
    """Report liveness and what the install currently holds."""
    return {
        "status": "ok",
        "version": app.version,
        "dev_mode": config.dev_mode(),
        "database": str(config.DB_PATH),
        "site_built": (config.SITE_DIR / "index.html").exists(),
        "articles": db.query(func.count(Article.id)).scalar() or 0,
        "publications": db.query(func.count(Publication.id)).scalar() or 0,
        "editions": db.query(func.count(DailyEdition.id)).scalar() or 0,
        "pending_standfirsts": db.query(func.count(Article.id))
        .filter(Article.standfirst.is_(None))
        .scalar()
        or 0,
    }


@app.get("/api/publications")
def list_publications(db: Session = Depends(get_db)) -> dict:
    """Every configured publication, with how many articles are held for each."""
    counts = dict(
        db.query(Article.publication_id, func.count(Article.id))
        .group_by(Article.publication_id)
        .all()
    )
    publications = db.query(Publication).order_by(Publication.name).all()
    return {
        "publications": [
            {
                "name": p.name,
                "slug": p.slug,
                "tier": p.tier,
                "feed_url": p.feed_url,
                "homepage_url": p.homepage_url,
                "article_count": counts.get(p.id, 0),
                "page": sitegen.publication_href(p.slug),
            }
            for p in publications
        ]
    }


@app.get("/api/articles")
def list_articles(
    date: str | None = Query(default=None, description="YYYY-MM-DD"),
    publication: str | None = Query(default=None, description="Publication slug"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    body: bool = Query(default=False, description="Include sanitized body HTML"),
    db: Session = Depends(get_db),
) -> dict:
    """Return articles, newest first.

    Query parameters:
        date:        Restrict to one calendar day. An article counts for a day
                     if it was published then, or — when the feed gave no date —
                     if it was ingested then.
        publication: Restrict to one publication, by slug.
        limit:       Page size, 1–500. Default 50.
        offset:      Rows to skip, for paging.
        body:        Include ``body_html``. Off by default; it is large.
    """
    query = db.query(Article).options(joinedload(Article.publication))

    if date:
        start, end = _day_bounds(date)
        from sqlalchemy import and_, or_

        query = query.filter(
            or_(
                and_(Article.published_date >= start, Article.published_date < end),
                and_(
                    Article.published_date.is_(None),
                    Article.created_at >= start,
                    Article.created_at < end,
                ),
            )
        )

    if publication:
        row = db.query(Publication).filter(Publication.slug == publication).first()
        if row is None:
            raise HTTPException(status_code=404, detail=f"No publication {publication!r}.")
        query = query.filter(Article.publication_id == row.id)

    total = query.with_entities(func.count(Article.id)).scalar() or 0
    articles = (
        query.order_by(Article.published_date.desc().nullslast(), Article.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    return {
        "date": date,
        "publication": publication,
        "total": total,
        "offset": offset,
        "limit": limit,
        "articles": [_article_json(a, include_body=body) for a in articles],
    }


@app.get("/api/editions")
def list_editions(db: Session = Depends(get_db)) -> dict:
    """Every built edition, newest first, with its lead story."""
    rows = (
        db.query(DailyEdition)
        .options(joinedload(DailyEdition.lead_article).joinedload(Article.publication))
        .order_by(DailyEdition.date.desc())
        .all()
    )
    return {
        "editions": [
            {
                "date": row.date,
                "article_count": row.article_count,
                "built_at": row.built_at.isoformat() + "Z" if row.built_at else None,
                "page": sitegen.edition_href(row.date),
                "lead": {
                    "id": row.lead_article.id,
                    "title": row.lead_article.title,
                    "publication": row.lead_article.publication.name,
                    "page": sitegen.article_href(row.lead_article),
                }
                if row.lead_article is not None
                else None,
            }
            for row in rows
        ]
    }


@app.post("/api/refresh", status_code=202)
def refresh() -> JSONResponse:
    """Poll every free publication, write standfirsts, and rebuild the site.

    Returns as soon as the work is handed to a background thread. Watch the
    server log for progress, or poll ``/api/health`` for the article count.
    """
    _spawn(ingest=True)
    return JSONResponse(
        status_code=202,
        content={"status": "started", "detail": "Ingest and rebuild running in the background."},
    )


@app.post("/api/build", status_code=202)
def rebuild() -> JSONResponse:
    """Regenerate the static site from what is already stored. No feeds polled."""
    _spawn(ingest=False)
    return JSONResponse(
        status_code=202,
        content={"status": "started", "detail": "Rebuild running in the background."},
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    """Run the server. ``python -m mainspring.app --dev``."""
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="Serve Mainspring.")
    parser.add_argument(
        "--dev",
        action="store_true",
        help="Skip the model and use placeholder standfirsts.",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address. Default 127.0.0.1 — there is no authentication.",
    )
    parser.add_argument("--port", type=int, default=8000, help="Port. Default 8000.")
    parser.add_argument(
        "--site-dir",
        type=Path,
        default=None,
        help=f"Directory to serve. Default {config.SITE_DIR}.",
    )
    args = parser.parse_args(argv)

    if args.dev:
        os.environ["DEV_MODE"] = "1"
    if args.site_dir is not None:
        os.environ["SITE_DIR"] = str(args.site_dir)
        config.SITE_DIR = args.site_dir

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
