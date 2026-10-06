"""Live connectivity probe for every book/award source. Read-only: no DB, no email.

    python test_connections.py            # one request per source
    python test_connections.py --sweep    # also try every curl_cffi profile on StoryGraph

Run it from the network that matters — the weekly cron runs on GitHub's runners,
and Cloudflare judges the runner IP as well as the TLS fingerprint, so a pass on a
laptop says little. `.github/workflows/connections.yml` runs it there on demand.

Not a pytest module (pytest's testpaths is tests/): it hits live sites, and the
answer changes week to week. Exit status is 1 if Goodreads or StoryGraph (with the
pinned IMPERSONATE profile) is unreachable.
"""

import argparse
import re
import sys
import time
import typing
from datetime import date

import requests
from curl_cffi import requests as cffi_requests

import awards
import googlebooks
import scraper
import storygraph
from scraper import GOODREADS_NEW_RELEASES_URL

CHALLENGE = storygraph._CHALLENGE_SIGNATURES


def _verdict(status: int, text: str, marker: str) -> str:
    if status == 403:
        return "BLOCKED (403)"
    if any(sig in text[:2000].lower() for sig in CHALLENGE):
        return "BLOCKED (challenge page)"
    if status != 200:
        return f"FAIL (HTTP {status})"
    count = text.count(marker)
    return f"OK ({count} x '{marker}')" if count else f"SUSPECT (200 but no '{marker}' — markup change?)"


def probe_storygraph(profile: str) -> tuple[bool, str, str | None]:
    """Return (ok, verdict, first book uuid) for the browse page under one profile."""
    try:
        resp = cffi_requests.get(storygraph.BROWSE_URL, impersonate=profile, timeout=25)
    except Exception as e:
        return False, f"ERROR ({str(e)[:200]})", None
    verdict = _verdict(resp.status_code, resp.text, "book-pane")
    book_id = None
    if verdict.startswith("OK"):
        from bs4 import BeautifulSoup
        pane = BeautifulSoup(resp.text, "html.parser").select_one("div.book-pane[data-book-id]")
        book_id = pane.get("data-book-id") if pane else None
    return verdict.startswith("OK"), verdict, book_id


def probe_storygraph_fragment(profile: str, book_id: str) -> str:
    url = f"{storygraph.BASE_URL}/books/{book_id}/community_reviews"
    try:
        resp = cffi_requests.get(url, impersonate=profile, timeout=25)
    except Exception as e:
        return f"ERROR ({str(e)[:200]})"
    return _verdict(resp.status_code, resp.text, "average-star-rating")


def probe_plain(url: str, marker: str, session=requests, **kwargs) -> tuple[bool, str]:
    """GET with the same session (headers, retries) the pipeline uses for that source."""
    try:
        resp = session.get(url, timeout=(5, 20), **kwargs)
    except requests.RequestException as e:
        return False, f"ERROR ({str(e)[:200]})"
    verdict = _verdict(resp.status_code, resp.text, marker)
    return verdict.startswith("OK"), verdict


def all_profiles() -> list[str]:
    from curl_cffi.requests import impersonate
    names = typing.get_args(getattr(impersonate, "BrowserTypeLiteral", typing.Literal[()]))
    # Versioned names only. The bare aliases ("chrome") and the legacy dotted
    # spellings ("safari18_0" == "safari180") duplicate a canonical name.
    return ([n for n in names if any(c.isdigit() for c in n) and not re.match(r"safari\d+_\d", n)]
            or list(storygraph.IMPERSONATE_FALLBACKS))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sweep", action="store_true",
                    help="try every curl_cffi impersonation profile against StoryGraph")
    args = ap.parse_args()

    import curl_cffi
    print(f"curl_cffi {curl_cffi.__version__}; pinned IMPERSONATE={storygraph.IMPERSONATE}\n")
    today = date.today()
    critical_ok = True

    ok, verdict = probe_plain(
        GOODREADS_NEW_RELEASES_URL.format(year=today.year, month=today.month),
        "__NEXT_DATA__", session=scraper._session)
    critical_ok &= ok
    print(f"{'Goodreads new-release page':38} {verdict}")

    ok, verdict, book_id = probe_storygraph(storygraph.IMPERSONATE)
    critical_ok &= ok
    print(f"{'StoryGraph browse (' + storygraph.IMPERSONATE + ')':38} {verdict}")
    if book_id:
        print(f"{'StoryGraph rating fragment':38} {probe_storygraph_fragment(storygraph.IMPERSONATE, book_id)}")

    for name, url, marker, kw in [
        ("Google Books API (opt-in)", googlebooks.API_URL, '"items"',
         {"params": {"q": googlebooks.SUBJECT_QUERY, "maxResults": 1}}),
        (f"sfadb {today.year} (awards)", awards.SFADB_URL.format(year=today.year),
         "chronowinsblock", {"session": awards._session}),
        (f"Wikipedia {today.year} (awards)", awards.WIKIPEDIA_API, "wikitable",
         {"session": awards._session,
          "params": {"action": "parse", "page": f"{today.year} in literature",
                     "prop": "text", "format": "json", "formatversion": 2}}),
    ]:
        print(f"{name:38} {probe_plain(url, marker, **kw)[1]}")

    if args.sweep:
        print("\nStoryGraph profile sweep (browse page):")
        working = []
        for profile in all_profiles():
            ok, verdict, _ = probe_storygraph(profile)
            print(f"  {profile:20} {verdict}")
            if ok:
                working.append(profile)
            time.sleep(1.5)
        print(f"\nWorking profiles: {', '.join(working) or 'NONE'}")
        if working and storygraph.IMPERSONATE not in working:
            print(f"-> set IMPERSONATE = {working[-1]!r} in storygraph.py")

    return 0 if critical_ok else 1


if __name__ == "__main__":
    sys.exit(main())
