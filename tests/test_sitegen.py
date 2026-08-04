"""Tests for ranking and site generation.

Ranking is tested as a pure function, against plain objects and with no database
in sight — which is the property that makes it swappable. Site generation is
tested by building a real site into ``tmp_path`` from a fixture dataset and then
reading the files back.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from mainspring.models import Article, DailyEdition, Publication
from mainspring.sitegen import build as sitegen
from mainspring.sitegen import rank
from tests.conftest import FakeArticle


# ===========================================================================
# Ranking
# ===========================================================================

class TestRankingStructure:
    """Every article lands in exactly one slot."""

    def test_slots_are_filled(self, fake_articles) -> None:
        ranking = rank.rank_articles(fake_articles)
        assert ranking.lead is not None
        assert len(ranking.secondaries) == 3
        assert len(ranking.river) == len(fake_articles) - 4

    def test_no_article_appears_twice(self, fake_articles) -> None:
        ranking = rank.rank_articles(fake_articles)
        urls = [a.url for a in ranking.all_articles]
        assert len(urls) == len(set(urls))
        assert set(urls) == {a.url for a in fake_articles}

    def test_empty_input_yields_an_empty_ranking(self) -> None:
        ranking = rank.rank_articles([])
        assert ranking.is_empty
        assert ranking.lead is None
        assert ranking.all_articles == []

    def test_a_single_article_becomes_the_lead(self) -> None:
        only = FakeArticle("https://a.example/p/one", "alpha", 500)
        ranking = rank.rank_articles([only])
        assert ranking.lead is only
        assert ranking.secondaries == []
        assert ranking.river == []

    def test_input_is_not_mutated(self, fake_articles) -> None:
        before = list(fake_articles)
        rank.rank_articles(fake_articles)
        assert fake_articles == before

    def test_limit_trims_the_river_only(self, fake_articles) -> None:
        ranking = rank.rank_articles(fake_articles, limit=6)
        assert len(ranking.all_articles) == 6
        assert ranking.lead is not None
        assert len(ranking.secondaries) == 3
        assert len(ranking.river) == 2

    def test_limit_below_the_headline_count_empties_the_river(self, fake_articles) -> None:
        ranking = rank.rank_articles(fake_articles, limit=2)
        assert ranking.river == []
        assert ranking.lead is not None

    def test_secondary_count_is_configurable(self, fake_articles) -> None:
        assert len(rank.rank_articles(fake_articles, secondary_count=0).secondaries) == 0
        assert len(rank.rank_articles(fake_articles, secondary_count=5).secondaries) == 5


class TestDeterminism:
    """The same inputs must always produce the same page."""

    def test_repeated_calls_agree(self, fake_articles) -> None:
        first = rank.rank_articles(fake_articles)
        second = rank.rank_articles(fake_articles)
        assert first.lead.url == second.lead.url
        assert [a.url for a in first.secondaries] == [a.url for a in second.secondaries]
        assert [a.url for a in first.river] == [a.url for a in second.river]

    def test_input_order_does_not_change_the_result(self, fake_articles) -> None:
        forward = rank.rank_articles(fake_articles)
        backward = rank.rank_articles(list(reversed(fake_articles)))
        assert forward.lead.url == backward.lead.url
        assert [a.url for a in forward.secondaries] == [a.url for a in backward.secondaries]
        assert [a.url for a in forward.river] == [a.url for a in backward.river]

    def test_ties_are_broken_by_url_not_position(self) -> None:
        """Identical length and date must still resolve the same way every time."""
        articles = [
            FakeArticle("https://z.example/p/last", "alpha", 1000, "2026-08-01"),
            FakeArticle("https://a.example/p/first", "alpha", 1000, "2026-08-01"),
        ]
        assert rank.rank_articles(articles).lead.url == "https://a.example/p/first"
        assert rank.rank_articles(list(reversed(articles))).lead.url == "https://a.example/p/first"


class TestLeadSelection:
    """Lead choice: longest, from the publication that has waited longest."""

    def test_without_history_the_longest_leads(self, fake_articles) -> None:
        ranking = rank.rank_articles(fake_articles, lead_history={})
        assert ranking.lead.word_count == 5000

    def test_a_publication_that_has_never_led_goes_first(self) -> None:
        """A new subscription reaches the front page immediately."""
        articles = [
            FakeArticle("https://a.example/p/long", "alpha", 9000),
            FakeArticle("https://b.example/p/short", "beta", 100),
        ]
        ranking = rank.rank_articles(articles, lead_history={"alpha": "2026-08-03"})
        assert rank.publication_slug(ranking.lead) == "beta"

    def test_rotation_prefers_the_longest_wait(self) -> None:
        articles = [
            FakeArticle("https://a.example/p/x", "alpha", 9000),
            FakeArticle("https://b.example/p/x", "beta", 5000),
            FakeArticle("https://c.example/p/x", "gamma", 1000),
        ]
        history = {"alpha": "2026-08-03", "beta": "2026-08-01", "gamma": "2026-08-02"}
        ranking = rank.rank_articles(articles, lead_history=history)
        assert rank.publication_slug(ranking.lead) == "beta"

    def test_within_the_chosen_publication_the_longest_wins(self) -> None:
        articles = [
            FakeArticle("https://a.example/p/short", "alpha", 400, "2026-08-04"),
            FakeArticle("https://a.example/p/long", "alpha", 4000, "2026-08-01"),
        ]
        ranking = rank.rank_articles(articles, lead_history={})
        assert ranking.lead.url == "https://a.example/p/long"

    def test_rotation_spreads_leads_over_a_week(self, fake_articles) -> None:
        """The point of rotation: six days should not be six of the same voice."""
        history: dict[str, str] = {}
        leads = []
        for day in range(1, 7):
            date = f"2026-08-0{day}"
            ranking = rank.rank_articles(fake_articles, lead_history=history)
            slug = rank.publication_slug(ranking.lead)
            leads.append(slug)
            history[slug] = date

        # Three publications, six days: each leads exactly twice.
        assert sorted(set(leads)) == ["alpha", "beta", "gamma"]
        assert all(leads.count(slug) == 2 for slug in set(leads))
        # And never twice running.
        assert all(leads[i] != leads[i + 1] for i in range(len(leads) - 1))

    def test_choose_lead_on_empty_input(self) -> None:
        assert rank.choose_lead([], {}) is None


class TestSecondaries:
    """Secondaries: longest, spread across publications."""

    def test_ordered_by_length(self, fake_articles) -> None:
        ranking = rank.rank_articles(fake_articles, lead_history={})
        counts = [a.word_count for a in ranking.secondaries]
        assert counts == sorted(counts, reverse=True)

    def test_publications_are_not_repeated_when_there_are_enough(self) -> None:
        articles = [
            FakeArticle(f"https://{slug}.example/p/x", slug, 5000 - index * 100)
            for index, slug in enumerate(["alpha", "beta", "gamma", "delta", "epsilon"])
        ]
        ranking = rank.rank_articles(articles, lead_history={})
        slugs = [rank.publication_slug(a) for a in ranking.secondaries]
        assert rank.publication_slug(ranking.lead) not in slugs
        assert len(slugs) == len(set(slugs))

    def test_distinct_publications_come_first_when_slots_outnumber_them(
        self, fake_articles
    ) -> None:
        """Three publications cannot fill four slots, so one repeats — but only once.

        The repeat has to be the last slot filled, after every other publication
        has been offered one.
        """
        ranking = rank.rank_articles(fake_articles, lead_history={})
        slugs = [rank.publication_slug(a) for a in ranking.secondaries]
        assert set(slugs) == {"alpha", "beta", "gamma"}
        # Exactly one publication appears twice across the four headline slots.
        headline_slugs = [rank.publication_slug(ranking.lead), *slugs]
        assert len(headline_slugs) - len(set(headline_slugs)) == 1

    def test_one_publication_does_not_take_the_whole_page(self) -> None:
        """Three long essays from one writer must not own every headline slot."""
        articles = [
            FakeArticle(f"https://a.example/p/{i}", "alpha", 9000 - i) for i in range(5)
        ] + [
            FakeArticle("https://b.example/p/x", "beta", 200),
            FakeArticle("https://c.example/p/x", "gamma", 100),
        ]
        ranking = rank.rank_articles(articles, lead_history={})
        top = [ranking.lead, *ranking.secondaries]
        slugs = {rank.publication_slug(a) for a in top}
        assert slugs == {"alpha", "beta", "gamma"}

    def test_slots_are_filled_even_with_one_publication(self) -> None:
        """Better to repeat a publication than leave a hole in the layout."""
        articles = [
            FakeArticle(f"https://a.example/p/{i}", "alpha", 1000 - i * 10) for i in range(6)
        ]
        ranking = rank.rank_articles(articles, lead_history={})
        assert len(ranking.secondaries) == 3
        assert all(rank.publication_slug(a) == "alpha" for a in ranking.secondaries)


class TestFreshness:
    """The lead and secondaries come from the day's own stories where possible."""

    def test_a_fresh_short_story_beats_a_held_long_one_for_the_lead(self) -> None:
        held = FakeArticle("https://a.example/p/old", "alpha", 9000, "2026-07-28")
        fresh = FakeArticle("https://b.example/p/new", "beta", 300, "2026-08-04")
        ranking = rank.rank_articles([held, fresh], fresh=[fresh], lead_history={})
        assert ranking.lead is fresh

    def test_held_stories_fill_the_secondaries(self) -> None:
        fresh = FakeArticle("https://a.example/p/new", "alpha", 500, "2026-08-04")
        held = [
            FakeArticle(f"https://b.example/p/{i}", f"pub{i}", 4000 - i, "2026-08-01")
            for i in range(3)
        ]
        ranking = rank.rank_articles([fresh, *held], fresh=[fresh], lead_history={})
        assert ranking.lead is fresh
        assert len(ranking.secondaries) == 3
        assert {a.url for a in ranking.secondaries} == {a.url for a in held}

    def test_all_fresh_stories_take_slots_before_any_held_one(self) -> None:
        fresh = [
            FakeArticle(f"https://f{i}.example/p/x", f"fresh{i}", 200, "2026-08-04")
            for i in range(3)
        ]
        held = FakeArticle("https://h.example/p/x", "held", 9000, "2026-08-01")
        ranking = rank.rank_articles([held, *fresh], fresh=fresh, lead_history={})
        top = {a.url for a in [ranking.lead, *ranking.secondaries]}
        assert {a.url for a in fresh} <= top
        assert held.url in top  # the fourth slot

    def test_fresh_none_treats_everything_as_fresh(self, fake_articles) -> None:
        assert (
            rank.rank_articles(fake_articles, fresh=None).lead.url
            == rank.rank_articles(fake_articles, fresh=fake_articles).lead.url
        )

    def test_an_empty_fresh_set_still_produces_a_page(self, fake_articles) -> None:
        """A day with no new posts still gets a front page from the window."""
        ranking = rank.rank_articles(fake_articles, fresh=[])
        assert ranking.lead is not None
        assert len(ranking.secondaries) == 3


