"""Echo — an anonymous YouTube follower for the Codex constellation.

Echo follows YouTube channels **without a Google account**: it polls each channel's public
RSS feed, downloads new uploads with yt-dlp (optionally through a ProtonVPN egress), and
hands the file to the existing media pipeline. It is a thin worker on the Argus pattern — it
ships no storage, no player and no transcoder, because Apollo, Artemis and Hephaestus
already are those things.

The design, and the cross-service findings that constrain it, live in ``docs/DESIGN.md``.
"""

__version__ = "0.1.0"
