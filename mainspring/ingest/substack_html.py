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

import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, NavigableString, Tag

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
    r"tracking-pixel|beacon"
    r")",
    re.IGNORECASE,
)

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

#: Selectors for the container holding a Substack post's body, most specific
#: first. ``.available-content`` is what a free (or unlocked) post uses;
#: ``.body.markup`` is the inner markup wrapper; the rest are fallbacks for
#: older markup and for non-Substack feeds.
_BODY_SELECTORS: tuple[str, ...] = (
    "div.available-content",
    "div.body.markup",
    "div.post-content",
    "div.markup",
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


def _sanitize(root: Tag, base_url: str) -> None:
    """Strip chrome and non-allowlisted markup from ``root``, in place."""
    # Pass 1 — delete whole subtrees: banned tags, then anything that looks like
    # furniture. Done before unwrapping so that a chrome <div> takes its
    # children with it rather than leaking a "Subscribe now" paragraph.
    for tag in root.find_all(list(DROP_TAGS)):
        tag.decompose()

    for tag in root.find_all(True):
        if tag.decomposed:
            continue
        if _is_chrome(tag):
            tag.decompose()

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
    for selector in _BODY_SELECTORS:
        container = soup.select_one(selector)
        if container and len(container.get_text(strip=True)) > 200:
            return clean_body_html(container.decode_contents(), base_url=base_url)

    body = soup.body
    return clean_body_html(body.decode_contents() if body else page_html, base_url=base_url)


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
