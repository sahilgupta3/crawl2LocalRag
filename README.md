# crawl2LocalRag

![Python](https://img.shields.io/badge/python-3.11%2B-blue) ![License](https://img.shields.io/badge/license-MIT-green) ![Local-first](https://img.shields.io/badge/runs-100%25%20local-success) ![No API keys](https://img.shields.io/badge/API%20keys-none-informational)

**Build a private, local knowledge base from documentation websites - and ask it questions in plain
language, with cited answers, entirely on your own laptop.**

<!-- KPI:TOP -->
**50 pages crawled · 100% verified readable · 2 discovery methods · 747 code examples preserved · 100% local**
<!-- /KPI:TOP -->

## Why this exists

People in customer- and partner-facing technical roles - solution engineers, consultants, architects,
support engineers - answer questions all day, across many topics. The answers are scattered:

- **public sources**: product documentation, blogs, best-practice guides, community articles
- **their own material**: customer and partner decks, PDFs, Word documents, spreadsheets
- **recordings**: webinars, enablement sessions, demos, customer calls

Finding the right answer means opening ten tabs, digging through folders full of `v1.1`, `v1.2 copy` and
`final_final` files, and trying to remember which recording covered it. It is slow, and answers are
often incomplete.

**Why not a cloud chatbot?**

- It does not know *your* material.
- Confidential, customer-facing content often must not be pasted into public AI services.
- It answers from memory, not from sources - you cannot verify where a statement came from.

## What it does

    public websites ──► discover ──► fetch ──► clean ──► verify ──► Markdown corpus
       (sitemaps, robots.txt,     (HTTP, or     (main text only,    (every file
        feeds, web archives,       headless      no navigation,      opened and
        link-following)            Chromium)     ads or media)       checked)
                                                                         │
    question ──► hybrid search (meaning + exact keywords) ──► re-rank ──► local LLM ──► cited answer

1. **Crawls documentation websites** incrementally. Pages are found through six independent discovery
   methods, so if one misses a page another finds it. Only the main article text is kept, as clean
   Markdown with headings and code blocks intact.
2. **Keeps itself current.** Re-runs skip unchanged pages (sitemap dates, content hashes) and download
   only what is new or changed - weekly updates take minutes.
3. **Answers questions locally** (local RAG, see below): hybrid search, cross-encoder re-ranking and a
   local LLM via Ollama, with numbered citations to the original page. Nothing leaves your machine.

## How it helps

| Situation | Without it | With it |
|---|---|---|
| "How do I configure X?" | Search several sites, read many pages | One question, one answer with sources |
| Several questions in one email | Answer one by one | Paste them all, each answered with citations |
| "Where was this documented?" | Memory and guesswork | A citation that opens the exact page |
| Confidential context | Cannot use AI on it | Runs fully offline on your laptop |
| Keeping knowledge current | Bookmarks go stale | Incremental updates in minutes |

## Extending it: your own documents and recordings

The real value grows when the same knowledge base also contains **your own material**. The design
supports this - **these modules are not included in this repository**, but the pipeline is built so they
plug in as additional sources:

- **Documents** (PowerPoint incl. speaker notes, PDF, Word, Excel): read a folder read-only, convert to
  Markdown, detect duplicates and older versions by text similarity and keep only the newest.
- **Recordings**: transcribe videos locally (e.g. with Whisper), keep a registry of file hashes and audio
  fingerprints so a recording is never transcribed twice, then index the transcripts.

Both stay on your laptop - that is what makes it usable for confidential, customer-facing content.

## Quick start (macOS / Linux)

    ./setup.sh                      # creates .venv and installs dependencies
    ./setup.sh --browser            # optional: headless Chromium for JavaScript-only sites
    ./crawl.sh --mode discover      # dry run: what each discovery method finds
    ./crawl.sh --mode inspect --url https://kubernetes.io/docs/concepts/workloads/controllers/deployment/
    ./crawl.sh --mode crawl         # incremental crawl of all enabled sites
    ./crawl.sh --mode verify        # open every saved file and check the real content

The included demo configuration crawls the English **Kubernetes documentation** (kubernetes.io, CC BY 4.0).

## Commands

| Mode | What it does |
|---|---|
| `discover` | Runs every discovery method, shows how many URLs each found and how many *only* it found. Saves nothing |
| `crawl` | Incremental crawl (`--site NAME`, `--limit N`, `--force`, `--methods ...`, `--refresh-archives`) |
| `urls` | Crawl only the URLs listed in `config/custom_urls.txt` |
| `inspect` | Fetch one `--url` and show exactly what would be extracted. Saves nothing |
| `verify` | Content check per site: verified OK, thin, empty, bot-blocked, duplicates, repeated boilerplate |
| `stats` | Database summary |

## Adding a website

Copy the template block in `config/sites.yaml`, set `base_url`, `sitemaps`, `allowed_domains` and
optionally `include_patterns` / `content_selectors`. No code changes. Test with `discover` and `inspect`,
then `crawl --site NAME --limit 20` and `verify`.

## Design principles

- **Local-first and private** - no cloud services, no API keys.
- **Accuracy over speed** - quality gate (no empty, thin or bot-challenge pages), redirect handling,
  content-hash de-duplication, optional latest-version-only filter for versioned manuals.
- **Incremental by design** - unchanged pages are skipped without downloading.
- **Verified** - `verify` opens every file; lines repeated across many files reveal leftover boilerplate.
- **Config-driven** - sites, filters, selectors and politeness settings live in one YAML file.
- **Responsible crawling** - respects robots.txt, per-domain rate limits, never evades bot protection.

## Lessons learned while building it

1. Probe every site first: HTTP 200 does not mean content - JavaScript apps return an empty shell.
2. A browser-style `Accept` header combined with a bot User-Agent can trigger web-firewall blocks.
3. Versioned documentation multiplies page counts - keep only the newest version per manual.
4. The same text often lives under several URLs - de-duplicate by content hash, store redirects once.
5. Parallelise by website, never split one site across processes (rate limits and de-duplication are per process).
6. Public web archives are useful but flaky (503/504) - back off, never block, cache results.
7. Verify by reading the files, not by trusting counters.

## Results

<!-- KPI:RESULTS -->
| KPI | Value |
|---|---|
| Unique in-scope URLs discovered | 1,097 |
| Discovery methods | 2 independent - pages found by only one method: sitemap 1,058, bfs 39 |
| Pages saved as clean Markdown | 50 |
| Verified readable (every file opened and checked) | 100.0% |
| Duplicates removed / redirects collapsed / thin pages rejected | 0 / 1 / 0 |
| Corpus | 133,825 words, 1.0 MB, 747 code examples preserved |
| First full crawl | 0 min, 109 pages/min at a polite 2 requests/sec |
| Incremental re-run | 0.8 min - 0% of pages skipped without re-processing, 0 new/updated |

*Measured on 2026-10-26 - demo corpus: kubernetes_docs - regenerate with `python kpi.py`.*
<!-- /KPI:RESULTS -->

In real use on a 16 GB laptop, the same engine handled ~33,000 pages across seven documentation sites, 100% verified readable, with weekly incremental updates taking minutes.

## Responsible use

You are responsible for complying with the terms of use and licences of the sites you crawl.
Respect robots.txt and keep request rates low. Do not redistribute crawled content.

## License

MIT - see LICENSE.
