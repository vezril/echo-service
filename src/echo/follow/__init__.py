"""Discovery: which channels to follow, and what is new on them.

Loads the channel-follow config (schema in ``codex/docs/echo.md``), polls each channel's
public RSS feed at ``https://www.youtube.com/feeds/videos.xml?channel_id=<ID>``, and reports
uploads not yet seen.

The ``UC…`` ``channel_id`` is canonical — it is what the feed keys on. A ``handle`` or
``url`` is a convenience resolved once on first sync and then recorded, so a handle rename
never loses the subscription.
"""
