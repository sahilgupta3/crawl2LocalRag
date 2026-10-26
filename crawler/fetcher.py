"""
fetcher.py - network layer.
HttpFetcher  pooled session with retries + backoff, size limit, non-HTML guard, per-domain rate limit
             shared by all threads, robots.txt. Sends only User-Agent + Accept-Language: some web firewalls
             block a browser-style Accept header combined with a bot User-Agent.
Renderer     optional headless Chromium (Playwright) for JavaScript-only sites. Images, media and fonts are
             blocked. Main thread only. Install with: ./setup.sh --browser
"""
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROBOTS_AGENT = "crawl2LocalRag"
BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0.0.0 Safari/537.36 crawl2LocalRag")


@dataclass
class FetchResult:
    url: str
    status: int = 0
    content: bytes = b""
    text: str = ""
    content_type: str = ""
    final_url: str = ""
    error: str = ""


class DomainRateLimiter:
    """Spaces requests to one domain across all threads (interval = 1 / requests_per_second)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._next = {}

    def wait(self, domain, rps):
        if not rps or float(rps) <= 0:
            return
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next.get(domain, 0.0))
            self._next[domain] = slot + 1.0 / float(rps)
        if slot > now:
            time.sleep(slot - now)


class HttpFetcher:
    def __init__(self, user_agent, timeout=25, max_bytes=8 * 1024 * 1024):
        self.timeout, self.max_bytes = timeout, max_bytes
        self.limiter = DomainRateLimiter()
        self._robots, self._robots_lock = {}, threading.Lock()
        self.session = requests.Session()
        retry = Retry(total=3, connect=3, read=2, backoff_factor=1.5, status_forcelist=(429, 500, 502, 503, 504),
                      allowed_methods=frozenset(["GET", "HEAD"]), respect_retry_after_header=True,
                      raise_on_status=False)
        adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=32)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers.update({"User-Agent": user_agent, "Accept-Language": "en-US,en;q=0.9"})

    def throttle(self, url, rps):
        self.limiter.wait(urlparse(url).netloc, rps)

    def get(self, url, rps=None, expect_html=True, max_bytes=None):
        self.throttle(url, rps)
        limit = max_bytes or self.max_bytes
        try:
            resp = self.session.get(url, timeout=self.timeout, stream=True, allow_redirects=True)
        except requests.RequestException as e:
            return FetchResult(url, error=f"request_error:{type(e).__name__}")
        try:
            ctype = resp.headers.get("Content-Type", "").lower()
            res = FetchResult(url, status=resp.status_code, content_type=ctype, final_url=resp.url)
            if expect_html and resp.status_code == 200 and ctype and "html" not in ctype:
                res.error = "not_html"
                return res
            length = resp.headers.get("Content-Length", "")
            if length.isdigit() and int(length) > limit:
                res.error = "too_large"
                return res
            buf = bytearray()
            for chunk in resp.iter_content(chunk_size=65536):
                buf.extend(chunk)
                if len(buf) > limit:
                    res.error = "too_large"
                    return res
            res.content = bytes(buf)
            return res
        except requests.RequestException as e:
            return FetchResult(url, status=resp.status_code, error=f"read_error:{type(e).__name__}")
        finally:
            resp.close()

    def allowed(self, url, site):
        if not site.get("respect_robots", True):
            return True
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        with self._robots_lock:
            rp = self._robots.get(base, "missing")
        if rp == "missing":
            rp = self._load_robots(base)
            with self._robots_lock:
                self._robots[base] = rp
        return True if rp is None else rp.can_fetch(ROBOTS_AGENT, url)

    def _load_robots(self, base):
        res = self.get(base + "/robots.txt", expect_html=False, max_bytes=2 * 1024 * 1024)
        if res.error or res.status != 200:
            return None
        rp = RobotFileParser()
        rp.parse(res.content.decode("utf-8", errors="replace").splitlines())
        rp.modified()
        return rp


class Renderer:
    """Lazy-started headless Chromium. Restarted every N pages to keep memory flat."""

    BLOCKED = {"image", "media", "font"}

    def __init__(self, timeout_ms=45000, idle_ms=12000, restart_every=300):
        self.timeout_ms, self.idle_ms, self.restart_every = timeout_ms, idle_ms, restart_every
        self._pw = self._browser = self._context = None
        self._pages = 0

    def _start(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise RuntimeError("Playwright is not installed - run ./setup.sh --browser")
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True, args=["--disable-dev-shm-usage", "--no-sandbox"])
        self._context = self._browser.new_context(user_agent=BROWSER_UA, locale="en-US",
                                                  viewport={"width": 1366, "height": 900})
        self._context.route("**/*", self._route)

    def _route(self, route):
        try:
            route.abort() if route.request.resource_type in self.BLOCKED else route.continue_()
        except Exception:
            pass

    def close(self):
        for obj, method in ((self._context, "close"), (self._browser, "close"), (self._pw, "stop")):
            if obj is not None:
                try:
                    getattr(obj, method)()
                except Exception:
                    pass
        self._pw = self._browser = self._context = None

    def render(self, url, wait_selector=None):
        if self._pages and self._pages % self.restart_every == 0:
            self.close()
        try:
            if self._browser is None:
                self._start()
        except Exception as e:
            self.close()
            return FetchResult(url, error=f"browser_unavailable:{str(e)[:160]}")
        self._pages += 1
        page = None
        try:
            page = self._context.new_page()
            resp = page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            try:
                page.wait_for_load_state("networkidle", timeout=2500 if wait_selector else self.idle_ms)
            except Exception:
                pass
            if wait_selector:
                try:
                    page.wait_for_selector(wait_selector, timeout=10000)
                except Exception:
                    pass
            return FetchResult(url, status=resp.status if resp else 0, text=page.content(),
                               content_type="text/html", final_url=page.url)
        except Exception as e:
            if self._browser is None or not self._browser.is_connected():
                self.close()
            return FetchResult(url, error=f"browser_error:{type(e).__name__}:{str(e)[:150]}")
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass
