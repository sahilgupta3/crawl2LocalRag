"""
sitemap.py - recursive sitemap reader: indexes, gzip, and detection of fake "sitemaps"
(JavaScript apps that return their HTML shell with HTTP 200). Returns {url: lastmod_or_None}.
accept(url) can skip child sitemaps, e.g. those of other languages.
"""
import gzip

from lxml import etree


def parse_sitemaps(fetcher, sitemap_urls, log, rps=None, accept=None, max_sitemaps=2000):
    results, seen, queue = {}, set(), list(sitemap_urls)
    while queue:
        sm = queue.pop(0)
        if sm in seen:
            continue
        if len(seen) >= max_sitemaps:
            log.warning(f"  sitemap limit ({max_sitemaps}) reached")
            break
        seen.add(sm)
        res = fetcher.get(sm, rps=rps, expect_html=False, max_bytes=100 * 1024 * 1024)
        if res.error or res.status != 200:
            log.warning(f"  sitemap {sm} -> HTTP {res.status} {res.error}")
            continue
        body = res.content
        if body[:2] == b"\x1f\x8b":
            try:
                body = gzip.decompress(body)
            except OSError:
                log.warning(f"  sitemap {sm} -> bad gzip")
                continue
        head = body[:5000].lower()
        if b"<urlset" not in head and b"<sitemapindex" not in head:
            log.warning(f"  sitemap {sm} is not XML - ignored")
            continue
        try:
            root = etree.fromstring(body, parser=etree.XMLParser(recover=True, huge_tree=True, resolve_entities=False))
        except etree.XMLSyntaxError as e:
            log.warning(f"  sitemap {sm} -> XML error {e}")
            continue
        if root is None:
            continue
        is_index = etree.QName(root).localname.lower() == "sitemapindex"
        for entry in root:
            if not isinstance(entry.tag, str):
                continue
            loc = lastmod = None
            for child in entry:
                if not isinstance(child.tag, str):
                    continue
                name = etree.QName(child).localname
                if name == "loc" and child.text:
                    loc = child.text.strip()
                elif name == "lastmod" and child.text:
                    lastmod = child.text.strip()
            if not loc:
                continue
            if is_index:
                if accept is None or accept(loc):
                    queue.append(loc)
            else:
                prev = results.get(loc)
                results[loc] = max([v for v in (prev, lastmod) if v], default=None)
    log.info(f"  sitemaps read: {len(seen)} | URLs listed: {len(results)}")
    return results
