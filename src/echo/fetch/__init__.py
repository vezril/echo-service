"""Download: the yt-dlp wrapper and the pre-download filters.

Owns the optional ProtonVPN egress (default on; credentials come from a Secret and never
from config), the quality cap, container choice, SponsorBlock, subtitles, and the filters
that drop noise *before* spending bandwidth on it — duration bounds, title include/exclude
regexes, and Shorts/livestream handling.

yt-dlp versus YouTube is cat-and-mouse; plan for mostly-unattended, occasionally-maintained.
"""
