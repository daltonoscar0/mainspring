"""Tests for the shared Substack HTML cleaner.

The cleaner is the piece both ingest paths depend on, so these tests are the
most detailed in the suite. They assert on two things: that furniture is gone,
and that writing is not.
"""

from __future__ import annotations

import pytest

from mainspring.ingest.substack_html import (
    ALLOWED_TAGS,
    clean_body_html,
    extract_post_body,
    slug_from_url,
)

#: Text that only appears inside chrome in the fixture. None of it may survive.
CHROME_MARKERS = [
    "Subscribe to Fixture Weekly",
    "Subscribe now",
    "Thousands of paid subscribers",
    "Type your email",
    "Leave a comment",
    "Restack",
    "Which do you prefer?",
    "Another post you might like",
    "This post is for paid subscribers",
    "Upgrade to paid",
    "Thanks for reading Fixture Weekly",
    "Subscribe for free",
    "You are receiving this because you subscribed",
    "Read in app",
    "Unsubscribe",
    "Manage your subscription",
    "__SUBSTACK__",
]

#: Every KEEP marker in the fixture. All of it must survive.
KEEP_MARKERS = [
    "KEEP-INTRO",
    "KEEP-HEADING",
    "KEEP-ALT",
    "KEEP-CAPTION",
    "KEEP-QUOTE",
    "KEEP-BULLET-ONE",
    "KEEP-BULLET-TWO",
    "KEEP-BODY",
    "KEEP-CLOSING",
]


class TestChromeRemoval:
    """Substack furniture must not reach the page."""

    @pytest.mark.parametrize("marker", CHROME_MARKERS)
    def test_chrome_is_stripped(self, substack_chrome_body: str, marker: str) -> None:
        result = clean_body_html(substack_chrome_body)
        assert marker not in result.html
        assert marker not in result.text

    def test_scripts_and_forms_are_gone(self, substack_chrome_body: str) -> None:
        result = clean_body_html(substack_chrome_body)
        for tag in ("<script", "<form", "<button", "<input", "<svg"):
            assert tag not in result.html

    def test_tracking_pixel_is_dropped(self, substack_chrome_body: str) -> None:
        result = clean_body_html(substack_chrome_body)
        assert "email-open" not in result.html
        # The one surviving image is the content chart, not the beacon.
        assert result.html.count("<img") == 1

    def test_only_allowlisted_tags_survive(self, substack_chrome_body: str) -> None:
        from bs4 import BeautifulSoup

        result = clean_body_html(substack_chrome_body)
        soup = BeautifulSoup(result.html, "lxml")
        # lxml adds html/body wrappers around a fragment; ignore those.
        found = {tag.name for tag in soup.find_all(True)} - {"html", "body"}
        assert found <= ALLOWED_TAGS, f"unexpected tags: {found - ALLOWED_TAGS}"

    def test_no_class_or_style_attributes_survive(self, substack_chrome_body: str) -> None:
        result = clean_body_html(substack_chrome_body)
        for attr in ('class="', 'style="', "onclick", "data-component-name"):
            assert attr not in result.html


class TestContentPreservation:
    """Writing, images and links must come through intact."""

    @pytest.mark.parametrize("marker", KEEP_MARKERS)
    def test_content_survives(self, substack_chrome_body: str, marker: str) -> None:
        result = clean_body_html(substack_chrome_body)
        assert marker in result.html

    def test_image_keeps_src_and_alt(self, substack_chrome_body: str) -> None:
        result = clean_body_html(substack_chrome_body)
        assert "chart.png" in result.html
        assert 'alt="KEEP-ALT A chart of quarterly output"' in result.html

    def test_hero_image_is_the_first_content_image(self, substack_chrome_body: str) -> None:
        result = clean_body_html(substack_chrome_body)
        assert result.hero_image_url is not None
        assert "chart.png" in result.hero_image_url
        assert result.hero_image_alt == "KEEP-ALT A chart of quarterly output"

    def test_structure_is_preserved(self, substack_chrome_body: str) -> None:
        result = clean_body_html(substack_chrome_body)
        for tag in ("<p>", "<h2>", "<figure>", "<figcaption>", "<blockquote>", "<ul>", "<li>"):
            assert tag in result.html

    def test_citation_links_survive_but_cta_links_do_not(
        self, substack_chrome_body: str
    ) -> None:
        result = clean_body_html(substack_chrome_body)
        # Real citations, including one in a sentence that mentions reading more.
        assert "example.org/cited" in result.html
        assert "example.org/statistics" in result.html
        assert "example.org/further-reading" in result.html
        # Calls to action pointing at Substack endpoints.
        assert "fixture.substack.com/subscribe" not in result.html
        assert "/comments" not in result.html

    def test_captions_render_but_do_not_reach_the_summarizer(
        self, substack_chrome_body: str
    ) -> None:
        """A leading image credit would otherwise become the standfirst."""
        result = clean_body_html(substack_chrome_body)
        assert "KEEP-CAPTION" in result.html
        assert "KEEP-CAPTION" not in result.text
        assert result.text.startswith("KEEP-INTRO")


