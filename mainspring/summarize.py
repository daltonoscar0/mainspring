"""summarize.py — generate standfirsts locally.

A standfirst (or dek) is the line of context that sits under a headline and
tells you whether to read on. Mainspring writes one for every article by running
a summarization model on the article body.

The model runs on your hardware. ``transformers`` and ``torch`` are imported
lazily inside :func:`load_model`, never at module import, so the rest of
Mainspring — ingestion, site generation, the API, the whole test suite — works
on a machine that has neither installed.

Model: ``sshleifer/distilbart-cnn-12-6``
    BART-large distilled and fine-tuned on CNN/DailyMail. News-shaped prose in,
    news-shaped summary out, at roughly twice the speed of the undistilled
    model. Downloaded once (~1.6 GB) into the HuggingFace cache and loaded from
    disk thereafter.

Dev mode
    ``DEV_MODE=1`` skips the model entirely and produces a ``[DEV]`` placeholder
    from the opening of the article. Layout work and the test suite need a
    plausible-length dek, not a good one.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy.orm import Session

from . import config
from .models import Article

logger = logging.getLogger(__name__)

#: HuggingFace model id. Local cache only — nothing is sent anywhere.
MODEL_NAME = "sshleifer/distilbart-cnn-12-6"

#: Output length bounds in tokens. A standfirst is one or two sentences; the old
#: 40–150 range produced deks that overwhelmed the headline, so the ceiling is
#: lower here.
MIN_TOKENS = 25
MAX_TOKENS = 90

#: BART's encoder maximum. Longer bodies are truncated, which is fine: the top of
#: a Substack post is almost always where its thesis is.
MAX_INPUT_TOKENS = 1024

#: Bodies shorter than this are not worth summarizing — the opening sentences
#: already are the summary.
MIN_BODY_CHARS = 240

#: Words in a dev-mode placeholder.
DEV_WORDS = 30

#: Concurrency for standfirst generation. Torch already parallelizes within a
#: single forward pass, so this is about overlapping tokenization and DB work
#: rather than adding compute.
SUMMARIZE_WORKERS = 4

_model = None
_tokenizer = None


# ---------------------------------------------------------------------------
# Model lifecycle
# ---------------------------------------------------------------------------

def load_model() -> None:
    """Load the summarization model into memory.

    Called once at startup and again before a batch run. Repeat calls are
    no-ops. In dev mode this returns immediately without importing torch.

    Raises:
        RuntimeError: if ``transformers``/``torch`` are missing, or the weights
            cannot be loaded. Startup fails loudly rather than silently
            producing empty deks.
    """
    global _model, _tokenizer

    if config.dev_mode():
        logger.info("DEV_MODE — skipping model load, using placeholder standfirsts.")
        return

    if _model is not None:
        return

    try:
        from transformers import BartForConditionalGeneration, BartTokenizer
    except ImportError as exc:
        raise RuntimeError(
            "transformers and torch are needed to generate standfirsts. "
            "Install them with `pip install -r requirements.txt`, or set "
            "DEV_MODE=1 to use placeholders."
        ) from exc

    logger.info("Loading %s — first run downloads ~1.6 GB.", MODEL_NAME)
    try:
        _tokenizer = BartTokenizer.from_pretrained(MODEL_NAME)
        _model = BartForConditionalGeneration.from_pretrained(MODEL_NAME)
        # eval() disables dropout, making inference faster and deterministic.
        _model.eval()
    except Exception as exc:  # noqa: BLE001 — surfaced as a startup failure
        raise RuntimeError(f"Could not load {MODEL_NAME}: {exc}") from exc

    logger.info("Model ready.")


def model_loaded() -> bool:
    """True when real inference is available on this process."""
    return _model is not None and _tokenizer is not None


# ---------------------------------------------------------------------------
# Text preparation
# ---------------------------------------------------------------------------

def _first_sentences(text: str, limit: int = 320) -> str:
    """Return whole sentences from the start of ``text``, up to ``limit`` chars.

    Used for bodies too short to summarize and as the fallback when generation
    fails. Cutting on a sentence boundary matters — a dek ending mid-clause
    reads like a bug, which on a newspaper front page it looks like too.
    """
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat

    window = flat[:limit]
    cut = max(window.rfind(". "), window.rfind("? "), window.rfind("! "))
    if cut > limit // 3:
        return window[: cut + 1]
    return window.rsplit(" ", 1)[0] + "…"


def _placeholder(text: str) -> str:
    """Build a dev-mode standfirst from the opening of the article.

    Prefixed with ``[DEV]`` so a placeholder is never mistaken for real output
    when reviewing a built site.
    """
    words = " ".join(text.split()[:DEV_WORDS])
    return f"[DEV] {words}…" if words else "[DEV] No body text."


def _tidy(summary: str) -> str:
    """Clean up model output for display under a headline.

    BART occasionally emits a leading space, doubled punctuation, or a trailing
    fragment with no terminator. None of that is wrong enough to reject the
    summary, and all of it is visible at display size.
    """
    text = " ".join(summary.split())
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"([.!?]){2,}", r"\1", text)
    if text and text[-1] not in ".!?…":
        text += "."
    return text


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

def standfirst(text: str) -> str:
    """Generate one standfirst from article body text.

    Args:
        text: Plain-text article body — the ``body_text`` column, i.e. the
              cleaner's output rather than raw HTML.

    Returns:
        A one-to-two sentence dek. Never raises and never returns an empty
        string for non-empty input: if the model fails, the article's opening
        sentences are used instead, because a front page with a missing dek is
        worse than one with a plain lede.

    Generation settings:
        ``num_beams=4`` — beam search over greedy decoding; slower, noticeably
        more fluent. ``no_repeat_ngram_size=3`` — blocks the repeated trigrams
        BART otherwise produces on long inputs. ``early_stopping=True`` — stop
        once every beam has finished. Sampling is deliberately off, so the same
        body always yields the same dek and a rebuild produces no diff.
    """
    if not text or not text.strip():
        return ""

    if config.dev_mode():
        return _placeholder(text)

    if len(text.strip()) < MIN_BODY_CHARS:
        return _first_sentences(text)

    if not model_loaded():
        raise RuntimeError("Model not loaded. Call load_model() first.")

    try:
        import torch

        inputs = _tokenizer(  # type: ignore[misc]
            text,
            max_length=MAX_INPUT_TOKENS,
            truncation=True,
            return_tensors="pt",
        )

        with torch.no_grad():
            output_ids = _model.generate(  # type: ignore[union-attr]
                inputs["input_ids"],
                attention_mask=inputs["attention_mask"],
                num_beams=4,
                min_length=MIN_TOKENS,
                max_length=MAX_TOKENS,
                length_penalty=1.0,
                no_repeat_ngram_size=3,
                early_stopping=True,
            )

        decoded = _tokenizer.decode(output_ids[0], skip_special_tokens=True)  # type: ignore[union-attr]
        tidied = _tidy(decoded)
        return tidied or _first_sentences(text)

    except Exception as exc:  # noqa: BLE001 — degrade to the lede, never crash a build
        logger.warning("Standfirst generation failed, falling back to lede: %s", exc)
        return _first_sentences(text)


def generate_standfirsts(
    db: Session,
    *,
    limit: int | None = None,
    workers: int = SUMMARIZE_WORKERS,
) -> int:
    """Fill in the standfirst for every article that lacks one.

    Idempotent: articles that already have a standfirst are not touched, so this
    is safe to call before every build. Articles whose body is empty get their
    ``summarizing`` flag cleared and are left without a dek rather than being
    retried forever.

    Args:
        db:      Open session, used to select the work. Each worker opens its own
                 session to write, because SQLAlchemy Sessions are not
                 thread-safe.
        limit:   Cap on articles processed this run. Useful on first run against
                 a large archive, where the model is the slow part.
        workers: Thread pool size. 1 runs inline, which keeps tracebacks
                 readable when debugging.

    Returns:
        How many standfirsts were written.
    """
    load_model()

    query = (
        db.query(Article.id)
        .filter(Article.standfirst.is_(None))
        .order_by(Article.published_date.desc().nullslast(), Article.id.desc())
    )
    if limit is not None:
        query = query.limit(limit)

    pending = [row[0] for row in query.all()]
    if not pending:
        logger.info("No standfirsts to generate.")
        return 0

    logger.info("Generating %d standfirst(s).", len(pending))

    if workers <= 1:
        written = sum(_generate_one(article_id) for article_id in pending)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            written = sum(pool.map(_generate_one, pending))

    logger.info("Wrote %d standfirst(s).", written)
    return written


def _generate_one(article_id: int) -> int:
    """Generate and store one article's standfirst. Returns 1 on success, else 0.

    Runs on a worker thread with its own session. The ``summarizing`` flag is
    always cleared on the way out, including on failure, so an article can never
    get stuck reporting itself as in progress.
    """
    from .database import session

    db = session()
    try:
        article = db.get(Article, article_id)
        if article is None or article.standfirst:
            return 0

        article.summarizing = True
        db.commit()

        body = article.body_text or ""
        if not body.strip():
            article.summarizing = False
            db.commit()
            return 0

        article.standfirst = standfirst(body)
        article.summarizing = False
        db.commit()
        logger.debug("Standfirst: %s", article.title[:60])
        return 1

    except Exception as exc:  # noqa: BLE001 — one bad article must not end the batch
        logger.error("Failed on article %s: %s", article_id, exc)
        db.rollback()
        try:
            article = db.get(Article, article_id)
            if article is not None:
                article.summarizing = False
                db.commit()
        except Exception:  # noqa: BLE001 — best effort
            pass
        return 0
    finally:
        db.close()
