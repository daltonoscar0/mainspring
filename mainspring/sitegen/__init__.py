"""Static site generation.

:mod:`mainspring.sitegen.rank` decides what goes where on the front page.
:mod:`mainspring.sitegen.build` renders the whole site into ``site/``.

The split is deliberate: ranking is a pure function over article metadata, so it
can be reasoned about and tested without touching Jinja, the database, or the
filesystem — and swapped for something smarter later without touching the
templates.
"""
