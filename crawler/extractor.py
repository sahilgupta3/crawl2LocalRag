"""
extractor.py - HTML -> clean Markdown (text only).
1. collect links (for link-following) and metadata before stripping anything
2. remove scripts, styles, media, forms, widgets and boilerplate containers
3. pick the main content via the site's CSS selectors (fallback: <body>)
4. Markdown with ATX headings and fenced code blocks kept; no backslash escaping (keeps snake_case intact)
5. drop boilerplate lines (never inside code blocks); flag bot-challenge pages
"""
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urldefrag

from bs4 import BeautifulSoup
from markdownify import markdownify as md

FENCE = "`" * 3
ALWAYS_REMOVE = ("script, style, noscript, template, iframe, frame, object, embed, svg, canvas, img, picture, "
                 "video, audio, track, source, map, button, input, select, textarea, link, meta, dialog")
BLOCK_MARKERS = ("access denied", "request unsuccessful", "attention required", "verify you are human",
                 "are you a robot", "just a moment...", "enable javascript and cookies", "pardon our interruption")
COPY_LABELS = {"CODE", "Copy", "Copy code", "Copied!", "Copy to clipboard"}
DEFAULT_DROP = [re.compile(p, re.I) for p in (
    r"^was this (page|article|topic|information) helpful\??$",
    r"^(share|share this( page| article| post)?|share on \w+)$",
    r"^(link copied\.?|copy link|copied!?)$",
    r"^(print|print this page)$",
    r"^(back to top|scroll to top|return to top)$",
    r"^skip to (main )?content$",
    r"^(subscribe|subscribe now|sign up for (our )?newsletter.*)$",
    r"^(on this page|in this article|table of contents)$",
    r"^(accept( all)? cookies|cookie (settings|preferences))$",
)]


@dataclass
class Extracted:
    title: str = ""
    markdown: str = ""
    text_len: int = 0
    links: list = field(default_factory=list)
    description: str = ""
    published: str = ""
    modified: str = ""
    selector: str = ""
    blocked: bool = False


def _meta(soup, *names):
    for n in names:
        tag = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
        if tag and tag.get("content"):
            return tag["content"].strip()
    return ""


def _code_language(el):
    classes = list(el.get("class") or [])
    code = el.find("code")
    if code is not None:
        classes += list(code.get("class") or [])
    for c in classes:
        for prefix in ("language-", "lang-", "highlight-"):
            if c.startswith(prefix):
                return c[len(prefix):]
    return ""


def _to_markdown(el):
    return md(str(el), heading_style="ATX", bullets="-", strip=["a"], code_language_callback=_code_language,
              escape_misc=False, escape_underscores=False, escape_asterisks=False)


def clean_markdown(text, site=None):
    if site is not None and "_re_drop" not in site:
        site["_re_drop"] = [re.compile(p, re.I) for p in (site.get("drop_line_patterns") or [])]
    extra = site["_re_drop"] if site is not None else []
    lines = [l.rstrip() for l in text.replace("\xa0", " ").splitlines()]
    kept, in_code = [], False
    for i, l in enumerate(lines):
        s = l.strip()
        if s.startswith(FENCE):
            in_code = not in_code
            kept.append(l)
            continue
        if in_code:
            kept.append(l)
            continue
        if s in {"×", "|", "*", "-"}:
            continue
        core = s.lstrip("-*#> ").strip()
        if core and (any(r.search(core) for r in DEFAULT_DROP) or any(r.search(core) for r in extra)):
            continue
        if s in COPY_LABELS and next((n.strip() for n in lines[i + 1:i + 4] if n.strip()), "").startswith(FENCE):
            continue
        kept.append(l)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip() + "\n"


def extract(html, url, site):
    soup = BeautifulSoup(html, "lxml")
    out = Extracted()
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("mailto:", "javascript:", "tel:", "#")):
            continue
        absolute = urldefrag(urljoin(url, href))[0]
        if absolute.startswith(("http://", "https://")) and absolute not in seen:
            seen.add(absolute)
            out.links.append(absolute)

    h1 = soup.find("h1")
    raw_title = ((h1.get_text(" ", strip=True) if h1 else "") or _meta(soup, "og:title")
                 or (soup.title.get_text(" ", strip=True) if soup.title else "") or url)
    out.title = re.sub(r"\s*\|\s*[^|]{1,40}$", "", raw_title).strip() or raw_title
    out.description = _meta(soup, "description", "og:description")
    out.published = _meta(soup, "article:published_time", "datePublished")
    out.modified = _meta(soup, "article:modified_time", "og:updated_time", "dateModified")

    for tag in soup.select(ALWAYS_REMOVE):
        if not tag.decomposed:
            tag.decompose()
    for sel in site.get("exclude_selectors") or []:
        try:
            for tag in soup.select(sel):
                if not tag.decomposed:
                    tag.decompose()
        except Exception:
            continue

    min_sel, parts = min(200, int(site.get("min_text_chars", 300))), []
    for sel in site.get("content_selectors") or []:
        try:
            els = [e for e in soup.select(sel) if len(e.get_text(" ", strip=True)) >= 50]
        except Exception:
            continue
        if not els:
            continue
        if site.get("select_all"):
            ids = {id(e) for e in els}
            top = [e for e in els if not any(id(p) in ids for p in e.parents)]
            if sum(len(e.get_text(" ", strip=True)) for e in top) >= min_sel:
                parts, out.selector = top, f"{sel} (x{len(top)})"
                break
        else:
            best = max(els, key=lambda e: len(e.get_text(" ", strip=True)))
            if len(best.get_text(" ", strip=True)) >= min_sel:
                parts, out.selector = [best], sel
                break
    if not parts:
        parts, out.selector = [soup.body or soup], "body (fallback)"

    texts = [p.get_text(" ", strip=True) for p in parts]
    out.text_len = sum(len(t) for t in texts)
    out.blocked = out.text_len < 3000 and any(m in " ".join(texts)[:3000].lower() for m in BLOCK_MARKERS)
    out.markdown = clean_markdown("\n\n---\n\n".join(m for m in (_to_markdown(p) for p in parts) if m.strip()), site)
    return out
