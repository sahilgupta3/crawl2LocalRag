"""
crawl_site.py - the crawl loop for one website.
  1. discover URLs with every configured method (newest sitemap dates first)
  2. skip what is known unchanged (sitemap lastmod) or recently known bad
  3. fetch: static threads | headless Chromium | auto (static first, Chromium on 403 / thin / bot page)
  4. quality gate -> redirect handling -> duplicate-content check -> save .md + .meta.json
  5. follow in-scope links found on fetched pages (remembered for future runs)
"""
import logging
import re
import time
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from crawler.discovery import discover_all
from crawler.extractor import extract, Extracted
from crawler.filters import site_accepts, site_domains
from crawler.utils import normalize_url, is_blocked_url, md5, url_to_filename, save_document, now_iso, age_days

NON_BREAKER = {"not_html", "too_large", "redirected_offsite", "http_404", "http_410"}


@dataclass
class Context:
    state: object
    fetcher: object
    renderer: object
    data_dir: Path
    log: logging.Logger
    force: bool = False
    limit: int = 0
    methods: Optional[list] = None
    refresh_archives: bool = False
    known: dict = field(default_factory=dict)
    seen_run: set = field(default_factory=set)
    hash_index: dict = field(default_factory=dict)


@dataclass
class PageResult:
    url: str
    save: bool = True
    mode: str = "static"
    http_status: int = 0
    error: str = ""
    thin: bool = False
    needs_render: bool = False
    final_url: str = ""
    links: list = field(default_factory=list)
    extract: Optional[Extracted] = None


def fetch_page(url, site, ctx, use_browser, save):
    """Fetch + extract + quality gate. Never raises. The static path is thread-safe."""
    res = PageResult(url=url, save=save, mode="browser" if use_browser else "static")
    try:
        rps = site.get("requests_per_second")
        if use_browser:
            ctx.fetcher.throttle(url, rps)
            fr = ctx.renderer.render(url, wait_selector=site.get("wait_selector"))
        else:
            fr = ctx.fetcher.get(url, rps=rps, expect_html=True)
        res.http_status = fr.status
        if fr.error:
            res.error = fr.error
            return res
        if fr.status != 200:
            res.error = f"http_{fr.status}"
            res.needs_render = (not use_browser) and fr.status == 403
            return res
        final = normalize_url(fr.final_url or url, site.get("keep_query", False))
        if final and urlparse(final).netloc not in site_domains(site):
            res.error = "redirected_offsite"
            return res
        res.final_url = final or url
        ex = extract(fr.text or fr.content, fr.final_url or url, site)
        res.extract, res.links = ex, ex.links
        if not save:
            return res
        if ex.blocked:
            res.error, res.needs_render = "bot_challenge_page", not use_browser
            return res
        if ex.text_len < int(site.get("min_text_chars", 300)):
            res.thin, res.needs_render = True, not use_browser
        return res
    except Exception as e:
        res.error = f"exception:{type(e).__name__}:{str(e)[:150]}"
        return res


def _run_batch(work, site, ctx):
    render = site.get("render", "static")
    if render == "playwright":
        return [fetch_page(u, site, ctx, True, s) for u, s in work]
    with ThreadPoolExecutor(max_workers=max(1, int(site.get("workers", 4)))) as pool:
        results = [f.result() for f in as_completed([pool.submit(fetch_page, u, site, ctx, False, s) for u, s in work])]
    if render == "auto":
        results = [fetch_page(r.url, site, ctx, True, r.save) if r.needs_render else r for r in results]
    return results


def build_document(ex, url):
    body = ex.markdown.lstrip()
    first = body.split("\n", 1)[0]
    if first.startswith("# ") and first[2:].strip().lower() == ex.title.strip().lower():
        body = body.split("\n", 1)[1] if "\n" in body else ""
    header = [f"# {ex.title}", "", f"Source: {url}"]
    if ex.published:
        header.append(f"Published: {ex.published}")
    if ex.modified:
        header.append(f"Last updated: {ex.modified}")
    return "\n".join(header) + "\n\n" + body.strip() + "\n"


