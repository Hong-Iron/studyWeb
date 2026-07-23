"""Shared test setup: make the suite fully offline and deterministic.

Env vars are set *before* studyweb is imported so the Settings singleton picks
them up (no network delays, no on-disk cache pollution)."""

import os

os.environ.setdefault("STUDYWEB_PER_HOST_DELAY", "0")
os.environ.setdefault("STUDYWEB_CACHE", "0")
os.environ.setdefault("STUDYWEB_RESPECT_ROBOTS", "false")
os.environ.setdefault("STUDYWEB_MARKET", "en-US")
