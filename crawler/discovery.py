"""
discovery.py - independent URL-discovery methods, merged and de-duplicated:
  sitemap      configured sitemaps (child sitemaps of other languages are skipped)
  robots       extra sitemaps advertised in robots.txt
  feeds        RSS / Atom feeds (configured + auto-detected)
  wayback      Internet Archive CDX index (finds orphan pages no sitemap or link points to)
  commoncrawl  Common Crawl index (server-side filters return nothing -> filtered here)
  bfs          links found while crawling, remembered from previous runs
Archive methods are slow and flaky (503/504): back off, never block, cache archive_refresh_days.
"""
import json
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from lxml import etree

from crawler.filters import site_accepts, site_domains, apply_latest_version
from crawler.sitemap import parse_sitemaps
from crawler.utils import normalize_url, is_foreign_language_url, now_iso, age_days

ALL_METHODS = ["sitemap", "robots", "feeds", "wayback", "commoncrawl", "bfs"]
ARCHIVE_METHODS = {"wayback", "commoncrawl"}
_SITEMAP_CACHE = {}


def _langs(site):
    return site.get("allowed_languages") or ["en"]


def _prefix(site):
    p = urlparse(site["base_url"])
    return site.get("archive_prefix") or (p.netloc + (p.path or "/"))


def _sitemaps(ctx, site, urls):
    langs = _langs(site)
    key = (tuple(sorted(set(urls))), tuple(langs))
    if key not in _SITEMAP_CACHE:
        _SITEMAP_CACHE[key] = parse_sitemaps(ctx.fetcher, list(key[0]), ctx.log, rps=site.get("requests_per_second"),
                                             accept=lambda u: not is_foreign_language_url(u, langs))
    return _SITEMAP_CACHE[key]


def _archive_get(sess, url, params, log, attempts=4, timeout=180):
    for a in range(1, attempts + 1):
        try:
            r = sess.get(url, params=params, timeout=timeout)
        except requests.RequestException as e:
            log.warning(f"    archive request error {type(e).__name__} (attempt {a})")
            time.sleep(10 * a)
            continue
        if r.status_code in (200, 404):
            return r
        if r.status_code in (429, 500, 502, 503, 504):
            log.warning(f"    archive HTTP {r.status_code} (attempt {a}) - backing off")
            time.sleep(20 * a)
            continue
        return None
    return None


def _m_sitemap(site, ctx, sess):
    return _sitemaps(ctx, site, site["sitemaps"]) if site.get("sitemaps") else {}


def _m_robots(site, ctx, sess):
    configured, extra = set(site.get("sitemaps") or []), set()
    for d in site_domains(site):
        res = ctx.fetcher.get(f"https://{d}/robots.txt", expect_html=False, max_bytes=2 * 1024 * 1024)
        if res.error or res.status != 200:
            continue
        for line in res.content.decode("utf-8", errors="replace").splitlines():
            if line.strip().lower().startswith("sitemap:"):
                sm = line.split(":", 1)[1].strip()
                if (urlparse(sm).netloc in site_domains(site) and sm not in configured
                        and not is_foreign_language_url(sm, _langs(site))):
                    extra.add(sm)
    return _sitemaps(ctx, site, sorted(extra)) if extra else {}


def _m_feeds(site, ctx, sess):
    feeds = list(site.get("feeds") or [])
    res = ctx.fetcher.get(site["base_url"], rps=site.get("requests_per_second"))
    if res.status == 200 and res.content:
        for link in BeautifulSoup(res.content, "lxml").find_all("link", href=True):
            t = (link.get("type") or "").lower()
            if "rss" in t or "atom" in t:
                feeds.append(urljoin(site["base_url"], link["href"]))
    out = {}
    for f in dict.fromkeys(feeds):
        res = ctx.fetcher.get(f, expect_html=False, max_bytes=20 * 1024 * 1024)
        if res.error or res.status != 200 or not res.content:
            continue
        try:
            root = etree.fromstring(res.content, parser=etree.XMLParser(recover=True, resolve_entities=False))
        except etree.XMLSyntaxError:
            continue
        for el in (root.iter() if root is not None else []):
            if isinstance(el.tag, str) and etree.QName(el).localname == "link":
                href = (el.get("href") or el.text or "").strip()
                if href.startswith(("http://", "https://")):
                    out[href] = None
    return out


def _m_wayback(site, ctx, sess):
    api = "https://web.archive.org/cdx/search/cdx"
    params = {"url": _prefix(site), "matchType": "prefix", "output": "json", "fl": "original", "collapse": "urlkey",
              "filter": ["statuscode:200", "mimetype:text/html"], "from": str(site.get("archive_from_year", 2023))}
    r = _archive_get(sess, api, {**params, "showNumPages": "true"}, ctx.log)
    if r is None:
        return None
    try:
        pages = max(1, int(r.text.strip().strip("[]") or 1))
    except ValueError:
        pages = 1
    out, ok_any = {}, False
    for p in range(min(pages, int(site.get("archive_max_pages", 25)))):
        r = _archive_get(sess, api, {**params, "page": str(p)}, ctx.log)
        if r is None:
            break
        ok_any = True
        if r.status_code == 404 or not r.text.strip():
            continue
        try:
            rows = r.json()
        except ValueError:
            continue
        for row in rows:
            if row and row[0] != "original":
                out[row[0]] = None
        time.sleep(1.5)
    return out if ok_any else None