def _drop_old(ctx, url, new_status):
    row = ctx.known.get(url)
    if not row:
        return
    if row.get("status") == "ok" and row.get("local_path"):
        p = ctx.data_dir / row["local_path"]
        for f in (p, p.with_suffix(".meta.json")):
            f.unlink(missing_ok=True)
        if ctx.hash_index.get(row.get("content_hash")) == url:
            ctx.hash_index.pop(row.get("content_hash"), None)
    row["status"] = new_status


def should_skip(url, lastmod, ctx, site):
    """'' = fetch | 'lastmod' = unchanged per sitemap | 'known_bad' = recently thin / gone / redirect / failed."""
    row = ctx.known.get(url)
    if ctx.force or not row:
        return ""
    st, recheck = row.get("status"), float(site.get("recheck_days", 14))
    if st == "ok":
        exists = row.get("local_path") and (ctx.data_dir / row["local_path"]).exists()
        return "lastmod" if (lastmod and row.get("lastmod") == lastmod and exists) else ""
    if st in ("thin", "gone", "redirect", "duplicate"):
        fresh = (lastmod and row.get("lastmod") == lastmod) or age_days(row.get("last_checked")) < recheck
        return "known_bad" if fresh else ""
    if st == "failed" and int(row.get("fail_count") or 0) >= 3 and age_days(row.get("last_checked")) < recheck:
        return "known_bad"
    return ""


def handle_result(res, site, ctx, lastmods, stats, found_by=None):
    """Main thread only: writes state + files. Returns the outcome label."""
    name = site["name"]
    if not res.save:
        stats["discovery_pages"] += 1
        return "discovery"
    url, lastmod = res.url, lastmods.get(res.url)
    if res.error:
        gone = res.http_status in (404, 410)
        ctx.state.record_failed(url, name, lastmod, res.http_status, res.mode, res.error, gone=gone)
        if gone:
            _drop_old(ctx, url, "gone")
        outcome = "gone" if gone else "failed"
        stats[outcome] += 1
        ctx.log.debug(f"  [{outcome.upper()}] {url} ({res.error})")
        return outcome
    if res.final_url and res.final_url != url:
        ctx.state.record_status(url, name, "redirect", lastmod, res.http_status, res.mode, f"redirect_to:{res.final_url}")
        _drop_old(ctx, url, "redirect")
        stats["redirects"] += 1
        if res.final_url in ctx.seen_run or not site_accepts(site, res.final_url):
            return "redirect"
        ctx.seen_run.add(res.final_url)
        url, lastmod = res.final_url, lastmods.get(res.final_url)
    ex = res.extract
    if res.thin:
        ctx.state.record_thin(url, name, lastmod, res.http_status, res.mode, ex.title if ex else None)
        stats["thin"] += 1
        return "thin"
    h = md5(ex.markdown)
    row = ctx.known.get(url)
    if (not ctx.force and row and row.get("status") == "ok" and row.get("content_hash") == h
            and row.get("local_path") and (ctx.data_dir / row["local_path"]).exists()):
        ctx.state.record_unchanged(url, lastmod)
        stats["unchanged"] += 1
        return "unchanged"
    dup_of = ctx.hash_index.get(h)
    if dup_of and dup_of != url:
        ctx.state.record_status(url, name, "duplicate", lastmod, res.http_status, res.mode, f"duplicate_of:{dup_of}")
        _drop_old(ctx, url, "duplicate")
        stats["duplicate_content"] += 1
        return "duplicate"
    rel = f"{name}/{url_to_filename(url)}.md"
    meta = {"url": url, "source": name, "title": ex.title, "description": ex.description,
            "published": ex.published, "modified": ex.modified, "sitemap_lastmod": lastmod, "content_hash": h,
            "render_mode": res.mode, "word_count": len(ex.markdown.split()),
            "discovered_by": sorted((found_by or {}).get(url, [])) or ["seed/link/custom"], "scraped_at": now_iso()}
    save_document(ctx.data_dir, rel, build_document(ex, url), meta)
    ctx.state.record_ok(url, name, h, lastmod, rel, ex.title, res.http_status, res.mode)
    outcome = "updated" if (row and row.get("status") == "ok") else "new"
    ctx.known[url] = {"url": url, "source": name, "status": "ok", "content_hash": h, "lastmod": lastmod,
                      "local_path": rel, "last_checked": now_iso(), "fail_count": 0}
    ctx.hash_index[h] = url
    stats[outcome] += 1
    ctx.log.debug(f"  [{outcome.upper()}] {url} ({ex.text_len} chars)")
    return outcome


