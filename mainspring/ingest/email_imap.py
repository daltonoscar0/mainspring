"""email_imap.py — ingest paid publications from email. Milestone 2.

Not implemented yet. This module exists now so the interface it will satisfy is
fixed while the RSS path is being built, and so nothing downstream has to change
when it lands.

Why email
---------
A paid Substack's RSS feed carries a teaser and a paywall link — the full post
is never in the feed. But it is already in the subscriber's inbox: Substack
emails paid posts in full, as HTML. So the content is available without
scraping, without credentials for substack.com, and without asking any third
party for anything. Reading a mailbox the user already controls is the
privacy-preserving way in.

Two supported arrangements
--------------------------
1. **IMAP against an existing mailbox.** The user points Mainspring at the
   account their subscriptions arrive on, read-only. Handled by
   :class:`ImapMailboxSource` using :mod:`imaplib` from the standard library —
   no new dependency.

2. **A forward-to address.** The user sets up a filter that forwards Substack
   mail to a dedicated address, and Mainspring reads only that. Preferred:
   Mainspring never sees the rest of the inbox, and the credentials it holds
   grant access to nothing else. Mechanically the same IMAP client against a
   different mailbox, which is why one implementation covers both.

How a message becomes an Article
--------------------------------
Substack's post emails are consistent enough to parse structurally rather than
heuristically:

  * ``From`` is ``<publication>@substack.com`` (or the publication's custom
    sending domain), and ``List-Id`` / ``X-Substack-Publication`` name the
    publication — that is how a message is matched to a subscription without
    guessing from the display name.
  * ``Subject`` is the post title, occasionally prefixed with the publication
    name and a separator.
  * The ``text/html`` part wraps the post body in table-based email chrome. The
    body region is the same markup the web post uses, so
    :func:`mainspring.ingest.substack_html.clean_body_html` handles it directly
    once the wrapper is peeled off.
  * The canonical post URL is in the header link and in the "View this post on
    the web" anchor. It is the deduplication key, identical to the one the RSS
    path stores, so a publication that switches tiers never duplicates a post.

Because both paths end at ``clean_body_html``, an emailed post and a fetched
post produce the same ``body_html``, the same ``body_text``, and therefore the
same standfirst.

Open questions to settle when this is built
-------------------------------------------
  * Credential storage. Environment variables are the plan (``MAINSPRING_IMAP_*``),
    since a self-hosted install already trusts its own environment and a config
    file invites accidental commits. A keyring backend would be better but adds
    a dependency and a platform matrix.
  * Idempotency across runs. Tracking the highest-seen ``UID`` per mailbox is
    cheap and survives restarts, but UIDs reset if the mailbox is recreated;
    ``Message-Id`` is stable but needs its own table. Probably both: UID as a
    fast-forward hint, ``Message-Id`` as the correctness guarantee.
  * Whether to delete or archive after ingest. Default should be neither —
    Mainspring should be safe to point at a real mailbox read-only.
  * Digest and thread emails, which bundle several posts into one message and
    need splitting before parsing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Protocol

from ..config import Subscription
from .rss import IngestReport

#: Environment variables this module will read once implemented. Named here so
#: the eventual README section and the implementation cannot drift apart.
ENV_HOST = "MAINSPRING_IMAP_HOST"
ENV_PORT = "MAINSPRING_IMAP_PORT"
ENV_USER = "MAINSPRING_IMAP_USER"
ENV_PASSWORD = "MAINSPRING_IMAP_PASSWORD"
ENV_MAILBOX = "MAINSPRING_IMAP_MAILBOX"


@dataclass(frozen=True)
class MailboxConfig:
    """Connection details for the mailbox paid posts arrive in.

    Attributes:
        host:      IMAP server hostname.
        port:      IMAP port. 993 for implicit TLS, which is the only mode that
                   will be supported — there is no reason to offer plaintext.
        user:      Account username.
        password:  Account password or app-specific password.
        mailbox:   Folder to read. ``INBOX`` for a dedicated forward-to address,
                   or the folder a filter files Substack mail into.
        read_only: Open the mailbox without write access so ingestion can never
                   modify or delete the user's mail. Default, and the only value
                   Milestone 2 will ship.
    """

    host: str
    port: int = 993
    user: str = ""
    password: str = ""
    mailbox: str = "INBOX"
    read_only: bool = True


@dataclass(frozen=True)
class RawMessage:
    """One fetched email, before parsing.

    Kept deliberately dumb — bytes plus the identifiers needed for idempotency —
    so the parser can be tested against a saved ``.eml`` file with no mailbox
    involved.

    Attributes:
        message_id: RFC 5322 ``Message-Id``. The stable dedup key.
        uid:        IMAP UID within the mailbox. A fast-forward hint only.
        raw:        Complete RFC 5322 message bytes.
    """

    message_id: str
    uid: int
    raw: bytes


class MailboxSource(Protocol):
    """Where messages come from.

    A Protocol rather than a base class so the test suite can supply a source
    backed by fixture files without importing :mod:`imaplib` at all.
    """

    def fetch_since(self, last_uid: int = 0) -> Iterable[RawMessage]:
        """Yield messages with a UID greater than ``last_uid``, oldest first."""
        ...


class ImapMailboxSource:
    """A :class:`MailboxSource` backed by an IMAP server. Not implemented.

    Will wrap :class:`imaplib.IMAP4_SSL`: connect, ``SELECT`` the mailbox
    read-only, ``UID SEARCH`` for Substack senders above ``last_uid``, then
    ``UID FETCH`` each result.
    """

    def __init__(self, config: MailboxConfig) -> None:
        self.config = config

    def fetch_since(self, last_uid: int = 0) -> Iterable[RawMessage]:
        """Not implemented — see the module docstring for the plan."""
        raise NotImplementedError(
            "IMAP ingestion arrives in Milestone 2. Paid publications are "
            "currently skipped; free publications work over RSS today."
        )


def parse_substack_email(message: RawMessage) -> dict:
    """Turn one Substack post email into an article record. Not implemented.

    Will return exactly the dict shape :func:`mainspring.ingest.rss.upsert_article`
    already consumes — title, url, slug, author, published_date, body_html,
    body_text, word_count, hero_image_url, hero_image_alt — with
    ``source_kind="email"``. Reusing that shape is the point: the persistence
    layer needs no changes when this lands.

    Args:
        message: The raw email to parse.

    Raises:
        NotImplementedError: always, for now.
    """
    raise NotImplementedError(
        "Substack email parsing arrives in Milestone 2. The body will be routed "
        "through mainspring.ingest.substack_html.clean_body_html, the same "
        "cleaner the RSS path uses."
    )


def ingest(
    subs: list[Subscription] | None = None,
    *,
    source: MailboxSource | None = None,
) -> IngestReport:
    """Ingest paid publications from email. Not implemented.

    Signature mirrors :func:`mainspring.ingest.rss.ingest` so
    ``scripts/build_edition.py`` can call both once this works, and returns the
    same :class:`~mainspring.ingest.rss.IngestReport`.

    Args:
        subs:   Subscriptions to consider. Only ``tier: paid`` entries apply;
                free ones are the RSS ingester's job.
        source: Override the mailbox, for tests. Defaults to an
                :class:`ImapMailboxSource` built from the environment.

    Raises:
        NotImplementedError: always, for now.
    """
    raise NotImplementedError(
        "Email ingestion arrives in Milestone 2. Run mainspring.ingest.rss.ingest() "
        "for free publications."
    )
