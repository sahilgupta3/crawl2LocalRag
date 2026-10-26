"""
cli.py - command line.
  discover  run every discovery method; per method: URLs found and how many ONLY it found. Saves nothing.
  crawl     incremental crawl of enabled sites (or --site NAME ...)
  urls      crawl only config/custom_urls.txt (or --file)
  inspect   fetch one --url and show what would be extracted. Saves nothing.
  verify    open every saved file and check the real content per site
  stats     database summary
"""
import argparse
import copy
import logging
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import yaml

from crawler.crawl_site import Context, crawl_website, fetch_page, handle_result
from crawler.discovery import ALL_METHODS, discover_all
from crawler.fetcher import HttpFetcher, Renderer
from crawler.filters import site_accepts
from crawler.state import StateDB
from crawler.utils import normalize_url, is_blocked_url, is_foreign_language_url
from crawler.verify import run_verify

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "sites.yaml"
CUSTOM_URLS = ROOT / "config" / "custom_urls.txt"
DATA, LOGS, DB = ROOT / "data", ROOT / "logs", ROOT / "state.db"
MERGE_KEYS = {"exclude_selectors"}


def setup_logging(verbose):
    LOGS.mkdir(parents=True, exist_ok=True)
    log_file = LOGS / f"crawl_{datetime.now():%Y%m%d_%H%M%S}.log"
    log = logging.getLogger("crawl2LocalRag")
    log.setLevel(logging.DEBUG)
    log.handlers.clear()
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG if verbose else logging.INFO)
    ch.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
    log.addHandler(fh)
    log.addHandler(ch)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return log, log_file


def load_config():
    raw = yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}
    defaults, sites = raw.get("defaults") or {}, []
    for s in raw.get("sites") or []:
        merged = copy.deepcopy(defaults)
        for k, v in s.items():
            merged[k] = (list(defaults.get(k) or []) + list(v or [])) if k in MERGE_KEYS else v
        sites.append(merged)
    names = [s["name"] for s in sites]
    if len(names) != len(set(names)):
        raise SystemExit("duplicate site names in config/sites.yaml")
    return defaults, sites


def match_site(url, defaults, sites):
    for s in sites:
        if site_accepts(s, url):
            return s
    host = urlparse(url).netloc
    for s in sites:
        if host in (s.get("allowed_domains") or []):
            return s
    custom = copy.deepcopy(defaults)
    custom.update({"name": "custom", "base_url": url, "allowed_domains": [host]})
    return custom


def run_crawl(ctx, sites, summary):
    for site in sites:
        ctx.log.info("=" * 78)
        ctx.log.info(f"SITE {site['name']} | render={site.get('render', 'static')}")
        t0 = time.time()
        try:
            stats = crawl_website(site, ctx)
        except KeyboardInterrupt:
            raise
        except Exception:
            ctx.log.exception(f"SITE {site['name']} crashed - continuing with the next site")
            stats = Counter(crashed=1)
        ctx.state.commit()
        summary[site["name"]] = dict(stats)
        ctx.log.info(f"SITE {site['name']} finished in {(time.time() - t0) / 60:.1f} min -> {dict(stats)}")


def run_urls(ctx, file_path, defaults, sites, summary):
    urls = [l.strip() for l in file_path.read_text().splitlines() if l.strip() and not l.strip().startswith("#")]
    stats = Counter()
    for raw in urls:
        url = normalize_url(raw, keep_query=True)
        if not url or is_blocked_url(url) or is_foreign_language_url(raw, defaults.get("allowed_languages") or ["en"]):
            ctx.log.info(f"  [SKIP blocked/invalid/other language] {raw}")
            continue
        site = match_site(url, defaults, sites)
        render = site.get("render", "static")
        res = fetch_page(url, site, ctx, render == "playwright", True)
        if render == "auto" and res.needs_render:
            res = fetch_page(url, site, ctx, True, True)
        outcome = handle_result(res, site, ctx, {}, stats, {})
        ctx.log.info(f"  [{outcome.upper()}] {url}" + (f" ({res.error})" if res.error else ""))
        ctx.state.commit()
    summary["custom_urls"] = dict(stats)


def run_inspect(ctx, url, render_override, defaults, sites):
    url = normalize_url(url, keep_query=True)
    site = match_site(url, defaults, sites)
    mode = render_override or site.get("render", "static")
    print(f"\nURL        : {url}\nSite       : {site['name']} (render={mode})")
    print(f"robots.txt : {'allowed' if ctx.fetcher.allowed(url, site) else 'DISALLOWED'}")
    print(f"URL filter : {'accepted' if site_accepts(site, url) else 'REJECTED (language / include / exclude rules)'}")
    res = fetch_page(url, site, ctx, mode == "playwright", True)
    if mode == "auto" and res.needs_render:
        print("Static     : insufficient -> retrying with headless Chromium")
        res = fetch_page(url, site, ctx, True, True)
    print(f"Fetched by : {res.mode} | HTTP {res.http_status} | error: {res.error or '-'} | thin: {res.thin}")
    ex = res.extract
    if not ex:
        print("Nothing extracted.")
        return
    print(f"Title      : {ex.title}\nSelector   : {ex.selector}\nText chars : {ex.text_len}\nLinks      : {len(ex.links)}")
    print(f"VERDICT    : {'WOULD SAVE' if not (res.error or res.thin) else 'WOULD NOT SAVE'}")
    print("-" * 30 + " markdown preview (first 1500 chars) " + "-" * 30)
    print(ex.markdown[:1500])


