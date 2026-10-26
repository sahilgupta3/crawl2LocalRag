"""
verify.py - opens EVERY saved .md file and checks the real text per site: verified OK, thin, empty,
bot-blocked, garbled, duplicate text; database vs disk; discovered-but-not-fetched; and lines repeated
in >= 30% of a site's files (= probable leftover boilerplate). Problems go to logs/verify_<time>.csv.
"""
import csv
import hashlib
import random
import re
from collections import Counter, defaultdict
from pathlib import Path

from crawler.extractor import BLOCK_MARKERS, FENCE

HEADER_PREFIXES = ("Source:", "Published:", "Last updated:")
MIN_BODY = 150


def body_of(text):
    lines = text.splitlines()
    i = 1 if lines and lines[0].startswith("# ") else 0
    while i < len(lines) and (not lines[i].strip() or lines[i].startswith(HEADER_PREFIXES)):
        i += 1
    return "\n".join(lines[i:])


def analyse(path):
    try:
        body = body_of(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return {"status": "unreadable", "words": 0, "hash": None, "lines": set()}
    compact = re.sub(r"\s+", "", body)
    chars = len(compact)
    junk = body.count("\ufffd") + sum(1 for c in body if ord(c) < 32 and c not in "\n\r\t")
    if chars == 0:
        status = "empty"
    elif chars < 3000 and any(m in body[:3000].lower() for m in BLOCK_MARKERS):
        status = "blocked"
    elif junk / chars > 0.01:
        status = "garbled"
    elif chars < MIN_BODY:
        status = "thin"
    else:
        status = "ok"
    lines, in_code = set(), False
    for l in body.splitlines():
        s = l.strip()
        if s.startswith(FENCE):
            in_code = not in_code
        elif not in_code and 25 <= len(s) <= 200 and not s.startswith(("#", "|")):
            lines.add(s)
    return {"status": status, "words": len(re.findall(r"\w+", body)),
            "hash": hashlib.md5(compact.encode()).hexdigest() if chars else None, "lines": lines}


def run_verify(state, data_dir, sources=None, samples=0, csv_path=None):
    data_dir = Path(data_dir)
    dirs = sorted(d for d in data_dir.iterdir() if d.is_dir()) if data_dir.exists() else []
    if sources:
        dirs = [d for d in dirs if d.name in set(sources)]
    ok_paths, db_status = defaultdict(set), defaultdict(Counter)
    for r in state.conn.execute("SELECT source, status, local_path FROM pages"):
        db_status[r["source"]][r["status"]] += 1
        if r["status"] == "ok" and r["local_path"]:
            ok_paths[r["source"]].add(r["local_path"])
    discovered = dict(state.conn.execute("SELECT source, COUNT(DISTINCT url) FROM discovered GROUP BY source").fetchall())
    not_fetched = dict(state.conn.execute(
        "SELECT d.source, COUNT(DISTINCT d.url) FROM discovered d LEFT JOIN pages p ON p.url = d.url "
        "WHERE p.url IS NULL GROUP BY d.source").fetchall())
    problems, rows1, rows2, boiler, picks = [], [], [], {}, {}
    for d in dirs:
        src, files = d.name, sorted(d.rglob("*.md"))
        c, seen, freq, present, ok_files, words, size = Counter(), {}, Counter(), set(), [], 0, 0
        for p in files:
            rel = p.relative_to(data_dir).as_posix()
            present.add(rel)
            a = analyse(p)
            c[a["status"]] += 1
            words += a["words"]
            size += p.stat().st_size
            freq.update(a["lines"])
            if a["status"] == "ok":
                ok_files.append(p)
            else:
                problems.append((src, rel, a["status"]))
            if a["hash"] in seen:
                c["dup"] += 1
                problems.append((src, rel, "duplicate_of:" + seen[a["hash"]]))
            elif a["hash"]:
                seen[a["hash"]] = rel
            if rel not in ok_paths.get(src, set()):
                c["orphan"] += 1
                problems.append((src, rel, "file_not_in_db"))
            if not p.with_suffix(".meta.json").exists():
                c["nometa"] += 1
        n = len(files)
        rows1.append((src, n, c["ok"], c["thin"], c["empty"], c["blocked"], c["garbled"] + c["unreadable"], c["dup"],
                      words // max(n, 1), size / 1e6))
        s = db_status.get(src, Counter())
        rows2.append((src, s["ok"], len(ok_paths.get(src, set()) - present), c["orphan"], c["nometa"], s["thin"],
                      s["failed"], s["gone"], s["redirect"], s["duplicate"], discovered.get(src, 0), not_fetched.get(src, 0)))
        if n >= 30:
            boiler[src] = [(l, k) for l, k in freq.most_common(6) if k >= 0.3 * n]
        if samples and ok_files:
            picks[src] = random.sample(ok_files, min(samples, len(ok_files)))

    print("\n=== CONTENT CHECK - every file opened and its text analysed ===")
    print(f"{'SITE':20} {'FILES':>7} {'VERIFIED OK':>14} {'THIN':>5} {'EMPTY':>5} {'BLOCK':>5} {'JUNK':>5} "
          f"{'DUPES':>5} {'AVG_WORDS':>9} {'MB':>7}")
    for r in rows1:
        pct = f"{r[2]} ({100 * r[2] / r[1]:.0f}%)" if r[1] else "0"
        print(f"{r[0]:20} {r[1]:>7} {pct:>14} {r[3]:>5} {r[4]:>5} {r[5]:>5} {r[6]:>5} {r[7]:>5} {r[8]:>9} {r[9]:>7.1f}")
    print("\n=== DATABASE vs DISK + COVERAGE ===")
    print(f"{'SITE':20} {'DB_OK':>7} {'MISSING':>7} {'ORPHAN':>6} {'NOMETA':>6} | {'THIN':>5} {'FAIL':>5} {'GONE':>5} "
          f"{'REDIR':>5} {'DUP':>5} | {'DISCOVERED':>10} {'NOT_FETCHED':>11}")
    for r in rows2:
        print(f"{r[0]:20} {r[1]:>7} {r[2]:>7} {r[3]:>6} {r[4]:>6} | {r[5]:>5} {r[6]:>5} {r[7]:>5} {r[8]:>5} {r[9]:>5} "
              f"| {r[10]:>10} {r[11]:>11}")
    print("  NOT_FETCHED = discovered but not attempted yet (normal after a --limit run)")
    if any(boiler.values()):
        print("\n=== REPEATED LINES (in >= 30% of a site's files -> probable boilerplate, see drop_line_patterns) ===")
        for src, items in boiler.items():
            for line, k in items:
                print(f"  {src:20} {k:>6}x  {line[:110]}")
    for src, files in picks.items():
        print(f"\n=== RANDOM SAMPLES: {src} ===")
        for p in files:
            print(f"--- {p.relative_to(data_dir)}")
            for l in [x for x in body_of(p.read_text(encoding='utf-8')).splitlines() if x.strip()][:5]:
                print(f"    {l[:140]}")
    if csv_path and problems:
        Path(csv_path).parent.mkdir(parents=True, exist_ok=True)
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows([("site", "file", "problem")] + problems)
        print(f"\n{len(problems)} problem entries written to {csv_path}")
    elif not problems:
        print("\nNo problems found.")