def _progress(name, handled, queued, s, t0):
    rate = handled / max(time.time() - t0, 1) * 60
    return (f"[{name}] handled={handled} queued={queued} new={s['new']} updated={s['updated']} "
            f"unchanged={s['unchanged']} skip_lastmod={s['skipped_lastmod']} skip_known_bad={s['skipped_known_bad']} "
            f"thin={s['thin']} failed={s['failed']} gone={s['gone']} redirects={s['redirects']} "
            f"dup={s['duplicate_content']} | {rate:.0f}/min")


def crawl_website(site, ctx):
    name, log, stats = site["name"], ctx.log, Counter()
    found, found_by, report = discover_all(site, ctx)
    log.info(f"[{name}] discovery: " + ", ".join(f"{m}={n}" for m, n in report.items())
             + f" -> {len(found)} unique URLs in scope")
    lastmods = dict(found)
    seeds = [n for n in (normalize_url(s, site.get("keep_query", False))
                         for s in [site["base_url"]] + list(site.get("start_urls") or []))
             if n and urlparse(n).netloc in site_domains(site) and not is_blocked_url(n)]
    pending, queued = deque((u, site_accepts(site, u)) for u in seeds), set(seeds)
    for u, _ in sorted(found.items(), key=lambda kv: (kv[1] is not None, kv[1] or ""), reverse=True):
        if u not in queued:
            queued.add(u)
            pending.append((u, True))

    render = site.get("render", "static")
    batch_size = 10 if render == "playwright" else max(int(site.get("workers", 4)) * 4, 16)
    caps = [c for c in (int(site.get("max_pages") or 0), ctx.limit) if c]
    cap = min(caps) if caps else 0
    bfs, bfs_max, bfs_new = bool(site.get("bfs", True)), int(site.get("bfs_max_new", 5000)), {}
    max_consec, consec = int(site.get("max_consecutive_errors", 30)), 0
    handled, next_report, t0 = 0, 100, time.time()
    try:
        while pending:
            if cap and handled >= cap:
                log.info(f"[{name}] page cap reached ({cap})")
                break
            work = []
            while pending and len(work) < batch_size and not (cap and handled >= cap):
                url, save = pending.popleft()
                if url in ctx.seen_run:
                    continue
                ctx.seen_run.add(url)
                handled += 1
                reason = should_skip(url, lastmods.get(url), ctx, site) if save else ""
                if reason == "lastmod":
                    ctx.state.record_unchanged(url, lastmods.get(url))
                    stats["skipped_lastmod"] += 1
                elif reason:
                    stats["skipped_known_bad"] += 1
                elif not ctx.fetcher.allowed(url, site):
                    stats["robots_disallowed"] += 1
                else:
                    work.append((url, save))
            if not work:
                continue
            for res in _run_batch(work, site, ctx):
                outcome = handle_result(res, site, ctx, lastmods, stats, found_by)
                consec = consec + 1 if (outcome == "failed" and res.error not in NON_BREAKER) else (
                    0 if outcome != "failed" else consec)
                if consec >= max_consec:
                    log.error(f"[{name}] {consec} consecutive failures (last: {res.error}) - aborting site. "
                              f"Debug with: --mode inspect --url {res.url}")
                    return stats
                if bfs and res.links and len(bfs_new) < bfs_max:
                    for link in res.links:
                        n = normalize_url(link, site.get("keep_query", False))
                        if n and n not in queued and site_accepts(site, n):
                            queued.add(n)
                            pending.append((n, True))
                            bfs_new[n] = None
                            found_by.setdefault(n, set()).add("bfs")
                            stats["bfs_discovered"] += 1
                            if len(bfs_new) >= bfs_max:
                                break
            ctx.state.commit()
            if handled >= next_report:
                log.info(_progress(name, handled, len(pending), stats, t0))
                next_report += 100
    finally:
        if bfs_new:
            ctx.state.add_discovered(name, "bfs", bfs_new)
        ctx.state.commit()
    log.info(_progress(name, handled, len(pending), stats, t0))
    return stats