class TestRiver:
    """The river runs newest first and groups by publication."""

    def test_ordered_by_recency(self, fake_articles) -> None:
        ranking = rank.rank_articles(fake_articles, lead_history={})
        dates = [a.published_date or datetime.min for a in ranking.river]
        assert dates == sorted(dates, reverse=True)

    def test_undated_articles_sink_to_the_bottom(self) -> None:
        """A feed that omits dates must not push its posts to the top of the river."""
        articles = [
            FakeArticle("https://a.example/p/lead", "alpha", 5000, "2026-08-04"),
            FakeArticle("https://a.example/p/recent", "alpha", 400, "2026-08-01"),
            FakeArticle("https://a.example/p/older", "alpha", 300, "2026-07-01"),
            FakeArticle("https://a.example/p/undated", "alpha", 200, None),
        ]
        ranking = rank.rank_articles(articles, secondary_count=0, lead_history={})
        assert [a.url for a in ranking.river] == [
            "https://a.example/p/recent",
            "https://a.example/p/older",
            "https://a.example/p/undated",
        ]

    def test_grouping_keeps_publications_together(self, fake_articles) -> None:
        groups = rank.group_by_publication(fake_articles)
        slugs = [slug for slug, _ in groups]
        assert len(slugs) == len(set(slugs)) == 3
        assert sum(len(items) for _, items in groups) == len(fake_articles)

    def test_the_freshest_publication_heads_the_river(self, fake_articles) -> None:
        groups = rank.group_by_publication(fake_articles)
        newest = [items[0].published_date or datetime.min for _, items in groups]
        assert newest == sorted(newest, reverse=True)

    def test_grouping_is_deterministic(self, fake_articles) -> None:
        first = rank.group_by_publication(fake_articles)
        second = rank.group_by_publication(list(reversed(fake_articles)))
        assert [(s, [a.url for a in items]) for s, items in first] == [
            (s, [a.url for a in items]) for s, items in second
        ]

    def test_grouping_empty_input(self) -> None:
        assert rank.group_by_publication([]) == []


