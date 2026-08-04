"""substack_html.py — turn Substack markup into a clean article body.

This is the one place that understands what Substack's HTML looks like. Both
ingest paths route through it:

  * RSS gives us the whole post inside ``<content:encoded>``.
  * A paid post's email (Milestone 2) gives us near-identical markup wrapped in
    table-based email chrome.

Either way the job is the same — throw away the furniture, keep the writing.

The cleaner is allowlist-based. Tags outside :data:`ALLOWED_TAGS` are unwrapped
rather than deleted, so a paragraph nested six ``<div>``s deep still survives,
while ``<script>``, ``<form>`` and friends are removed outright. That also means
the HTML we write into the static site can never carry executable content from
a publication, which matters because the generated pages are served from the
same origin as everything else you host.

What counts as furniture:
  subscribe widgets and "Subscribe now" buttons, share buttons and share
  dialogs, "Leave a comment" / "View comments" calls to action, like/restack
  bars, paywall interstitials, poll and post-recommendation embeds, the email
  footer with its unsubscribe links, and tracking pixels.

What is kept:
  headings, paragraphs, blockquotes, lists, images (with ``src`` and ``alt``)
  and their captions, links, inline emphasis, code, tables and horizontal rules.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, NavigableString, Tag

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Allowlists
# ---------------------------------------------------------------------------

#: Tags that survive cleaning. Everything else is unwrapped (children kept) or,
#: if it matched a chrome rule, removed with its subtree.
ALLOWED_TAGS: frozenset[str] = frozenset(
    {
        "p", "h1", "h2", "h3", "h4", "h5", "h6",
        "blockquote", "q", "cite",
        "ul", "ol", "li",
        "a", "img", "figure", "figcaption",
        "em", "strong", "i", "b", "u", "s", "del", "ins", "small", "sup", "sub",
        "code", "pre", "kbd", "samp",
        "hr", "br",
        "table", "thead", "tbody", "tfoot", "tr", "th", "td", "caption",
    }
)

#: Per-tag attribute allowlist. Anything not listed here is dropped, including
#: every ``class``, ``style``, ``id`` and ``on*`` handler.
ALLOWED_ATTRS: dict[str, frozenset[str]] = {
    "a": frozenset({"href", "title"}),
    "img": frozenset({"src", "alt", "title"}),
    "th": frozenset({"colspan", "rowspan", "scope"}),
    "td": frozenset({"colspan", "rowspan"}),
    "ol": frozenset({"start"}),
}

#: Tags removed along with everything inside them.
DROP_TAGS: frozenset[str] = frozenset(
    {
        "script", "style", "noscript", "template",
        "form", "button", "input", "select", "textarea", "label",
        "iframe", "object", "embed", "svg", "canvas", "video", "audio", "source",
        "nav", "footer", "header", "aside",
        "meta", "link",
    }
)

#: Block elements that force a paragraph break in the plain-text rendering.
_TEXT_BLOCKS: frozenset[str] = frozenset(
    {
        "p", "h1", "h2", "h3", "h4", "h5", "h6",
        "blockquote", "li", "pre", "tr", "caption",
    }
)

#: Elements kept in the HTML but left out of the plain text.
#:
#: Captions only. A Substack post very often opens with its hero image, so its
#: caption would otherwise be the first thing the summarizer reads — and a
#: standfirst that begins "Photo by someone on Unsplash" is worse than no
#: standfirst. The caption still renders on the article page.
_TEXT_EXCLUDED: frozenset[str] = frozenset({"figcaption"})

# ---------------------------------------------------------------------------
# Chrome detection
# ---------------------------------------------------------------------------

#: Substack (and generic newsletter) class/id fragments that mark furniture.
#: Matched case-insensitively against the space-joined class list and the id.
_CHROME_CLASS_RE = re.compile(
    r"(?:"
    r"subscription-widget|subscribe-widget|subscribe-prompt|subscribe-footer|"
    r"subscribe-dialog|signup-widget|"
    r"share-dialog|share-wrap|share-button|post-share|social-share|"
    r"like-button|restack|comments-wrap|comment-input|comment-cta|post-ufi|"
    r"paywall|gate-wrap|"
    r"poll-embed|digest-post-embed|embedded-publication|publication-embed|"
    r"recommendation|footer-wrap|email-footer|post-footer|meta-footer|"
    r"unsubscribe|preferences-link|"
    r"button-wrapper|pencraft-button|"
    r"tracking-pixel|beacon|"
    # Non-Substack news furniture. These sites fund themselves with appeals and
    # syndicate under licences, and both live inside the article element — so
    # without these the summarizer reads a donation pitch as the lede.
    # Each of these is deliberately qualified rather than a bare word. A bare
    # "newsletter" matched Substack's own `newsletter-post` article wrapper and
    # deleted every post; the size guard in strip_chrome now catches that class
    # of mistake, but the patterns should not rely on it.
    r"donat(?:e|ion)[-_]|[-_]donat(?:e|ion)|fundrais|support-us|"
    r"membership[-_]|pledge[-_]|"
    r"republish|reprint[-_]|copyright-notice|disclaimer|"
    r"newsletter[-_](?:signup|sign-up|form|cta|widget|box|prompt|banner)|"
    r"(?:email|newsletter)[-_]capture|signup[-_]|sign-up[-_]|"
    r"breadcrumb|site-nav|main-nav|nav-menu|menu-toggle|skip-link|"
    r"related-(?:post|stori|articl)|more-from|read-next|recirc|"
    r"author-bio|contributor-bio|byline-bio|"
    r"tag-list|topic-list|category-list|"
    r"letter-to-the-editor|"
    # Injected promo slots. Publishers splice fundraising and cross-promotion
    # into the middle of the body through named placeholders, so the appeal is
    # inside the article element and reads as its opening sentence. Truthout's
    # `truth-post-content-before truth-entity-placement` wrapping an
    # `inline-callout` is the shape; these names are generic across CMSs.
    r"entity-placement|[-_]placement\b|content-before|content-after|"
    r"inline-callout|callout--text|"
    r"lightbox|offcanvas|[-_]modal|modal[-_]"
    r")",
    re.IGNORECASE,
)

#: A "chrome" element holding at least this share of the document's text is not
#: chrome — the pattern misfired. Furniture is small; the article is not.
_CHROME_MAX_TEXT_SHARE = 0.4

#: Below this much total text the share guard is meaningless and is skipped. A
#: fragment consisting only of a subscribe widget holds 100% of its own text and
#: must still be removed.
_CHROME_GUARD_MIN_TEXT = 400

#: Reject a candidate body container whose text is more than this proportion
#: anchor text.
#:
#: Prose is mostly words; navigation, "related stories" rails and link indexes are
#: mostly links. Democracy Now's transcript pages are the case that motivated
#: this — a sidebar of guests, topics and links matched a body selector and gave
#: the summarizer "Related / Topics / Guests / Links / Transcript" as an article.
#: Site-agnostic, so it protects against layouts nobody has looked at yet.
_MAX_LINK_TEXT_SHARE = 0.5


def _link_share(container: Tag) -> float:
    """Return the proportion of a container's text that sits inside links."""
    total = len(container.get_text(strip=True))
    if not total:
        return 1.0
    linked = sum(len(a.get_text(strip=True)) for a in container.find_all("a"))
    return linked / total