class TestPlainText:
    """The plain-text rendering feeds the summarizer, so its shape matters."""

    def test_text_has_no_markup(self, substack_chrome_body: str) -> None:
        result = clean_body_html(substack_chrome_body)
        assert "<" not in result.text
        assert ">" not in result.text

    def test_blocks_are_separated_by_newlines(self, substack_chrome_body: str) -> None:
        result = clean_body_html(substack_chrome_body)
        lines = [line for line in result.text.split("\n") if line]
        assert len(lines) >= 6
        # No block runs into the next.
        assert not any("KEEP-HEADING" in line and "KEEP-QUOTE" in line for line in lines)

    def test_source_line_wrapping_does_not_split_a_paragraph(
        self, substack_chrome_body: str
    ) -> None:
        """The fixture wraps its paragraphs; the text must not inherit the breaks."""
        result = clean_body_html(substack_chrome_body)
        intro = next(line for line in result.text.split("\n") if "KEEP-INTRO" in line)
        assert "survive cleaning intact" in intro

    def test_word_count_matches_the_text(self, substack_chrome_body: str) -> None:
        result = clean_body_html(substack_chrome_body)
        assert result.word_count == len(result.text.split())
        assert result.word_count > 40

    def test_punctuation_is_not_orphaned_by_inline_links(
        self, substack_chrome_body: str
    ) -> None:
        result = clean_body_html(substack_chrome_body)
        assert " ." not in result.text
        assert " ," not in result.text


class TestRealMarkup:
    """The same cleaner, run against unmodified Substack output."""

    def test_feed_body_cleans_to_readable_prose(
        self, construction_physics_feed: bytes
    ) -> None:
        import feedparser

        parsed = feedparser.parse(construction_physics_feed)
        entry = parsed.entries[0]
        result = clean_body_html(entry.content[0]["value"], base_url=entry.link)

        assert result.word_count > 200
        assert not result.is_empty
        for tag in ("<script", "<button", "<svg", 'class="'):
            assert tag not in result.html

    def test_post_page_body_is_located_and_cleaned(self, substack_post_page: str) -> None:
        result = extract_post_body(
            substack_post_page,
            base_url="https://www.construction-physics.com/p/why-is-everyone-trying-to-build-a",
        )
        assert result.word_count > 1000
        assert "solid-state" in result.text.lower()
        assert "<p>" in result.html

    def test_relative_urls_resolve_against_the_post(self) -> None:
        result = clean_body_html(
            '<p>Read <a href="/p/other">the other post</a>.</p>'
            '<p><img src="/images/photo.png" alt="A photo"></p>',
            base_url="https://example.substack.com/p/this-post",
        )
        assert "https://example.substack.com/p/other" in result.html
        assert "https://example.substack.com/images/photo.png" in result.html


class TestEdgeCases:
    """Degenerate input must return an empty result, not raise."""

    @pytest.mark.parametrize("payload", ["", "   ", "\n\n"])
    def test_blank_input(self, payload: str) -> None:
        result = clean_body_html(payload)
        assert result.is_empty
        assert result.html == ""
        assert result.hero_image_url is None

    def test_chrome_only_input_cleans_to_nothing(self) -> None:
        result = clean_body_html(
            '<div class="subscription-widget-wrap"><p>Subscribe now</p></div>'
        )
        assert result.is_empty

    def test_javascript_hrefs_are_unwrapped_not_kept(self) -> None:
        result = clean_body_html('<p><a href="javascript:alert(1)">Click</a> me.</p>')
        assert "javascript:" not in result.html
        assert "Click" in result.html  # the text is content; only the link goes

    def test_empty_elements_are_collapsed(self) -> None:
        result = clean_body_html("<p></p><p>  </p><blockquote></blockquote><p>Real text.</p>")
        assert result.html.count("<p>") == 1
        assert "<blockquote>" not in result.html


class TestSlugFromUrl:
    """Slugs become filenames, so they must be stable and URL-safe."""

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://example.substack.com/p/the-headline", "the-headline"),
            ("https://example.substack.com/p/the-headline/", "the-headline"),
            ("https://example.substack.com/p/the-headline?utm_source=x", "the-headline"),
            ("https://example.substack.com/p/the-headline#comments", "the-headline"),
            ("https://example.substack.com/p/Mixed_Case_Post", "mixed-case-post"),
            ("https://example.substack.com/", "post"),
            ("https://example.substack.com", "post"),
        ],
    )
    def test_slug_derivation(self, url: str, expected: str) -> None:
        assert slug_from_url(url) == expected

    def test_tracking_parameters_do_not_change_the_slug(self) -> None:
        """Two shared links for one post must resolve to the same page."""
        bare = "https://example.substack.com/p/a-post"
        shared = "https://example.substack.com/p/a-post?utm_source=substack&utm_medium=email"
        assert slug_from_url(bare) == slug_from_url(shared)

    def test_slug_is_filesystem_safe(self) -> None:
        slug = slug_from_url("https://example.substack.com/p/../../etc/passwd")
        assert "/" not in slug
        assert ".." not in slug