def _m_commoncrawl(site, ctx, sess):
    r = _archive_get(sess, "https://index.commoncrawl.org/collinfo.json", None, ctx.log)
    if r is None or r.status_code != 200:
        return None
    try:
        colls = r.json()
    except ValueError:
        return None
    params = {"url": _prefix(site).rstrip("/") + "/*", "output": "json"}
    out, ok_any = {}, False
    for coll in colls[: int(site.get("commoncrawl_crawls", 3))]:
        api = coll.get("cdx-api") or f"https://index.commoncrawl.org/{coll['id']}-index"
        r = _archive_get(sess, api, {**params, "showNumPages": "true"}, ctx.log)
        if r is None:
            continue
        if r.status_code == 404:
            ok_any = True
            continue
        try:
            pages = int(r.json().get("pages", 1))
        except (ValueError, AttributeError):
            pages = 1
        for p in range(min(pages, int(site.get("archive_max_pages", 25)))):
            r = _archive_get(sess, api, {**params, "page": str(p)}, ctx.log)
            if r is None:
                break
            ok_any = True
            if r.status_code == 404:
                break
            for line in r.text.splitlines():
                try:
                    j = json.loads(line)
                except ValueError:
                    continue
                if (isinstance(j, dict) and j.get("url") and str(j.get("status")) == "200"
                        and "html" in (j.get("mime") or "")):
                    out[j["url"]] = None
            time.sleep(2)
        ctx.log.info(f"    commoncrawl {coll.get('id')}: {len(out)} urls so far")
    return out if ok_any else None


METHOD_FUNCS = {"sitemap": _m_sitemap, "robots": _m_robots, "feeds": _m_feeds,
                "wayback": _m_wayback, "commoncrawl": _m_commoncrawl}


def discover_all(site, ctx):
    """Returns (union {url: lastmod}, found_by {url: {methods}}, report {method: in-scope count})."""
    name, log = site["name"], ctx.log
    methods = list(ctx.methods or site.get("discovery") or ALL_METHODS)
    sess = requests.Session()
    sess.headers["User-Agent"] = site.get("user_agent") or "crawl2LocalRag"
    site["_latest_versions"] = None
    raw_by, fresh = {}, set()
    for m in methods:
        if m == "bfs":
            raw_by[m] = ctx.state.discovered(name, "bfs")
            continue
        fn = METHOD_FUNCS.get(m)
        if fn is None:
            log.warning(f"  unknown discovery method '{m}' - ignored")
            continue
        if m in ARCHIVE_METHODS and not ctx.refresh_archives:
            last = ctx.state.get_kv(f"discovery_last:{name}:{m}")
            if last and age_days(last) < float(site.get("archive_refresh_days", 30)):
                raw_by[m] = ctx.state.discovered(name, m)
                log.info(f"  [{m}] cached {last[:10]}: {len(raw_by[m])} urls")
                continue
        t0 = time.time()
        try:
            raw = fn(site, ctx, sess)
        except Exception as e:
            log.warning(f"  [{m}] failed: {type(e).__name__}: {str(e)[:150]}")
            raw = None
        if raw is None:
            log.warning(f"  [{m}] no result this time - the other methods still cover this site")
            continue
        raw_by[m] = raw
        if m in ARCHIVE_METHODS:
            fresh.add(m)
        log.info(f"  [{m}] {len(raw)} raw urls ({time.time() - t0:.0f}s)")

    norm_by = {}
    for m, raw in raw_by.items():
        d = {}
        for u, lm in raw.items():
            n = normalize_url(u, site.get("keep_query", False))
            if n and site_accepts(site, n) and (n not in d or (lm and (d[n] or "") < lm)):
                d[n] = lm
        norm_by[m] = d
    union = {}
    for d in norm_by.values():
        for u, lm in d.items():
            if u not in union or (lm and (union[u] or "") < lm):
                union[u] = lm
    if site.get("latest_version_only"):
        union = apply_latest_version(site, union, log)
        norm_by = {m: {u: lm for u, lm in d.items() if u in union} for m, d in norm_by.items()}
    for m, d in norm_by.items():
        if m in fresh or m in ("sitemap", "robots", "feeds"):
            ctx.state.add_discovered(name, m, d)
    for m in fresh:
        ctx.state.set_kv(f"discovery_last:{name}:{m}", now_iso())
    ctx.state.commit()
    found_by = {}
    for m, d in norm_by.items():
        for u in d:
            found_by.setdefault(u, set()).add(m)
    return union, found_by, {m: len(d) for m, d in norm_by.items()}
