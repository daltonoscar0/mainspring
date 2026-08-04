# Decisions

A running log of judgment calls, tradeoffs, and anything deferred or unverified.
Newest section last. Where a decision could reasonably have gone the other way,
the alternative is stated so it can be revisited without re-deriving the
argument.

---

## Scope

Milestone 1 only: RSS ingestion for free publications, local standfirsts, and a
static newspaper. Email ingestion (Milestone 2) is a stub with a fixed interface;
deployment (Milestone 3) is not started. Anything below marked **Deferred**
belongs to a later milestone or to a follow-up.

---

## Relationship to `the_mainspring` 1.x

The 1.x code was read and adapted rather than rewritten. What carried over,
sometimes reshaped:

| 1.x | 2.0 | Note |
|---|---|---|
| `scraper.py` `_extract_text` | `ingest/substack_html.py` | Rewritten from a denylist to an allowlist. See below. |
| `scraper.py` `FEEDS` dict | `subscriptions.yaml` | A hard-coded dict became user config, since the subscription list is the product. |
| `summarizer.py` | `summarize.py` | Same model, same beam-search parameters, same dev-mode shim. Output range narrowed. |
| `database.py` | `database.py` | Engine creation made lazy so `DB_PATH` can be set after import (the test suite needs this). |
| `main.py` pipeline | `app.py` + `scripts/build_edition.py` | Split: the CLI is the cron entry point, the app is the server. |
| `models.py` | `models.py` | Extended; see the schema section. |
| APScheduler every 6h, `ThreadPoolExecutor(4)` | unchanged | Both were reasonable and are kept. |

**Denylist to allowlist in the HTML cleaner.** 1.x removed elements whose class
matched a junk pattern and kept everything else. 2.0 keeps only tags on an
explicit allowlist and unwraps the rest. Two reasons: a denylist silently fails
open when a publication introduces new markup, and 2.0 writes the result into
static HTML served from the reader's own origin — an allowlist means a
publication cannot inject a `<script>` into the reader's site. It also disposes
of every `class`, `style` and `on*` attribute for free, so the stylesheet does
not have to defend against Substack's class names.

---

## Schema

**`summary` became `standfirst`, as a rename with no alias.** "Standfirst" is the
newspaper term for the line under a headline, which is what the column always
held; and the site generator's vocabulary should match the thing it builds. No
compatibility alias and no migration path: 2.0 has a different table shape
anyway (`Publication` FK, split body columns), so a 1.x `news.db` cannot be read
regardless. A fresh ingest rebuilds everything from feeds in seconds.

**`full_text` split into `body_html` and `body_text`.** One column cannot serve
both consumers: the site needs sanitized HTML with images and links intact, the
summarizer needs plain text. Deriving one from the other at read time would mean
re-parsing every body on every build.

**`source` became a `Publication` foreign key.** 1.x stored an outlet name as
free text that had to match a `FEEDS` key. A real key means renaming a
publication in `subscriptions.yaml` does not orphan its articles.

**`DailyDigest` became `DailyEdition`.** The digest concept — a second BART pass
summarizing the day — is gone. It was a paragraph nobody would read on a page
that already has forty headlines; the per-article standfirsts do that work. The
table now records what was published: date, lead article, count, build time.

**`url` remains the deduplication key.** Unchanged from 1.x, and now
load-bearing across ingest paths: a post that arrives by RSS today and by email
tomorrow has the same canonical URL, so Milestone 2 cannot duplicate the
archive. Tracking parameters are stripped when deriving the slug, so
`?utm_source=substack` does not fork a post into two pages.

**`(publication_id, slug)` is unique, not `slug` alone.** Two publications may
both post a "welcome". Within one publication a collision is real — Substack
truncates long titles when building a URL — so a numeric suffix is appended.

**Article dates are stored naive-UTC.** SQLite has no timezone-aware type.
Storing naive-UTC everywhere and converting once at the edge is less error-prone
than mixing.

---

## Ingestion

**Feeds are fetched, post pages usually are not.** Substack puts the whole post
in `content:encoded` for free publications, verified against the six seeded
feeds. So the normal path is one request per publication. A post page is fetched
only when a feed entry cleans down to fewer than
`rss.TEASER_WORD_THRESHOLD` (120) words. 120 was chosen from observation:
Substack teasers run 30–60 words, and a genuinely short post rarely falls below
120. A real short post that trips the threshold costs one wasted request and
keeps the feed body anyway, since the fallback only wins if it yields more text.

