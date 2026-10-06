"""Offline tests for StoryGraph's impersonation-profile fallback in storygraph._get.

The network is monkeypatched: these pin our rotation logic (adopt the first working
profile, stop touching the network once every profile is rejected), not the site.
"""

import pytest

import storygraph


class _Resp:
    def __init__(self, status: int, text: str = "<html>ok</html>"):
        self.status_code = status
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture
def fake_get(monkeypatch):
    """Route cffi_requests.get through a profile -> response table; record calls."""
    calls: list[str] = []
    table: dict[str, _Resp] = {}

    def get(url, params=None, impersonate=None, timeout=None):
        calls.append(impersonate)
        return table.get(impersonate, _Resp(403))

    monkeypatch.setattr(storygraph.cffi_requests, "get", get)
    monkeypatch.setattr(storygraph.time, "sleep", lambda s: None)
    monkeypatch.setattr(storygraph, "IMPERSONATE_FALLBACKS", ("p1", "p2", "p3"))
    monkeypatch.setattr(storygraph, "_active_profile", "pinned")
    monkeypatch.setattr(storygraph, "_all_profiles_blocked", False)
    monkeypatch.setattr(storygraph, "_consecutive_blocked_urls", 0)
    monkeypatch.setattr(storygraph, "BLOCKED_AFTER_URLS", 2)
    return calls, table


def test_pinned_profile_used_when_it_works(fake_get):
    calls, table = fake_get
    table["pinned"] = _Resp(200)
    assert storygraph._get(storygraph.BROWSE_URL) == "<html>ok</html>"
    assert calls == ["pinned"]


def test_403_rotates_to_first_working_fallback_and_keeps_it(fake_get):
    calls, table = fake_get
    table["p2"] = _Resp(200)
    assert storygraph._get(storygraph.BROWSE_URL) == "<html>ok</html>"
    assert calls == ["pinned", "p1", "p2"]
    assert storygraph._active_profile == "p2"

    calls.clear()
    storygraph._get(storygraph.BROWSE_URL)
    assert calls == ["p2"]  # adopted for the rest of the run


def test_challenge_page_counts_as_blocked(fake_get):
    calls, table = fake_get
    table["pinned"] = _Resp(200, "<title>Just a moment...</title>")
    table["p1"] = _Resp(200)
    assert storygraph._get(storygraph.BROWSE_URL) == "<html>ok</html>"
    assert storygraph._active_profile == "p1"


def test_one_fully_rejected_url_does_not_block_the_run(fake_get):
    """Rejection is partly per-request, so a single unlucky URL must not end the feed."""
    calls, table = fake_get
    assert storygraph._get(storygraph.BROWSE_URL) is None
    assert calls == ["pinned", "p1", "p2", "p3"]
    assert not storygraph._all_profiles_blocked

    table["pinned"] = _Resp(200)
    calls.clear()
    assert storygraph._get(storygraph.BROWSE_URL) == "<html>ok</html>"
    assert storygraph._consecutive_blocked_urls == 0  # a success resets the count


def test_consecutive_rejected_urls_stop_further_requests(fake_get):
    calls, _ = fake_get
    storygraph._get(storygraph.BROWSE_URL)
    storygraph._get(storygraph.BROWSE_URL)  # BLOCKED_AFTER_URLS = 2 in the fixture
    assert storygraph._all_profiles_blocked

    calls.clear()
    assert storygraph._get(storygraph.BROWSE_URL) is None
    assert calls == []  # no more requests once the source is judged blocked