def run_discover(ctx, sites):
    rows = []
    for site in sites:
        ctx.log.info(f"discovering {site['name']} ...")
        union, found_by, report = discover_all(site, ctx)
        only = Counter(next(iter(ms)) for ms in found_by.values() if len(ms) == 1)
        extra = sum(1 for ms in found_by.values() if not (ms & {"sitemap", "robots"}))
        rows.append((site["name"], report, only, len(union), extra, list(union)[:2]))
    print(f"\n{'SITE':20} {'UNIQUE':>7} {'NOT_IN_SITEMAP':>14}  " + "".join(f"{m:>16}" for m in ALL_METHODS))
    for name, report, only, total, extra, samples in rows:
        cells = "".join(f"{(str(report[m]) + '(+' + str(only[m]) + ')') if m in report else '-':>16}" for m in ALL_METHODS)
        print(f"{name:20} {total:>7} {extra:>14}  {cells}")
        for s in samples:
            print(f"{'':22}e.g. {s}")
    print("\nN(+k): N in-scope URLs found by that method, k of them found by NO other method.")


def run_stats(ctx):
    print(f"\n{'SITE':22} {'STATUS':10} {'COUNT':>8}")
    for r in ctx.state.conn.execute("SELECT source, status, COUNT(*) c FROM pages GROUP BY 1, 2 ORDER BY 1, 2"):
        print(f"{r['source']:22} {r['status']:10} {r['c']:>8}")
    files = list(DATA.rglob("*.md")) if DATA.exists() else []
    print(f"\nMarkdown files in data/: {len(files)} | {sum(p.stat().st_size for p in files) / 1e6:.1f} MB")


def main():
    p = argparse.ArgumentParser(description="crawl2LocalRag - incremental, text-only documentation crawler")
    p.add_argument("--mode", choices=["discover", "crawl", "urls", "inspect", "verify", "stats"], default="crawl")
    p.add_argument("--site", nargs="+", help="only these site names (also works for disabled sites)")
    p.add_argument("--file", default=str(CUSTOM_URLS), help="URL list for --mode urls")
    p.add_argument("--url", help="single URL for --mode inspect")
    p.add_argument("--render", choices=["static", "playwright", "auto"], help="override render mode (inspect)")
    p.add_argument("--methods", nargs="+", choices=ALL_METHODS, help="override discovery methods")
    p.add_argument("--refresh-archives", action="store_true", help="re-query Wayback / Common Crawl now")
    p.add_argument("--limit", type=int, default=0, help="max pages per site (testing)")
    p.add_argument("--force", action="store_true", help="ignore lastmod / hash shortcuts, re-save everything")
    p.add_argument("--samples", type=int, default=0, help="verify: print N random files per site")
    p.add_argument("--verbose", action="store_true", help="per-page lines on screen")
    a = p.parse_args()

    log, log_file = setup_logging(a.verbose)
    defaults, all_sites = load_config()
    if a.site:
        bad = [n for n in a.site if n not in {s["name"] for s in all_sites}]
        if bad:
            sys.exit(f"unknown site(s): {bad}")
        sites = [s for s in all_sites if s["name"] in a.site]
    else:
        sites = [s for s in all_sites if s.get("enabled", True)]
    state = StateDB(DB)
    known = state.load_all()
    ctx = Context(state=state, fetcher=HttpFetcher(defaults.get("user_agent", "crawl2LocalRag")), renderer=Renderer(),
                  data_dir=DATA, log=log, force=a.force, limit=a.limit, methods=a.methods,
                  refresh_archives=a.refresh_archives, known=known,
                  hash_index={r["content_hash"]: u for u, r in known.items() if r.get("status") == "ok" and r.get("content_hash")})
    log.info(f"mode={a.mode} | sites={[s['name'] for s in sites]} | log: {log_file.relative_to(ROOT)}")
    run_id, summary = None, {}
    try:
        if a.mode == "stats":
            run_stats(ctx)
        elif a.mode == "verify":
            run_verify(state, DATA, sources=a.site, samples=a.samples,
                       csv_path=LOGS / f"verify_{datetime.now():%Y%m%d_%H%M%S}.csv")
        elif a.mode == "discover":
            run_discover(ctx, sites)
        elif a.mode == "inspect":
            if not a.url:
                sys.exit("--mode inspect needs --url")
            run_inspect(ctx, a.url, a.render, defaults, all_sites)
        else:
            run_id = state.start_run(a.mode, [s["name"] for s in sites])
            if a.mode == "crawl":
                run_crawl(ctx, sites, summary)
            else:
                run_urls(ctx, Path(a.file), defaults, all_sites, summary)
    except KeyboardInterrupt:
        log.warning("Interrupted - progress saved. Re-run the same command to resume.")
        summary["interrupted"] = True
    finally:
        state.commit()
        if run_id:
            state.finish_run(run_id, summary)
        ctx.renderer.close()
        state.close()