class TestRiverCap:
    """A high-volume publication must not take the whole river."""

    @pytest.fixture
    def lopsided(self) -> list[FakeArticle]:
        """One wire service posting constantly, three newsletters posting once."""
        wire = [
            FakeArticle(f"https://wire.example/p/{i}", "wire", 500, "2026-08-04")
            for i in range(30)
        ]
        others = [
            FakeArticle(f"https://{slug}.example/p/x", slug, 400, "2026-08-03")
            for slug in ("alpha", "beta", "gamma")
        ]
        return wire + others

    def test_uncapped_the_wire_dominates(self, lopsided) -> None:
        """Establishes the problem the cap exists to solve."""
        river = rank.rank_articles(lopsided, lead_history={}).river
        slugs = [rank.publication_slug(a) for a in river]
        assert slugs.count("wire") > 20

    def test_the_cap_limits_each_publication(self, lopsided) -> None:
        river = rank.rank_articles(
            lopsided, lead_history={}, per_publication=4
        ).river
        counts: dict[str, int] = {}
        for article in river:
            slug = rank.publication_slug(article)
            counts[slug] = counts.get(slug, 0) + 1
        assert all(count <= 4 for count in counts.values()), counts

    def test_the_cap_keeps_the_most_recent_items(self) -> None:
        articles = [
            FakeArticle("https://w.example/p/new", "wire", 100, "2026-08-04"),
            FakeArticle("https://w.example/p/mid", "wire", 100, "2026-08-03"),
            FakeArticle("https://w.example/p/old", "wire", 100, "2026-08-01"),
            FakeArticle("https://a.example/p/x", "alpha", 900, "2026-08-04"),
        ]
        river = rank.rank_articles(
            articles, lead_history={}, secondary_count=0, per_publication=2
        ).river
        urls = [a.url for a in river]
        assert "https://w.example/p/old" not in urls
        assert "https://w.example/p/new" in urls

    def test_the_cap_preserves_recency_order(self, lopsided) -> None:
        river = rank.rank_articles(
            lopsided, lead_history={}, per_publication=3
        ).river
        dates = [a.published_date or datetime.min for a in river]
        assert dates == sorted(dates, reverse=True)

    def test_smaller_publications_are_not_crowded_out(self, lopsided) -> None:
        """The point of the cap: every publication still reaches the page.

        Asserted over the whole page rather than the river alone — a small
        publication landing in a headline slot is the outcome working, not
        failing.
        """
        ranking = rank.rank_articles(
            lopsided, lead_history={}, per_publication=4, limit=12
        )
        placed = {rank.publication_slug(a) for a in ranking.all_articles}
        assert placed == {"wire", "alpha", "beta", "gamma"}

        # And the wire holds no more than its share: 4 river slots plus at most
        # one headline slot.
        wire_count = sum(
            1 for a in ranking.all_articles if rank.publication_slug(a) == "wire"
        )
        assert wire_count <= 5, wire_count

    def test_none_and_zero_disable_the_cap(self, lopsided) -> None:
        uncapped = rank.rank_articles(lopsided, lead_history={}).river
        assert len(rank.rank_articles(lopsided, lead_history={}, per_publication=None).river) == len(uncapped)
        assert len(rank.rank_articles(lopsided, lead_history={}, per_publication=0).river) == len(uncapped)

    def test_the_cap_applies_in_a_real_build(self, dataset, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(sitegen.config, "RIVER_PER_PUBLICATION", 1)
        site_dir = tmp_path / "capped"
        sitegen.build_site(dataset, site_dir=site_dir)
        html = read(site_dir, "index.html")
        # One river slot per publication means at most one item under each
        # standing head.
        assert html.count('class="river__item"') <= html.count('class="standing-head"')


class TestSplitRivers:
    """Two rivers with reserved slots, so the wire cannot bury the newsletters."""

    SPECS = (
        rank.SectionSpec(key="newsletter", label="Your correspondents", limit=4),
        rank.SectionSpec(key="wire", label="The wire", limit=6),
    )

    @pytest.fixture
    def mixed(self) -> list[FakeArticle]:
        """A wire posting today, newsletters that filed days ago.

        The realistic shape: pooled and sorted by recency, every slot goes to the
        wire and no newsletter appears at all.
        """
        wire = [
            FakeArticle(f"https://wire{i % 3}.example/p/{i}", f"wire{i % 3}", 600, "2026-08-04")
            for i in range(24)
        ]
        # Three newsletters with three posts each, so some remain for the river
        # after the lead and secondary slots are filled.
        letters = [
            FakeArticle(f"https://{slug}.example/p/{n}", slug, 900 - n, "2026-08-01")
            for slug in ("alpha", "beta", "gamma")
            for n in range(3)
        ]
        return wire + letters

    @staticmethod
    def section_of(article) -> str:
        return "wire" if rank.publication_slug(article).startswith("wire") else "newsletter"

    def test_pooled_the_newsletters_disappear(self, mixed) -> None:
        """Establishes the problem the split exists to solve."""
        river = rank.rank_articles(
            mixed, lead_history={}, per_publication=4, limit=12
        ).river
        slugs = {rank.publication_slug(a) for a in river}
        assert not slugs & {"alpha", "beta", "gamma"}

    def test_split_gives_the_newsletters_their_own_slots(self, mixed) -> None:
        ranking = rank.rank_articles(
            mixed,
            lead_history={},
            per_publication=4,
            section_of=self.section_of,
            sections=self.SPECS,
        )
        assert [s.key for s in ranking.sections] == ["newsletter", "wire"]
        letters = ranking.sections[0]
        assert letters.articles, "the newsletter river must not be empty"
        assert all(self.section_of(a) == "newsletter" for a in letters.articles)

    def test_each_river_holds_only_its_own_kind(self, mixed) -> None:
        ranking = rank.rank_articles(
            mixed, lead_history={}, section_of=self.section_of, sections=self.SPECS
        )
        for section in ranking.sections:
            assert all(self.section_of(a) == section.key for a in section.articles)

    def test_section_limits_are_respected(self, mixed) -> None:
        ranking = rank.rank_articles(
            mixed, lead_history={}, section_of=self.section_of, sections=self.SPECS
        )
        by_key = {s.key: s for s in ranking.sections}
        assert len(by_key["newsletter"].articles) <= 4
        assert len(by_key["wire"].articles) <= 6

    def test_labels_are_carried_through(self, mixed) -> None:
        ranking = rank.rank_articles(
            mixed, lead_history={}, section_of=self.section_of, sections=self.SPECS
        )
        assert [s.label for s in ranking.sections] == ["Your correspondents", "The wire"]

    def test_river_is_the_concatenation_of_the_sections(self, mixed) -> None:
        """``river`` stays meaningful so counts and archives need no special case."""
        ranking = rank.rank_articles(
            mixed, lead_history={}, section_of=self.section_of, sections=self.SPECS
        )
        expected = [a for section in ranking.sections for a in section.articles]
        assert ranking.river == expected

    def test_no_article_is_placed_twice(self, mixed) -> None:
        ranking = rank.rank_articles(
            mixed, lead_history={}, section_of=self.section_of, sections=self.SPECS
        )
        urls = [a.url for a in ranking.all_articles]
        assert len(urls) == len(set(urls))

    def test_per_publication_cap_still_applies_inside_a_river(self, mixed) -> None:
        ranking = rank.rank_articles(
            mixed,
            lead_history={},
            per_publication=2,
            section_of=self.section_of,
            sections=self.SPECS,
        )
        wire = next(s for s in ranking.sections if s.key == "wire")
        counts: dict[str, int] = {}
        for article in wire.articles:
            slug = rank.publication_slug(article)
            counts[slug] = counts.get(slug, 0) + 1
        assert all(count <= 2 for count in counts.values()), counts

    def test_a_section_may_override_the_global_cap(self, mixed) -> None:
        specs = (
            rank.SectionSpec(key="newsletter", label="L", limit=10),
            rank.SectionSpec(key="wire", label="W", limit=20, per_publication=1),
        )
        ranking = rank.rank_articles(
            mixed, lead_history={}, per_publication=5,
            section_of=self.section_of, sections=specs,
        )
        wire = next(s for s in ranking.sections if s.key == "wire")
        assert len(wire.articles) == len({rank.publication_slug(a) for a in wire.articles})

    def test_sections_without_section_of_are_ignored(self, mixed) -> None:
        """Both arguments are required; one alone falls back to a single river."""
        ranking = rank.rank_articles(mixed, lead_history={}, sections=self.SPECS)
        assert ranking.sections == []
        assert ranking.river

    def test_an_unmatched_section_key_drops_those_articles(self, mixed) -> None:
        only_wire = (rank.SectionSpec(key="wire", label="W", limit=99),)
        ranking = rank.rank_articles(
            mixed, lead_history={}, section_of=self.section_of, sections=only_wire
        )
        assert all(self.section_of(a) == "wire" for a in ranking.river)

    def test_determinism(self, mixed) -> None:
        first = rank.rank_articles(
            mixed, lead_history={}, section_of=self.section_of, sections=self.SPECS
        )
        second = rank.rank_articles(
            list(reversed(mixed)), lead_history={},
            section_of=self.section_of, sections=self.SPECS,
        )
        assert [a.url for a in first.river] == [a.url for a in second.river]


class TestSplitRiversOnThePage:
    """The rendered front page."""

    @pytest.fixture
    def two_kinds(self, dataset):
        """Make one fixture publication a wire outlet."""
        publication = dataset.query(Publication).filter_by(slug="gamma-notes").one()
        publication.kind = "wire"
        dataset.commit()
        return dataset

    def test_both_headings_render(self, two_kinds, tmp_path) -> None:
        sitegen.build_site(two_kinds, site_dir=tmp_path / "two")
        html = read(tmp_path / "two", "index.html")
        assert "Your correspondents" in html
        assert "The wire" in html

    def test_each_river_gets_its_own_container(self, two_kinds, tmp_path) -> None:
        sitegen.build_site(two_kinds, site_dir=tmp_path / "two")
        html = read(tmp_path / "two", "index.html")
        assert 'class="river river--newsletter"' in html
        assert 'class="river river--wire"' in html

    def test_a_wire_publication_appears_only_in_the_wire_river(
        self, two_kinds, tmp_path
    ) -> None:
        import re

        sitegen.build_site(two_kinds, site_dir=tmp_path / "two")
        html = read(tmp_path / "two", "index.html")
        wire_block = html.split('class="river river--wire"')[1]
        letters_block = html.split('class="river river--newsletter"')[1].split(
            'class="river river--wire"'
        )[0]
        assert "Gamma Notes" in wire_block
        assert "Gamma Notes" not in letters_block

    def test_an_empty_river_is_omitted(self, dataset, tmp_path) -> None:
        """All-newsletter subscriptions must not render a bare "The wire" rule."""
        sitegen.build_site(dataset, site_dir=tmp_path / "one")
        html = read(tmp_path / "one", "index.html")
        assert 'class="river river--newsletter"' in html
        assert "river--wire" not in html


class TestPublicationSlug:
    """Ranking must read a slug off an ORM object or a plain one."""

    def test_from_a_plain_attribute(self) -> None:
        assert rank.publication_slug(FakeArticle("u", "alpha", 1)) == "alpha"

    def test_from_a_related_object(self) -> None:
        class Pub:
            slug = "beta"

        class Art:
            publication = Pub()
            url = "u"

        assert rank.publication_slug(Art()) == "beta"

    def test_missing_slug_falls_back(self) -> None:
        class Art:
            url = "u"

        assert rank.publication_slug(Art()) == "unknown"


# ===========================================================================
# Site generation
# ===========================================================================

@pytest.fixture
def dataset(db):
    """Three publications with nine articles across four days.

    Enough to exercise a full front page — lead, three secondaries, a
    multi-publication river — plus an archive with several editions and one
    undated article.
    """
    publications = []
    for name, slug in [
        ("Alpha Review", "alpha-review"),
        ("Beta Letter", "beta-letter"),
        ("Gamma Notes", "gamma-notes"),
    ]:
        row = Publication(
            name=name,
            slug=slug,
            feed_url=f"https://{slug}.substack.com/feed",
            tier="free",
            homepage_url=f"https://{slug}.substack.com",
        )
        db.add(row)
        publications.append(row)
    db.commit()

    spec = [
        ("alpha-review", "the-long-essay", 5200, "2026-08-04", True),
        ("alpha-review", "a-second-piece", 1400, "2026-08-04", False),
        ("beta-letter", "the-beta-report", 4100, "2026-08-04", True),
        ("gamma-notes", "notes-on-a-theme", 3300, "2026-08-04", False),
        ("alpha-review", "yesterdays-essay", 2600, "2026-08-03", False),
        ("beta-letter", "an-earlier-letter", 2200, "2026-08-03", False),
        ("gamma-notes", "older-notes", 1800, "2026-08-02", False),
        ("beta-letter", "oldest-letter", 900, "2026-08-01", False),
        ("gamma-notes", "an-undated-post", 1100, None, False),
    ]

    by_slug = {p.slug: p for p in publications}
    for publication_slug, slug, words, published, has_image in spec:
        publication = by_slug[publication_slug]
        db.add(
            Article(
                publication_id=publication.id,
                title=slug.replace("-", " ").title(),
                url=f"https://{publication_slug}.substack.com/p/{slug}",
                slug=slug,
                author=f"{publication.name} Writer",
                published_date=datetime.strptime(published, "%Y-%m-%d") if published else None,
                created_at=datetime(2026, 8, 4, 6, 0),
                body_html=(
                    f"<p>The opening paragraph of {slug}.</p>"
                    "<h2>A heading</h2>"
                    "<p>A second paragraph with <a href='https://example.org'>a link</a>.</p>"
                    "<blockquote><p>A quotation.</p></blockquote>"
                ),
                body_text=f"The opening paragraph of {slug}. A second paragraph.",
                word_count=words,
                hero_image_url=(
                    f"https://cdn.example.com/{slug}.png" if has_image else None
                ),
                hero_image_alt=f"Art for {slug}" if has_image else None,
                standfirst=f"A standfirst describing {slug.replace('-', ' ')} in brief.",
                source_kind="rss",
            )
        )
    db.commit()
    return db


@pytest.fixture
def built(dataset, tmp_path):
    """Build the site from the fixture dataset and return (report, site_dir)."""
    site_dir = tmp_path / "site"
    report = sitegen.build_site(dataset, site_dir=site_dir)
    return report, site_dir


def read(site_dir, relative: str) -> str:
    """Read one generated page."""
    return (site_dir / relative).read_text(encoding="utf-8")


class TestBuildOutput:
    """The expected files exist, and the report describes them."""

    def test_report_is_clean(self, built) -> None:
        report, _ = built
        assert report.ok
        assert report.errors == []

    def test_counts(self, built) -> None:
        report, _ = built
        assert report.articles == 9
        assert report.publications == 3
        # One edition per date that has an article: Aug 1–4 plus the undated
        # post, which is filed under its ingest date (also Aug 4).
        assert report.editions == 4

    def test_the_expected_pages_exist(self, built) -> None:
        _, site_dir = built
        for path in [
            "index.html",
            "archive.html",
            ".nojekyll",
            "static/mainspring.css",
            "publications/index.html",
            "publications/alpha-review.html",
            "publications/beta-letter.html",
            "publications/gamma-notes.html",
            "articles/alpha-review/the-long-essay.html",
            "editions/2026-08-04/index.html",
            "editions/2026-08-01/index.html",
        ]:
            assert (site_dir / path).is_file(), f"missing {path}"

    def test_every_article_gets_a_page(self, built, dataset) -> None:
        _, site_dir = built
        for article in dataset.query(Article).all():
            page = site_dir / "articles" / article.publication.slug / f"{article.slug}.html"
            assert page.is_file(), f"missing page for {article.title}"

    def test_pages_are_html(self, built) -> None:
        _, site_dir = built
        for page in site_dir.rglob("*.html"):
            html = page.read_text(encoding="utf-8")
            assert html.startswith("<!doctype html>")
            assert html.rstrip().endswith("</html>")
            assert "<title>" in html


class TestFrontPage:
    """The front page must be a front page, not a list."""

    def test_masthead_and_dateline(self, built) -> None:
        _, site_dir = built
        html = read(site_dir, "index.html")
        assert "The Mainspring" in html
        # "Tuesday, August 4, 2026" — the long form, spelled out.
        assert "Tuesday, August 4, 2026" in html

    def test_headline_hierarchy_is_present(self, built) -> None:
        _, site_dir = built
        html = read(site_dir, "index.html")
        assert html.count('class="lead__headline"') == 1
        assert html.count('class="story__headline"') == 3
        assert html.count('class="river__item"') >= 1

    def test_the_river_is_grouped_by_publication(self, built) -> None:
        _, site_dir = built
        html = read(site_dir, "index.html")
        assert 'class="standing-head"' in html

    def test_the_lead_carries_art_and_a_standfirst(self, built) -> None:
        _, site_dir = built
        html = read(site_dir, "index.html")
        assert 'class="lead__standfirst"' in html
        assert 'class="lead__figure"' in html
        assert "cdn.example.com" in html

    def test_the_root_page_matches_the_newest_edition(self, built) -> None:
        _, site_dir = built
        root = read(site_dir, "index.html")
        newest = read(site_dir, "editions/2026-08-04/index.html")
        # Same lead story, different link depth.
        assert "The Long Essay" in root
        assert "The Long Essay" in newest
        assert 'href="articles/' in root
        assert 'href="../../articles/' in newest

    def test_bylines_name_the_publication(self, built) -> None:
        _, site_dir = built
        html = read(site_dir, "index.html")
        for name in ("Alpha Review", "Beta Letter", "Gamma Notes"):
            assert name in html

    def test_an_empty_database_still_renders(self, db, tmp_path) -> None:
        """A fresh install must get an explanatory page, not a traceback."""
        report = sitegen.build_site(db, site_dir=tmp_path / "site")
        assert report.ok
        html = read(tmp_path / "site", "index.html")
        assert "No edition yet" in html
        assert "build_edition.py" in html


class TestArticlePage:
    """Reading layout, byline, dek, and a link back to the source."""

    @pytest.fixture
    def html(self, built) -> str:
        _, site_dir = built
        return read(site_dir, "articles/alpha-review/the-long-essay.html")

    def test_headline_and_standfirst(self, html: str) -> None:
        assert 'class="article__headline"' in html
        assert 'class="article__standfirst"' in html
        assert "The Long Essay" in html

    def test_byline_names_publication_and_author(self, html: str) -> None:
        assert "Alpha Review" in html
        assert "Alpha Review Writer" in html

    def test_body_html_is_rendered_not_escaped(self, html: str) -> None:
        """The cleaner already allowlisted these tags, so they render as markup."""
        assert "<p>The opening paragraph of the-long-essay.</p>" in html
        assert "<blockquote><p>A quotation.</p></blockquote>" in html
        assert "&lt;p&gt;" not in html

    def test_original_post_is_linked(self, html: str) -> None:
        assert "https://alpha-review.substack.com/p/the-long-essay" in html
        assert 'rel="noopener noreferrer nofollow"' in html

    def test_links_back_to_publication_and_edition(self, html: str) -> None:
        assert "../../publications/alpha-review.html" in html
        assert "../../editions/2026-08-04/index.html" in html

    def test_titles_are_escaped(self, dataset, tmp_path) -> None:
        """A headline containing markup must not become markup."""
        article = dataset.query(Article).first()
        article.title = 'Trouble & <script>alert("x")</script>'
        dataset.commit()

        sitegen.build_site(dataset, site_dir=tmp_path / "site2")
        html = (
            tmp_path / "site2" / "articles" / article.publication.slug / f"{article.slug}.html"
        ).read_text(encoding="utf-8")
        assert "<script>alert" not in html
        assert "&lt;script&gt;" in html
        assert "Trouble &amp;" in html


class TestPublicationPages:
    """A page per publication, plus a directory."""

    def test_a_publication_lists_only_its_own_articles(self, built) -> None:
        _, site_dir = built
        html = read(site_dir, "publications/alpha-review.html")
        assert "The Long Essay" in html
        assert "Yesterdays Essay" in html
        assert "The Beta Report" not in html

    def test_publication_page_links_to_the_source(self, built) -> None:
        _, site_dir = built
        html = read(site_dir, "publications/alpha-review.html")
        assert "https://alpha-review.substack.com" in html

    def test_the_directory_lists_every_publication(self, built) -> None:
        _, site_dir = built
        html = read(site_dir, "publications/index.html")
        for name in ("Alpha Review", "Beta Letter", "Gamma Notes"):
            assert name in html

    def test_a_paid_publication_is_labelled(self, dataset, tmp_path) -> None:
        publication = dataset.query(Publication).filter_by(slug="gamma-notes").one()
        publication.tier = "paid"
        dataset.commit()

        sitegen.build_site(dataset, site_dir=tmp_path / "site3")
        html = read(tmp_path / "site3", "publications/gamma-notes.html")
        assert "Paid subscription" in html


class TestArchive:
    """The archive and the dated editions."""

    def test_archive_lists_every_edition(self, built) -> None:
        _, site_dir = built
        html = read(site_dir, "archive.html")
        for date in ("August 1, 2026", "August 2, 2026", "August 3, 2026", "August 4, 2026"):
            assert date in html

    def test_editions_are_newest_first(self, built) -> None:
        _, site_dir = built
        html = read(site_dir, "archive.html")
        assert html.index("August 4, 2026") < html.index("August 1, 2026")

    def test_each_edition_page_is_dated(self, built) -> None:
        _, site_dir = built
        html = read(site_dir, "editions/2026-08-01/index.html")
        assert "Saturday, August 1, 2026" in html

    def test_edition_rows_are_recorded(self, built, dataset) -> None:
        _, _ = built
        editions = dataset.query(DailyEdition).order_by(DailyEdition.date).all()
        assert [e.date for e in editions] == [
            "2026-08-01", "2026-08-02", "2026-08-03", "2026-08-04",
        ]
        assert all(e.article_count > 0 for e in editions)
        assert editions[-1].lead_article_id is not None

    def test_rebuilding_does_not_duplicate_edition_rows(self, dataset, tmp_path) -> None:
        sitegen.build_site(dataset, site_dir=tmp_path / "a")
        sitegen.build_site(dataset, site_dir=tmp_path / "b")
        assert dataset.query(DailyEdition).count() == 4


class TestRelativeLinks:
    """Every link is relative, so the site works under a GitHub Pages subpath."""

    def test_no_absolute_internal_links(self, built) -> None:
        import re

        _, site_dir = built
        for page in site_dir.rglob("*.html"):
            html = page.read_text(encoding="utf-8")
            # Root-relative hrefs would break under /<repo>/.
            assert not re.search(r'(?:href|src)="/[^/]', html), f"absolute path in {page.name}"

    def test_stylesheet_depth_is_correct_per_page(self, built) -> None:
        _, site_dir = built
        cases = {
            "index.html": 'href="static/mainspring.css"',
            "archive.html": 'href="static/mainspring.css"',
            "publications/index.html": 'href="../static/mainspring.css"',
            "articles/alpha-review/the-long-essay.html": 'href="../../static/mainspring.css"',
            "editions/2026-08-04/index.html": 'href="../../static/mainspring.css"',
        }
        for page, expected in cases.items():
            assert expected in read(site_dir, page), f"{page} expected {expected}"

    def test_every_internal_link_resolves_to_a_file(self, built) -> None:
        """No page may link to something the build did not write."""
        import re

        _, site_dir = built
        broken: list[str] = []
        for page in site_dir.rglob("*.html"):
            html = page.read_text(encoding="utf-8")
            for target in re.findall(r'(?:href|src)="([^"#:]+)"', html):
                if target.startswith(("http", "//", "mailto:")):
                    continue
                if not (page.parent / target).resolve().exists():
                    broken.append(f"{page.relative_to(site_dir)} -> {target}")
        assert not broken, f"broken links: {broken}"

    def test_rel_prefix_depths(self) -> None:
        assert sitegen.rel_prefix("index.html") == ""
        assert sitegen.rel_prefix("publications/alpha.html") == "../"
        assert sitegen.rel_prefix("articles/alpha/post.html") == "../../"
        assert sitegen.rel_prefix("editions/2026-08-04/index.html") == "../../"


class TestIdempotence:
    """Two builds of the same data must produce identical bytes."""

    def test_output_is_byte_identical(self, dataset, tmp_path, monkeypatch) -> None:
        # "Built <date>" is the one timestamp in the output; pin it so the
        # comparison is about content, not about the clock.
        monkeypatch.setattr(sitegen, "date_medium", lambda value: "Aug 4, 2026")

        first = tmp_path / "first"
        second = tmp_path / "second"
        sitegen.build_site(dataset, site_dir=first)
        sitegen.build_site(dataset, site_dir=second)

        pages = sorted(p.relative_to(first) for p in first.rglob("*.html"))
        assert pages == sorted(p.relative_to(second) for p in second.rglob("*.html"))
        for page in pages:
            assert (first / page).read_bytes() == (second / page).read_bytes(), page

    def test_stale_pages_are_removed(self, dataset, tmp_path) -> None:
        """A renamed post must not leave its old page behind."""
        site_dir = tmp_path / "site"
        sitegen.build_site(dataset, site_dir=site_dir)
        stale = site_dir / "articles" / "alpha-review" / "the-long-essay.html"
        assert stale.exists()

        article = dataset.query(Article).filter_by(slug="the-long-essay").one()
        article.slug = "renamed-essay"
        dataset.commit()

        sitegen.build_site(dataset, site_dir=site_dir)
        assert not stale.exists()
        assert (site_dir / "articles" / "alpha-review" / "renamed-essay.html").exists()

    def test_unowned_files_are_preserved(self, dataset, tmp_path) -> None:
        """SITE_DIR may be a deploy checkout; only our own directories are cleared."""
        site_dir = tmp_path / "site"
        site_dir.mkdir()
        (site_dir / "CNAME").write_text("news.example.com\n", encoding="utf-8")
        (site_dir / ".git").mkdir()

        sitegen.build_site(dataset, site_dir=site_dir)
        assert (site_dir / "CNAME").read_text().strip() == "news.example.com"
        assert (site_dir / ".git").is_dir()


class TestLeadExcerpt:
    """The lead's opening paragraphs, which also stop its column collapsing."""

    BODY = (
        "A short kicker.\n"
        "The first real paragraph of the story, long enough to count as a lede "
        "rather than a stray line of furniture.\n"
        "The second paragraph, also comfortably past the minimum length that the "
        "excerpt builder requires of a block.\n"
        "The third paragraph, which is likewise long enough to be considered a "
        "genuine paragraph of body copy.\n"
        "A fourth paragraph that should fall outside the paragraph cap entirely "
        "and never be returned by the function.\n"
    )

    def test_returns_opening_paragraphs(self) -> None:
        excerpt = sitegen.lead_excerpt(self.BODY)
        assert len(excerpt) == 3
        assert excerpt[0].startswith("The first real paragraph")

    def test_short_leading_lines_are_skipped(self) -> None:
        """A one-line kicker or date stamp is not a lede."""
        assert not any(p == "A short kicker." for p in sitegen.lead_excerpt(self.BODY))

    def test_character_budget_drops_whole_paragraphs(self) -> None:
        excerpt = sitegen.lead_excerpt(self.BODY, budget=150)
        assert len(excerpt) == 1
        # Never truncated mid-sentence.
        assert excerpt[0].endswith(".")

    def test_at_least_one_paragraph_survives_a_tiny_budget(self) -> None:
        assert len(sitegen.lead_excerpt(self.BODY, budget=1)) == 1

    def test_empty_body(self) -> None:
        assert sitegen.lead_excerpt(None) == []
        assert sitegen.lead_excerpt("") == []
        assert sitegen.lead_excerpt("Too short.\nAlso short.") == []

    def test_a_paragraph_restating_the_dek_is_dropped(self) -> None:
        """Extractive summaries repeat the opening; printing it twice looks broken."""
        excerpt = sitegen.lead_excerpt(
            self.BODY,
            standfirst="The first real paragraph of the story, long enough to count",
        )
        assert excerpt[0].startswith("The second paragraph")

    def test_the_dev_mode_placeholder_is_recognised(self) -> None:
        """The [DEV] shim is the article's opening by construction."""
        excerpt = sitegen.lead_excerpt(
            self.BODY,
            standfirst="[DEV] The first real paragraph of the story, long enough…",
        )
        assert excerpt[0].startswith("The second paragraph")

    def test_an_unrelated_dek_drops_nothing(self) -> None:
        excerpt = sitegen.lead_excerpt(
            self.BODY, standfirst="An abstractive summary sharing no wording at all."
        )
        assert excerpt[0].startswith("The first real paragraph")

    def test_only_leading_repeats_are_dropped(self) -> None:
        """A sentence recurring later in the piece is the author's business."""
        body = (
            "An opening paragraph that is entirely its own thing and quite long.\n"
            "A repeated line that the dek also happens to have used verbatim here.\n"
        )
        excerpt = sitegen.lead_excerpt(
            body, standfirst="A repeated line that the dek also happens to have used"
        )
        assert len(excerpt) == 2

    def test_the_front_page_renders_the_excerpt(self, built) -> None:
        _, site_dir = built
        html = read(site_dir, "index.html")
        assert 'class="lead__text"' in html
        assert "Continue reading" in html

    def test_only_the_lead_gets_body_text(self, built) -> None:
        """Secondaries and river items show a dek only."""
        _, site_dir = built
        html = read(site_dir, "index.html")
        assert html.count('class="lead__text"') == 1

    def test_excerpt_text_is_escaped(self, dataset, tmp_path) -> None:
        # Applied to every article, because which one leads is the ranker's
        # decision and this test is about escaping, not ranking.
        for article in dataset.query(Article).all():
            article.body_text = (
                "An opening paragraph containing <script>alert(1)</script> and enough "
                "other words to clear the minimum block length comfortably."
            )
        dataset.commit()

        sitegen.build_site(dataset, site_dir=tmp_path / "esc")
        html = read(tmp_path / "esc", "index.html")
        assert "<script>alert" not in html
        assert "&lt;script&gt;" in html


class TestDateFormatting:
    """Dates are formatted without platform-specific strftime flags."""

    @pytest.mark.parametrize(
        ("value", "long_form", "medium", "short"),
        [
            ("2026-08-04", "Tuesday, August 4, 2026", "Aug 4, 2026", "Aug 4"),
            ("2026-01-01", "Thursday, January 1, 2026", "Jan 1, 2026", "Jan 1"),
            ("2026-12-25", "Friday, December 25, 2026", "Dec 25, 2026", "Dec 25"),
        ],
    )
    def test_formats(self, value, long_form, medium, short) -> None:
        assert sitegen.date_long(value) == long_form
        assert sitegen.date_medium(value) == medium
        assert sitegen.date_short(value) == short

    def test_single_digit_days_have_no_leading_zero(self) -> None:
        assert sitegen.date_long("2026-08-04") == "Tuesday, August 4, 2026"
        assert "0" not in sitegen.date_short("2026-08-04").split()[1]

    def test_accepts_datetimes_too(self) -> None:
        assert sitegen.date_medium(datetime(2026, 8, 4, 13, 5)) == "Aug 4, 2026"


class TestEditionWindow:
    """An edition draws on a window of the wire so a slow day is still a page."""

    def test_a_thin_day_borrows_from_earlier(self, dataset, tmp_path, monkeypatch) -> None:
        """Aug 2 has one article of its own but should still fill its slots."""
        monkeypatch.setattr(sitegen.config, "EDITION_WINDOW_DAYS", 7)
        site_dir = tmp_path / "site"
        sitegen.build_site(dataset, site_dir=site_dir)

        html = read(site_dir, "editions/2026-08-02/index.html")
        assert html.count('class="lead__headline"') == 1
        assert html.count('class="story__headline"') >= 1

    def test_window_of_one_gives_strict_single_day_editions(
        self, dataset, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(sitegen.config, "EDITION_WINDOW_DAYS", 1)
        site_dir = tmp_path / "site"
        sitegen.build_site(dataset, site_dir=site_dir)

        html = read(site_dir, "editions/2026-08-01/index.html")
        # Aug 1 has exactly one article, so it is the lead and nothing else.
        assert html.count('class="lead__headline"') == 1
        assert html.count('class="story__headline"') == 0

    def test_the_window_never_reaches_forward(self, dataset, tmp_path, monkeypatch) -> None:
        """An edition must not contain stories published after its own date."""
        monkeypatch.setattr(sitegen.config, "EDITION_WINDOW_DAYS", 7)
        site_dir = tmp_path / "site"
        sitegen.build_site(dataset, site_dir=site_dir)

        html = read(site_dir, "editions/2026-08-01/index.html")
        assert "The Long Essay" not in html  # published Aug 4