**Feeds are fetched concurrently, written serially.** Four threads, as in 1.x.
SQLite takes one writer at a time, and the write is microseconds against a
network fetch, so there is nothing to gain from contending for it.

**Re-ingest refreshes bodies but preserves standfirsts.** Posts get edited. Title
and byline are always refreshed; the body is only rewritten if it actually
changed, and only then is the standfirst cleared for regeneration. Without the
body comparison, every ingest would invalidate every dek and re-run the model
over the whole archive.

**Paid publications are declared but skipped.** `tier: paid` entries get a
`Publication` row and a page, so the site can show them as awaiting email
ingestion, but the RSS ingester passes over them — a paid feed carries only a
teaser, and storing teasers as articles would put stubs on the front page.

**A publication removed from `subscriptions.yaml` keeps its row and articles.**
Otherwise editing the file would punch holes in the archive. It simply stops
being polled.

**Session flushes before the dedup and slug-collision lookups.** Found by a test.
Sessions are created with `autoflush=False` and a whole feed is upserted before a
single commit, so without an explicit flush a collision between two entries *of
the same feed* was invisible to the lookup and surfaced as an `IntegrityError` at
commit time — losing that publication's entire batch rather than renaming one
page. The per-publication commit is now also wrapped, so a batch that fails
anyway costs one publication and not the run.

---

## Summarization

**Output narrowed to 25–90 tokens, from 1.x's 40–150.** A standfirst is one or
two sentences. At 150 tokens the dek competes with the headline for attention,
which on a front page reads as a layout error.

**Sampling is off; beam search only.** `num_beams=4`, no temperature, no
top-p — carried over from 1.x. The reason to keep it deliberate: generation must
be deterministic so a rebuild produces no diff. That is what makes committing
`site/` viable in Milestone 3.

**Generation never raises.** On any failure the article's opening sentences,
truncated at a sentence boundary, become the dek. A front page with a plain lede
is better than one with a hole, and better than a build that fails at 3am.

**`transformers` and `torch` are imported inside `load_model`, never at module
scope.** Ingestion, site generation, the API and the whole test suite therefore
run on a machine with neither installed. They are listed last in
`requirements.txt` with a comment saying so.

**Image captions are excluded from `body_text` but kept in `body_html`.** Found by
looking at output: Substack posts usually open with hero art, so the caption was
the first thing the summarizer read, and deks began "Photo by someone on
Unsplash". Captions still render on the article page — they are content for a
reader, just not signal for a summarizer.

---

## The front page

**An edition covers a window of the wire, not one calendar day.**
`EDITION_WINDOW_DAYS`, default 7. This is the biggest deviation from a literal
reading of the brief, and it was forced by the data. Measured over the six seeded
publications' feeds: 43 of 71 dates had exactly one article, 16 had two, and only
two dates reached six. A strict one-day edition is a single story — which is not
a front page, and cannot have "2–3 secondary stories, then a multi-column river".

So an edition is dated for its own day and draws on the preceding week. The lead
and secondary slots are reserved for that day's *fresh* stories wherever there
are any (`rank_articles(..., fresh=...)`); held stories fill the remaining slots
and the river. This is what a real paper does on a slow day. Set
`EDITION_WINDOW_DAYS=1` for strict single-day editions, which is honest but
usually thin. Tested both ways.

**Lead rotation is recomputed from scratch on every build, not read back from
`daily_editions`.** Editions are built oldest-first and the rotation accumulates
forward. Reading the previous run's rows would make the build depend on build
history, so the same database could produce different sites. Recomputing makes
the build a pure function of the data — which is what lets `site/` be committed
without diff noise, verified by a byte-identical-rebuild test.

**A publication that has never led goes first in the rotation.** A newly added
subscription reaches the front page immediately instead of waiting out the
cycle.

**Secondaries prefer one story per publication.** Not in the brief, which says
"secondaries by word count". Added because the real data breaks without it: Astral
Codex Ten averages 4,182 words with 19,000-word outliers, so pure length ranking
handed it the lead and all three secondary slots and the top of the page stopped
looking like a front page. Distinct publications are offered every slot first;
the rule relaxes rather than leave a hole. With three publications and four
headline slots exactly one repeats, which is tested.

**Ranking is a pure function over duck-typed objects.** It reads `url`, `title`,
`word_count`, `published_date` and a publication slug — nothing else, no session,
no clock. Every sort key ends in `url` so the ordering is total and input order
cannot affect output. Tested by ranking the same list forwards and backwards.

