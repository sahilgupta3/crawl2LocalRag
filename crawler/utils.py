"""
utils.py - shared helpers.
  normalize_url ........... one canonical form per page, so every discovery method de-duplicates
  is_blocked_url .......... never fetch media, binaries, archives or office files (text-only crawler)
  is_foreign_language_url . language decided from the URL only (subdomain, path, file name, query)
  md5 / url_to_filename ... change detection and stable, readable file names
  save_document ........... atomic write of <name>.md + <name>.meta.json
"""
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

BLOCKED_EXTENSIONS = {
    ".mp4", ".mp3", ".avi", ".mov", ".wmv", ".flv", ".wav", ".ogg", ".webm", ".m4a", ".m4v", ".mkv", ".aac", ".flac",
    ".exe", ".msi", ".dmg", ".pkg", ".deb", ".rpm", ".bin", ".iso", ".apk", ".jar", ".whl",
    ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar",
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".bmp", ".ico", ".webp", ".tif", ".tiff", ".avif",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".csv", ".json", ".xml", ".yaml", ".yml",
    ".css", ".js", ".map", ".woff", ".woff2", ".ttf", ".eot", ".otf",
}
TRACKING_PARAMS = {"gclid", "fbclid", "mkt_tok", "_hsenc", "_hsmi", "ref", "cmp", "campaign"}


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def age_days(iso):
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds() / 86400
    except (TypeError, ValueError):
        return float("inf")


def md5(text):
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def normalize_url(url, keep_query=False):
    """https, lowercase host, no default port, no fragment, no trailing slash, no (tracking) query.
    Path case is preserved - some sites are case-sensitive."""
    try:
        p = urlparse(url.strip())
    except ValueError:
        return ""
    if p.scheme not in ("http", "https") or not p.netloc:
        return ""
    netloc = p.netloc.lower()
    for port in (":443", ":80"):
        if netloc.endswith(port):
            netloc = netloc[:-len(port)]
    path = re.sub(r"/{2,}", "/", p.path) or "/"
    if len(path) > 1:
        path = path.rstrip("/") or "/"
    query = ""
    if keep_query and p.query:
        pairs = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                 if not (k.lower().startswith("utm_") or k.lower() in TRACKING_PARAMS)]
        query = urlencode(sorted(pairs))
    return urlunparse(("https", netloc, path, "", query, ""))


def is_blocked_url(url):
    return os.path.splitext(urlparse(url).path.lower())[1] in BLOCKED_EXTENSIONS


def slugify(text, max_len=110):
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-._")
    return (s or "index")[:max_len]


def url_to_filename(url):
    """Readable + unique + deterministic: the same URL always maps to the same file."""
    p = urlparse(url)
    raw = re.sub(r"\.html?$", "", p.path.strip("/")) or "index"
    raw = raw.replace("/", "__") + (("__" + p.query) if p.query else "")
    return f"{slugify(raw)}-{hashlib.md5(url.encode()).hexdigest()[:8]}"


def atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def save_document(data_dir, rel_path, markdown, metadata):
    p = Path(data_dir) / rel_path
    atomic_write(p, markdown)
    atomic_write(p.with_suffix(".meta.json"), json.dumps(metadata, indent=2, ensure_ascii=False))
    return p


# ---------------------------------------------------------------- language (URL only)
LANG_CODES = {"en", "de", "fr", "ja", "zh", "ko", "es", "pt", "it", "nl", "ru", "pl", "sv", "tr", "cs", "da", "fi",
              "nb", "hu", "ar", "th", "vi", "uk", "ro", "el", "bg", "hr", "sk", "sl", "he", "id", "hi", "bn", "fa", "ms"}
COUNTRY_TO_LANG = {"jp": "ja", "kr": "ko", "cn": "zh", "tw": "zh", "br": "pt", "cz": "cs", "dk": "da", "gr": "el"}
LANG_QUERY_KEYS = {"lang", "language", "locale", "hl", "lc", "culture"}
_LOCALE_RE = re.compile(r"^([a-z]{2})(?:[-_]([a-z]{2}|hans|hant))?$", re.I)
_REGION_SEG_RE = re.compile(r"^[a-z]{2}[-_]([a-z]{2}|hans|hant)$", re.I)
_LANG_FILE_RE = re.compile(r"[._]([a-z]{2}(?:[-_][a-z]{2,4})?)\.(md|markdown|rst|txt)$", re.I)


def _lang_of(token):
    m = _LOCALE_RE.match(token or "")
    if not m:
        return None
    code = COUNTRY_TO_LANG.get(m.group(1).lower(), m.group(1).lower())
    return code if code in LANG_CODES else None


def url_languages(url):
    try:
        p = urlparse(url)
    except ValueError:
        return []
    found = []
    labels = p.netloc.lower().split(":")[0].split(".")
    if len(labels) >= 3:
        found.append(_lang_of(labels[0]))
    segs = [s for s in p.path.split("/") if s]
    for i, seg in enumerate(segs):
        if i == 0 or _REGION_SEG_RE.match(seg):      # bare "ja" only as first segment; "ja_jp" anywhere
            found.append(_lang_of(seg))
    if segs:
        m = _LANG_FILE_RE.search(segs[-1])
        if m:
            found.append(_lang_of(m.group(1)))
    for k, v in parse_qsl(p.query, keep_blank_values=True):
        if k.lower() in LANG_QUERY_KEYS:
            found.append(_lang_of(v))
    return [x for x in found if x]


def is_foreign_language_url(url, allowed=("en",)):
    allowed = {a.lower() for a in (allowed or ["en"])}
    return any(x not in allowed for x in url_languages(url))
