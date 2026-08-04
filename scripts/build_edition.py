#!/usr/bin/env python3
"""build_edition.py — produce today's edition: ingest, summarize, build.

One command, three stages, and a nonzero exit if any of them fails. This is what
cron calls, and what the GitHub Actions workflow in Milestone 3 will call before
pushing ``site/`` to Pages.

    python scripts/build_edition.py                # everything
    DEV_MODE=1 python scripts/build_edition.py     # placeholder standfirsts
    python scripts/build_edition.py --skip-ingest   # rebuild from what is stored
    python scripts/build_edition.py --offline       # no network at all

Exit codes:
    0  every stage succeeded
    1  a stage failed — the log says which
    2  the run was interrupted
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

# Allow running the script directly from a checkout, without installing.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mainspring import config  # noqa: E402
from mainspring.database import init_db, session  # noqa: E402
from mainspring.ingest import rss  # noqa: E402
from mainspring.sitegen import build as sitegen  # noqa: E402
from mainspring.summarize import generate_standfirsts  # noqa: E402

logger = logging.getLogger("build_edition")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Define and parse the command line."""
    parser = argparse.ArgumentParser(
        description="Ingest subscriptions, write standfirsts, and build the static site.",
    )
    parser.add_argument(
        "--skip-ingest",
        action="store_true",
        help="Do not poll feeds. Rebuild from articles already stored.",
    )
    parser.add_argument(
        "--skip-summarize",
        action="store_true",
        help="Do not generate standfirsts. Articles without one render without a dek.",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Make no network requests. Implies --skip-ingest unless a feed is cached.",
    )
    parser.add_argument(
        "--dev",
        action="store_true",
        help="Shorthand for DEV_MODE=1: skip the model, use placeholder standfirsts.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Generate at most N standfirsts this run. Useful on a first run.",
    )
    parser.add_argument(
        "--site-dir",
        type=Path,
        default=None,
        help=f"Output directory (default: {config.SITE_DIR}).",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Log warnings and errors only.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the pipeline. Returns the process exit code."""
    args = parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.dev:
        # Set before anything reads it — config.dev_mode() checks at call time.
        os.environ["DEV_MODE"] = "1"

    failures: list[str] = []

    init_db()

    # --- 1. ingest ---------------------------------------------------------
    if args.skip_ingest:
        logger.info("Skipping ingest.")
    else:
        try:
            subscriptions = config.load_subscriptions()
        except config.SubscriptionsError as exc:
            logger.error("Cannot read subscriptions: %s", exc)
            return 1

        report = rss.ingest(subscriptions, allow_fetch=not args.offline)
        logger.info("Ingest: %s", report.summary())
        if not report.ok:
            # A single unreachable feed should not fail the whole edition — the
            # other publications still have news. It is reported, and the exit
            # code reflects it, so a cron run surfaces the problem.
            for error in report.errors:
                logger.error("Ingest error: %s", error)
            failures.append(f"{len(report.errors)} feed(s) failed")

    # --- 2. standfirsts ----------------------------------------------------
    if args.skip_summarize:
        logger.info("Skipping standfirst generation.")
    else:
        db = session()
        try:
            written = generate_standfirsts(db, limit=args.limit)
            logger.info("Standfirsts: %d written.", written)
        except Exception as exc:  # noqa: BLE001 — reported, then the build still runs
            logger.error("Standfirst generation failed: %s", exc)
            failures.append("standfirst generation failed")
        finally:
            db.close()

    # --- 3. build ----------------------------------------------------------
    try:
        result = sitegen.build(site_dir=args.site_dir)
    except Exception as exc:  # noqa: BLE001 — a failed build is a failed run
        logger.exception("Build failed: %s", exc)
        return 1

    logger.info("Build: %s", result.summary())
    if not result.ok:
        for error in result.errors:
            logger.error("Build error: %s", error)
        failures.append("build reported errors")

    if failures:
        logger.error("Finished with problems: %s", "; ".join(failures))
        return 1

    logger.info("Edition ready in %s", result.site_dir)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        sys.exit(2)