**Sort keys avoid `datetime.timestamp()`.** Found by a test: `datetime.min`
converts through local time and raises `ValueError: year 0 is out of range` on
this platform. Keys are computed from `toordinal()` instead, with a missing date
mapping to negative infinity so undated posts sink to the bottom of the river
rather than floating to the top.

---

## Design

The brief specified the visual direction — serif display headlines, hairline
rules, small caps section labels, near-black on off-white, one accent — so that
is what was built. Where it left an axis free:

**Typefaces are local stacks only; no webfonts.** A page that fetches its
typography from a CDN is not a private page, and there is no build step to
subset a font into the repo. Headlines are Times (the face cut for a newspaper),
body copy is Charter/Iowan Old Style — a book face with a larger x-height.
Display and text are deliberately different families rather than one family at
two sizes. Utility type (kickers, cutlines, folios) is a system grotesque,
uppercase and tracked out, the way a paper sets everything that is not headline
or text.

**The accent is process blue (`#1b3a6b`), not red or terracotta.** Two-colour
broadsheets printed black plus one spot ink and blue was as common as red. It
also keeps the page out of the warm-cream-and-terracotta register that generated
layouts default to.

**The colophon states real data, including `Circulation: 1`.** Edition number,
date, publication count, dispatch count, how many were filed today. It is the one
joke the product can make and it is literally true — this is a newspaper with one
subscriber, who is also the editor.

**No motion beyond link and focus states.** A newspaper does not animate.
`prefers-reduced-motion` is respected regardless, and there is a print
stylesheet.

**No JavaScript at all in the output.** The brief allowed "a small amount of
vanilla JS". None turned out to be necessary, so there is none. Every page is
static HTML plus one stylesheet.

**Templates never receive ORM objects.** `build.py` constructs plain view dicts
with pre-formatted dates. A template cannot then trigger a lazy load or reach a
column it should not display, and a page can be rendered in a test from a
literal dict. Jinja runs with `StrictUndefined`, so a typo in a template fails
the build instead of silently blanking a section.

**Autoescaping is on everywhere; `body_html` is marked safe at one line in
`build.py`.** That is the only trusted insertion, and it is trusted because the
cleaner's allowlist produced it. A headline containing `<script>` is escaped —
tested.

**Dates are formatted by hand, not with `%-d`/`%#d`.** Those flags are
platform-specific and would render differently on macOS and Windows.

---

## Site generation and deployment shape

**Every link is relative, derived from one `rel` prefix per page.** No base URL is
baked in anywhere, so the same output works at a domain root, under a GitHub
Pages project subpath (`/mainspring/`), and from `file://`. A test walks every
generated page and asserts that no internal link is root-relative and that every
one resolves to a file that exists.

**The build clears only the directories it owns** — `articles/`, `publications/`,
`editions/`, `static/` — and leaves anything else in `site/` alone. This is what
makes it safe to point `SITE_DIR` at a deploy checkout containing `.git` or a
`CNAME`. Tested. The alternative, building into a temp directory and swapping,
is atomic but would delete a `.git` directory that Milestone 3 may well need.

**`.nojekyll` is written on every build.** GitHub Pages otherwise runs Jekyll
over the output and drops any path beginning with an underscore.

**`site/` is gitignored.** Milestone 3 will publish it from a workflow artifact
rather than from the source history.

---

## Fixtures and verification

**The RSS and post-page fixtures are real, captured over the network during this
session.** `construction_physics_feed.xml` and `astral_codex_ten_feed.xml` are
unmodified Substack output trimmed to three `<item>` elements each;
`substack_post_page.html` is the `<article>` region of a real post.

**`substack_chrome_body.html` is hand-written, and this is the one synthesized
fixture.** No single real post contains a paywall *and* an email footer *and* a
poll embed — a free post has no paywall, a web post has no email footer. The
individual markup patterns are copied from real Substack output; the file's own
header comment says so. Every element that must survive is marked `KEEP` in its
text.

**All six seeded feeds were verified to resolve and to carry full post bodies.**
Read Max was in the initial list and was dropped: `readmax.com` is an unrelated
site, and the real publication has moved to Patreon (`maxread.substack.com`'s
latest post announces it). Silver Bulletin replaced it — 20 of 20 entries carry
full text. Nothing in the seed list is unverified.

**The test suite blocks the network at the socket layer.** An autouse fixture
patches `socket.connect` and `socket.create_connection` to raise, so a
regression that adds a request fails the test rather than quietly depending on
the machine being online. `DEV_MODE=1` is likewise forced for every test, so the
model is never reached for.