#: Link targets that mean the anchor is a call to action, not a citation.
_CHROME_HREF_RE = re.compile(
    r"(?:"
    r"/subscribe\b|action=share|/comments?\b|utm_source=substack&utm_medium=email"
    r"|/account/?$|/unsubscribe\b|substack\.com/app-link|/refer\b|/leaderboard\b"
    r")",
    re.IGNORECASE,
)

#: Anchor text that means the anchor is a call to action. Compared against the
#: anchor's stripped, lowercased text, so it only fires on short button-like
#: labels — a sentence that happens to contain "subscribe" is left alone.
_CHROME_LINK_TEXT = frozenset(
    {
        "subscribe", "subscribe now", "subscribe for free", "upgrade to paid",
        "upgrade your subscription", "become a paid subscriber", "get 20% off",
        "share", "share this post", "share on twitter", "share on facebook",
        "leave a comment", "leave a comment »", "view comments", "post a comment",
        "comment", "comments", "read in app", "open in app", "like",
        "restack", "unsubscribe", "manage your subscription", "view profile",
        "give a gift subscription", "refer a friend", "no thanks",
    }
)

#: Selectors for the container holding a post's body, most specific first.
#:
#: The first four are Substack: ``.available-content`` is what a free (or
#: unlocked) post uses and ``.body.markup`` is the inner wrapper. The rest cover
#: the news CMSs people actually point Mainspring at — WordPress, Drupal, and a
#: few hand-rolled ones — because a self-hosted reader should not refuse to read
#: anything that is not a Substack.
#:
#: Order matters and specificity wins: a generic ``article`` or ``main`` on a news
#: site frequently wraps the body *plus* donation appeals, republication notices
#: and a related-stories rail, so those two sit last.
_BODY_SELECTORS: tuple[str, ...] = (
    # Substack
    "div.available-content",
    "div.body.markup",
    "div.markup",
    # Semantic standard, when a publisher bothers
    "[itemprop='articleBody']",
    # Common CMS body wrappers
    "div.entry-content",
    "div.post-content",
    "div.article-body",
    "div.article__body",
    "div.article-content",
    "div.ar__con",             # Truthout
    "div.story-body",
    "div.post-body",
    "div.field--name-body",    # Drupal
    "div.node__content",       # Drupal
    # Id-form equivalents. Older CMSs identify the body by id rather than class,
    # and a selector list that only looks at classes silently falls through to
    # <main> and swallows the site navigation.
    "#story_text",             # Democracy Now
    "#article-body",
    "#articleBody",
    "#post-content",
    "#entry-content",
    "#story",
    "#main-content",
    # Last resorts, which over-capture on many news sites
    "article",
    "main",
    "[role='main']",
)

