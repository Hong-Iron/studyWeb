"""Shared test setup: make the suite fully offline and deterministic.

Env vars are set *before* studyweb is imported so the Settings singleton picks
them up (no network delays, no on-disk cache pollution)."""

import os

os.environ.setdefault("STUDYWEB_PER_HOST_DELAY", "0")
os.environ.setdefault("STUDYWEB_CACHE", "0")
os.environ.setdefault("STUDYWEB_RESPECT_ROBOTS", "false")
os.environ.setdefault("STUDYWEB_MARKET", "en-US")
# Pin the fetch ladder to the one engine the tests monkeypatch. Otherwise a
# simulated 403 would escalate for real and start a browser on a dev machine
# that happens to have Chrome. test_engines.py exercises escalation with its
# own fake engines instead.
os.environ.setdefault("STUDYWEB_FETCH_LADDER", "static")