---

## Deferred

**Remote images leak the reader's IP to Substack's CDN.** This is the one open
tension with the privacy premise, and it is worth naming plainly: article bodies
reference `substackcdn.com` URLs, so viewing a page tells that CDN who is
reading. Mitigated but not solved — every page sets
`<meta name="referrer" content="no-referrer">` and every `<img>` carries
`referrerpolicy="no-referrer"`, so no URL or referrer is disclosed, but the
request still happens. The fix is to mirror images into `site/media/` at build
time and rewrite `src` attributes, which makes the site genuinely self-contained
and also makes it survive a publication deleting its art. Out of Milestone 1
scope; it is in the README roadmap.

**No authentication on the API.** `POST /api/refresh` is open to anyone who can
reach the port. `app.py`'s CLI defaults to `--host 127.0.0.1` for that reason
(1.x defaulted to `0.0.0.0`). Documented in the README.

**No pagination on publication pages.** Every article a publication has ever
posted is listed on one page. Fine at a few hundred; not at ten thousand.

**Full-text search.** Not attempted. Would have to be local — SQLite FTS5 is in
the standard library's `sqlite3`, so it needs no new dependency — but it implies
either JavaScript in the output or a server, and the output is deliberately
static.

**OPML import.** The obvious way to bulk-load `subscriptions.yaml` from an
existing reader. Small and self-contained; skipped only because it is not in
scope.

**Non-Substack feeds are untested but should work.** `parse_feed` uses generic
feedparser fields, and `extract_post_body` falls back through `<article>`,
`<main>` and `[role=main]`. Nothing rejects a non-Substack URL. Only Substack was
verified.

---

## Dependency justifications

Every dependency, and why it is not stdlib:

| Package | Why |
|---|---|
| `fastapi`, `uvicorn` | Carried from 1.x. Serves the built site and the JSON API. |
| `sqlalchemy` | Carried from 1.x. ORM over SQLite; no database server. |
| `feedparser` | Named in the brief. Real-world feeds are malformed in ways `xml.etree` will not survive. |
| `requests` | Carried from 1.x, for the teaser-fallback fetch. `urllib.request` would do, but `requests` is already present and handles redirects and timeouts more legibly. |
| `beautifulsoup4` + `lxml` | Carried from 1.x. The cleaner needs tree surgery — `unwrap`, `decompose` — that `html.parser` alone does not offer. |
| `python-dateutil` | Carried from 1.x. Fallback date parsing when feedparser cannot produce a `struct_time`. |
| `jinja2` | Named in the brief. Template inheritance and autoescaping; `string.Template` has neither. |
| `apscheduler` | Carried from 1.x. Named in the brief. |
| `PyYAML` | **New.** `subscriptions.yaml` is hand-edited config, and YAML tolerates comments — which the seeded file uses heavily to explain the fields. The alternatives were JSON (no comments, and trailing-comma hostile for a file people edit) and TOML (`tomllib` is stdlib in 3.11 but read-only, and the nested-list-of-tables syntax is worse for this shape). |
| `transformers`, `torch` | Named in the brief. Lazily imported; not needed in dev mode. |
| `pytest` | **New.** Test dependency. The brief asks for pytest. |

`scrapy` was in 1.x's `requirements.txt` and is dropped — it was never imported.

---

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `DEV_MODE` | unset | `1`/`true`/`yes` skips the model, uses `[DEV]` placeholder standfirsts. |
| `DB_PATH` | `news.db` | SQLite file location. |
| `SITE_DIR` | `site/` | Build output directory. |
| `SUBSCRIPTIONS_PATH` | `subscriptions.yaml` | Subscription list location. |
| `SITE_NAME` | `The Mainspring` | Masthead text. |
| `SITE_TAGLINE` | the Debs line | Strapline under the masthead. |
| `EDITION_WINDOW_DAYS` | `7` | Days of wire an edition carries. `1` for strict daily editions. |
| `FRONT_PAGE_LIMIT` | `40` | Maximum stories on one front page. |
| `REFRESH_INTERVAL_HOURS` | `6` | Scheduler interval. |
| `REQUEST_TIMEOUT` | `20` | Seconds per HTTP request. |
| `MAINSPRING_USER_AGENT` | descriptive UA | Sent on every request so publications can identify the bot. |

`DEV_MODE` is read at call time rather than import time, so tests can toggle it
with `monkeypatch.setenv` without reloading modules.