#: Hosts/paths that only ever serve tracking pixels.
_PIXEL_RE = re.compile(
    r"(?:substack\.com/api/v\d|/o/|open\?token=|/pixel|\bbeacon\b|email-open)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CleanedBody:
    """The result of cleaning one post body.

    Attributes:
        html:            Sanitized HTML fragment, safe to write into a page.
        text:            Plain-text rendering, one block per line. This is what
                         the summarizer sees.
        hero_image_url:  First content image in the body, or None. The front
                         page uses it for the lead slot; the article page just
                         renders ``html``, which still contains the image in
                         its original position.
        hero_image_alt:  That image's alt text, or None.
        word_count:      Whitespace-delimited word count of ``text``.
    """

    html: str
    text: str
    hero_image_url: str | None
    hero_image_alt: str | None
    word_count: int

    @property
    def is_empty(self) -> bool:
        """True when cleaning produced nothing worth publishing."""
        return self.word_count == 0


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _classes_and_id(tag: Tag) -> str:
    """Return a single searchable string of a tag's class list and id."""
    raw_class = tag.get("class") or []
    classes = " ".join(raw_class) if isinstance(raw_class, list) else str(raw_class)
    return f"{classes} {tag.get('id') or ''}"


def _is_chrome(tag: Tag) -> bool:
    """Decide whether a tag is Substack furniture rather than article content."""
    if _CHROME_CLASS_RE.search(_classes_and_id(tag)):
        return True

    # Substack marks its widgets with data-component-name, e.g.
    # data-component-name="SubscribeWidget" / "ShareButton".
    component = str(tag.get("data-component-name") or "")
    if component and re.search(r"(subscribe|share|comment|paywall|poll)", component, re.I):
        return True

    if tag.name == "a":
        href = str(tag.get("href") or "")
        text = tag.get_text(" ", strip=True).lower().strip("  →»›")
        if text in _CHROME_LINK_TEXT:
            return True
        # A short anchor pointing at a subscribe/share/comment endpoint is a
        # button. A long one is prose that happens to link there, so the length
        # guard keeps real sentences intact.
        if href and _CHROME_HREF_RE.search(href) and len(text) <= 40:
            return True

    return False


def _is_tracking_pixel(tag: Tag) -> bool:
    """True for 1x1 beacons and known open-tracking endpoints."""
    src = str(tag.get("src") or "")
    if not src or _PIXEL_RE.search(src):
        return True
    for dimension in ("width", "height"):
        value = str(tag.get(dimension) or "").strip()
        if value.isdigit() and int(value) <= 2:
            return True
    return False


def _best_image_src(tag: Tag, base_url: str) -> str | None:
    """Pick the best URL for an ``<img>``, resolving it against ``base_url``.

    Substack lazy-loads images, so the real URL is sometimes in ``data-src``
    while ``src`` holds a placeholder. ``srcset`` candidates are ignored: the
    plain ``src`` is already a CDN-resized image and picking a width here would
    bake a layout assumption into the database.
    """
    for attr in ("src", "data-src", "data-original"):
        candidate = str(tag.get(attr) or "").strip()
        if candidate and not candidate.startswith("data:"):
            return urljoin(base_url, candidate) if base_url else candidate
    return None


def strip_chrome(root: Tag) -> None:
    """Delete banned tags and furniture subtrees from ``root``, in place.

    Split out from :func:`_sanitize` so a full page can be de-chromed *before* a
    body container is chosen. That ordering matters more than it looks: on a news
    site the outer ``<article>`` often wraps the body plus a donation appeal, a
    republication notice and a related-stories rail, so choosing the container
    first and cleaning second hands the summarizer a fundraising pitch as the
    lede. Cleaning first shrinks those wrappers to their actual content and lets
    container selection work on markup that is already honest.

    Subtrees are removed whole, so a chrome ``<div>`` takes its children with it
    rather than leaking a stray "Subscribe now" paragraph.

    One safeguard makes this safe to extend: an element matching a chrome pattern
    is spared if it holds more than :data:`_CHROME_MAX_TEXT_SHARE` of the
    document's text. Furniture is small. Anything holding most of the page is the
    article, and the pattern misfired — which is exactly what happened when a
    bare ``newsletter`` pattern met Substack's ``newsletter-post`` wrapper and
    silently deleted every post. Any denylist accumulates that mistake; this
    turns it from data loss into a no-op.
    """
    for tag in root.find_all(list(DROP_TAGS)):
        tag.decompose()

    total_text = len(root.get_text(strip=True))
    budget = (
        total_text * _CHROME_MAX_TEXT_SHARE
        if total_text >= _CHROME_GUARD_MIN_TEXT
        else 0
    )

    for tag in root.find_all(True):
        if tag.decomposed:
            continue
        if not _is_chrome(tag):
            continue
        if budget and len(tag.get_text(strip=True)) > budget:
            logger.debug(
                "Sparing <%s class=%r> from chrome removal: holds %d of %d chars",
                tag.name,
                tag.get("class"),
                len(tag.get_text(strip=True)),
                total_text,
            )
            continue
        tag.decompose()


def _sanitize(root: Tag, base_url: str) -> None:
    """Strip chrome and non-allowlisted markup from ``root``, in place."""
    # Pass 1 — remove banned tags and furniture. Idempotent, so it costs nothing
    # when extract_post_body has already run it over the whole document.
    strip_chrome(root)

    # Pass 2 — images: resolve URLs, drop pixels, normalize attributes.
    for img in root.find_all("img"):
        if _is_tracking_pixel(img):
            img.decompose()
            continue
        src = _best_image_src(img, base_url)
        if not src:
            img.decompose()
            continue
        alt = str(img.get("alt") or "").strip()
        title = str(img.get("title") or "").strip()
        img.attrs = {"src": src, "alt": alt}
        if title:
            img.attrs["title"] = title

    # Pass 3 — links: resolve relative hrefs, drop javascript: targets, and
    # mark external links so the template can add rel/target consistently.
    for anchor in root.find_all("a"):
        href = str(anchor.get("href") or "").strip()
        if not href or href.lower().startswith(("javascript:", "vbscript:", "data:")):
            anchor.unwrap()
            continue
        anchor.attrs = {"href": urljoin(base_url, href) if base_url else href}

    # Pass 4 — everything else: keep allowlisted attributes, unwrap unknown
    # tags. Iterating over a materialized list because unwrap() mutates the tree.
    for tag in list(root.find_all(True)):
        if tag.decomposed:
            continue
        if tag.name not in ALLOWED_TAGS:
            tag.unwrap()
            continue
        if tag.name in ("a", "img"):
            continue  # already normalized above
        allowed = ALLOWED_ATTRS.get(tag.name, frozenset())
        tag.attrs = {k: v for k, v in tag.attrs.items() if k in allowed}

    # Pass 5 — collapse the debris: elements left with neither text nor an
    # image after all that removal.
    _drop_empty(root)


def _drop_empty(root: Tag) -> None:
    """Remove elements that ended up carrying no content.

    Runs innermost-first so that emptying a child can empty its parent in the
    same pass — a ``<blockquote>`` whose only ``<p>`` was a share button should
    disappear, not linger as an empty rule.
    """
    void = {"img", "br", "hr"}
    for tag in reversed(list(root.find_all(True))):
        if tag.decomposed or tag.name in void:
            continue
        if tag.find(list(void)):
            continue
        if not tag.get_text(strip=True):
            tag.decompose()


def _to_text(root: Tag) -> str:
    """Render a cleaned fragment as plain text, one block per line.

    Block boundaries matter: BART reads this, and running a headline into the
    following sentence produces noticeably worse standfirsts than keeping them
    apart.
    """
    parts: list[str] = []

    def walk(node: Tag) -> None:
        for child in node.children:
            if isinstance(child, NavigableString):
                text = re.sub(r"\s+", " ", str(child)).strip()
                if text:
                    parts.append(text)
            elif isinstance(child, Tag):
                if child.name in _TEXT_EXCLUDED:
                    continue
                if child.name in _TEXT_BLOCKS:
                    # Collapse whitespace *inside* the block before adding the
                    # boundary markers, so source line wrapping inside a
                    # paragraph does not become a spurious block break.
                    inner = re.sub(r"\s+", " ", child.get_text(" ", strip=True)).strip()
                    if inner:
                        parts.append("\n" + inner + "\n")
                elif child.name in ("br", "hr"):
                    parts.append("\n")
                else:
                    walk(child)

    walk(root)

    joined = " ".join(parts)
    # Collapse runs of whitespace, then runs of newlines, so the result is a
    # tidy block-per-line document rather than a ragged one.
    joined = re.sub(r"[ \t]+", " ", joined)
    joined = re.sub(r" *\n *", "\n", joined)
    joined = re.sub(r"\n{2,}", "\n", joined)
    # Inline links contribute their own separator, which leaves a space before
    # the punctuation that followed them ("via WikiArt ."). Reattach it.
    joined = re.sub(r" +([,.;:!?%)\]])", r"\1", joined)
    joined = re.sub(r"([(\[]) +", r"\1", joined)
    return joined.strip()


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def clean_body_html(html: str, *, base_url: str = "") -> CleanedBody:
    """Clean one post body fragment.

    This is the entry point for content that is already narrowed down to the
    body — an RSS ``<content:encoded>`` payload, or the body region of a
    Substack email.

    Args:
        html:     Raw HTML fragment. May be empty.
        base_url: Post URL, used to resolve relative image and link targets.
                  Substack emits absolute URLs, but other feeds do not.

    Returns:
        A :class:`CleanedBody`. Empty input yields an empty result rather than
        raising, because a feed entry with no body is normal, not exceptional.
    """
    if not html or not html.strip():
        return CleanedBody("", "", None, None, 0)

    soup = BeautifulSoup(html, "lxml")
    # lxml wraps a bare fragment in <html><body>; operate on the body so the
    # serialized output stays a fragment.
    root = soup.body or soup

    _sanitize(root, base_url)

    hero_url: str | None = None
    hero_alt: str | None = None
    first_image = root.find("img")
    if isinstance(first_image, Tag):
        hero_url = str(first_image.get("src") or "") or None
        hero_alt = str(first_image.get("alt") or "").strip() or None

    text = _to_text(root)
    # Removing subtrees leaves behind the whitespace that surrounded them.
    # Collapsing blank-line runs keeps the emitted fragment readable without
    # touching whitespace that is significant between inline elements.
    cleaned_html = re.sub(r"\n[ \t]*(?:\n[ \t]*)+", "\n", root.decode_contents()).strip()

    return CleanedBody(
        html=cleaned_html,
        text=text,
        hero_image_url=hero_url,
        hero_image_alt=hero_alt,
        word_count=len(text.split()),
    )


def extract_post_body(page_html: str, *, base_url: str = "") -> CleanedBody:
    """Clean a full Substack post *page*, locating the body first.

    Used by the RSS ingester's fallback path, when a feed entry carried only a
    teaser and the post itself has to be fetched.

    Chrome is stripped from the whole document *before* a container is chosen —
    see :func:`strip_chrome` for why that ordering is what makes non-Substack
    sites work.

    Args:
        page_html: The complete HTML document from the post URL.
        base_url:  The post URL, for resolving relative references.

    Returns:
        A :class:`CleanedBody` built from the most specific body container
        found, falling back to the whole document.
    """
    if not page_html or not page_html.strip():
        return CleanedBody("", "", None, None, 0)

    soup = BeautifulSoup(page_html, "lxml")
    root = soup.body or soup
    strip_chrome(root)

    best: Tag | None = None
    for selector in _BODY_SELECTORS:
        container = root.select_one(selector)
        if container is None or len(container.get_text(strip=True)) <= 200:
            continue
        share = _link_share(container)
        if share > _MAX_LINK_TEXT_SHARE:
            # Mostly links: a nav block or a related-stories rail, not an article.
            logger.debug(
                "Rejecting %r as body container: %.0f%% of its text is links",
                selector,
                share * 100,
            )
            # Remember it anyway — if nothing better turns up it still beats
            # handing back the entire document.
            best = best or container
            continue
        return clean_body_html(container.decode_contents(), base_url=base_url)

    target = best if best is not None else root
    return clean_body_html(target.decode_contents(), base_url=base_url)


def slug_from_url(url: str) -> str:
    """Derive an article slug from a post URL.

    ``https://example.substack.com/p/the-headline`` becomes ``the-headline``.
    Query strings and fragments are discarded — Substack appends tracking
    parameters to shared links, and two URLs differing only by ``?utm_source``
    are the same post.

    Falls back to a hash-free placeholder when the path carries no usable
    segment, in which case the caller is responsible for disambiguating.
    """
    path = urlparse(url).path.rstrip("/")
    segment = path.rsplit("/", 1)[-1] if path else ""
    slug = re.sub(r"[^a-z0-9]+", "-", segment.lower()).strip("-")
    return slug[:200] or "post"
