"""Ingestion paths.

Two ways content reaches Mainspring:

  :mod:`mainspring.ingest.rss`         — free publications, over RSS. Works today.
  :mod:`mainspring.ingest.email_imap`  — paid publications, over IMAP. Milestone 2.

Both funnel their HTML through :mod:`mainspring.ingest.substack_html`, so a post
looks identical on the site whether it arrived in a feed or in an inbox.
"""
