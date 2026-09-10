"""database.py — SQLAlchemy engine, session factory, and schema initializer.

A single SQLite file (``news.db`` by default) holds everything: publications,
articles, standfirsts and edition records. No database server, no migrations
framework, no ORM magic beyond ``declarative_base``.

The engine is created lazily so that setting ``DB_PATH`` after import — which
is what the test suite does — still takes effect.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from . import config

logger = logging.getLogger(__name__)

#: Declarative base shared by every model in :mod:`mainspring.models`.
Base = declarative_base()

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None
_engine_path: Path | None = None


def _resolve_db_path() -> Path:
    """Return the SQLite file path, honouring a late DB_PATH override."""
    override = os.environ.get("DB_PATH")
    return Path(override) if override else config.DB_PATH


def get_engine() -> Engine:
    """Return the process-wide SQLite engine, creating it on first use.

    If ``DB_PATH`` changes between calls the old engine is disposed and a new
    one built. That only happens in tests, but it keeps the two from silently
    disagreeing about which file they are writing to.

    ``check_same_thread=False`` is required because the ingest thread pool and
    APScheduler each open their own Session on a different thread. Sessions are
    never shared across threads, so this is safe.
    """
    global _engine, _session_factory, _engine_path

    path = _resolve_db_path()
    if _engine is not None and _engine_path == path:
        return _engine

    if _engine is not None:
        _engine.dispose()

    path.parent.mkdir(parents=True, exist_ok=True)
    _engine = create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False},
    )
    _session_factory = sessionmaker(autocommit=False, autoflush=False, bind=_engine)
    _engine_path = path
    return _engine


def session() -> Session:
    """Open a new Session. The caller is responsible for closing it.

    Prefer this over holding a module-level session: every worker thread and
    every request gets its own.
    """
    get_engine()
    assert _session_factory is not None  # set by get_engine()
    return _session_factory()


def init_db() -> None:
    """Create any tables that do not exist yet, and add any missing columns.

    Safe to call on every startup. Importing :mod:`mainspring.models` here
    registers the table definitions on ``Base.metadata`` before ``create_all``
    runs.
    """
    from . import models  # noqa: F401, registers tables on Base.metadata

    engine = get_engine()
    Base.metadata.create_all(bind=engine)
    _add_missing_columns(engine)


def _add_missing_columns(engine: Engine) -> None:
    """Add columns present on the models but not yet in the database file.

    ``create_all`` creates missing *tables* but never alters existing ones, so a
    new column on an existing install would otherwise fail every query with
    "no such column". This is not a migration framework and does not pretend to
    be — it handles the additive case, which is the only kind of change made so
    far, and leaves anything else alone.

    The alternative was telling people to delete ``news.db``, which throws away
    the archive: feeds only carry the last twenty-odd posts, so everything older
    exists nowhere else.

    SQLite requires a DEFAULT when adding a NOT NULL column, so columns without
    one are added nullable and the model's Python-side default fills them on the
    next write.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue

        present = {column["name"] for column in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in present:
                continue

            column_type = column.type.compile(engine.dialect)
            clause = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {column_type}'

            default = getattr(column.server_default, "arg", None)
            if default is not None:
                clause += f" NOT NULL DEFAULT '{default}'" if not column.nullable else f" DEFAULT '{default}'"

            with engine.begin() as connection:
                connection.execute(text(clause))
            logger.info("Added column %s.%s", table.name, column.name)


def get_db():
    """FastAPI dependency yielding a Session that is always closed.

    Usage::

        @app.get("/api/articles")
        def articles(db: Session = Depends(get_db)):
            ...
    """
    db = session()
    try:
        yield db
    finally:
        db.close()


def reset_engine() -> None:
    """Drop the cached engine so the next call rebuilds it.

    Only used by tests that point ``DB_PATH`` at a fresh temporary file.
    """
    global _engine, _session_factory, _engine_path
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
    _engine_path = None
