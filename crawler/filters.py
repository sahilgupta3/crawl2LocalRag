"""filters.py - URL scope rules shared by every discovery method and the crawl loop."""
import re
from urllib.parse import urlparse

from crawler.utils import is_blocked_url, is_foreign_language_url

_VERSION_SEG = re.compile(r"^\d+(?:\.\d+)+$")


def _patterns(site, key):
    ck = f"_re_{key}"
    if ck not in site:
        site[ck] = [re.compile(p) for p in (site.get(key) or [])]
    return site[ck]


def site_domains(site):
    return site.get("allowed_domains") or [urlparse(site["base_url"]).netloc]


def split_version(url):
    """'/manual/10.6/topic' -> ('/manual', (10, 6))"""
    segs = urlparse(url).path.split("/")
    for i, s in enumerate(segs):
        if _VERSION_SEG.match(s):
            return "/".join(segs[:i]), tuple(int(x) for x in s.split("."))
    return None, None


def version_ok(site, url):
    latest = site.get("_latest_versions")
    if not latest:
        return True
    prefix, ver = split_version(url)
    return prefix is None or prefix not in latest or latest[prefix] == ver


def site_accepts(site, url):
    if not url or is_blocked_url(url) or urlparse(url).netloc not in site_domains(site):
        return False
    if is_foreign_language_url(url, site.get("allowed_languages") or ["en"]) or not version_ok(site, url):
        return False
    inc, exc = _patterns(site, "include_patterns"), _patterns(site, "exclude_patterns")
    if inc and not any(r.search(url) for r in inc):
        return False
    return not any(r.search(url) for r in exc)


def apply_latest_version(site, urls, log):
    latest = {}
    for u in urls:
        prefix, ver = split_version(u)
        if prefix is not None and ver > latest.get(prefix, ()):
            latest[prefix] = ver
    site["_latest_versions"] = latest
    kept = {u: lm for u, lm in urls.items() if version_ok(site, u)}
    log.info(f"  latest-version filter: {len(latest)} versioned manuals | {len(urls)} -> {len(kept)} URLs")
    return kept
