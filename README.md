# RFPScout

An AI agent that finds nonprofit RFPs from across the web, extracts structured details, scores them by usefulness, and optionally drafts outreach emails ready for human review.

Built for the Fuller Focus AI Engineer Intern take-home (RFP track).

---

## Quick Start

The fastest way to see RFPScout work is the demo flag, which runs the full pipeline against fixture files. No API keys needed.

```bash
git clone <this-repo>
cd RFPScout

python3 -m venv venv
source venv/bin/activate          # macOS / Linux
# venv\Scripts\activate           # Windows

pip install -r requirements.txt

python agent.py --demo
```

Expected output: 5 saved RFPs, scores ranging from 56 to 97, exported to `data/rfps.csv` and `data/rfps.json`.

---

## What you'll see

The demo run prints a summary like this:

```
============================================================
RFPScout Run Summary
============================================================
  Search results found:        5
  Fetched successfully:        5
  Extracted as RFPs:           5
  Below inclusion threshold:   0
  After deduplication:         5
  Saved to database:           5 (inserted=5, updated=0, skipped=0)

Top records:
  [ 97] Accountability Lab | website | deadline: June 20, 2026
  [ 97] International Planned Parenthood Federation | website | deadline: May 30, 2026
  [ 71] SeamlessAccess | marketing | deadline: May 15, 2026
  [ 69] Enterprise Community Partners | consulting | deadline: August 28, 2026
  [ 56] City of Willow Springs | website | deadline: July 15, 2026
```

The full output (CSV and JSON) is in `data/rfps.csv` and `data/rfps.json`. A complete CSV example is included in the [Full example output](#full-example-output) section near the bottom.

---

## Live mode (optional)

To run against the real web, you'll need:

- A **Brave Search API key** from [api.search.brave.com](https://api.search.brave.com/app/keys)
  - Free tier: 1,000 queries/month, 1 query/second
  - Requires a card on file (no charge, $1 auth hold drops in a few days)
- A **GitHub Personal Access Token** for GitHub Models (free tier, no credit card required)
  - Get one at [github.com/settings/tokens](https://github.com/settings/tokens) — fine-grained PAT with no special scopes is enough

Copy `.env.example` to `.env` and fill in:

```env
SEARCH_PROVIDER=brave
BRAVE_API_KEY=your-key-here
GITHUB_TOKEN=your-token-here
```

Then run:

```bash
# Specific sector + service
python agent.py --sector education --service website --pages 1

# Interactive (prompts for sector + service)
python agent.py
```

Each run uses 6 search queries (one per query template). With `--pages 1` that's 6 Brave queries; with `--pages 2` it's up to 12.

For Gmail draft integration, see the [Drafts](#drafts) section below.

---

## What's covered by the assignment

The assignment asks for eight sections. They're all here, in order.

### 1. Problem Statement

Nonprofits publish RFPs across thousands of disconnected websites — their own CMSes, government portals, aggregator sites — and many use PDF rather than HTML, making them hard to find. Agencies that want this work spend hours per week manually searching, and the most useful signals (a fresh RFP with a named contact and a real budget) get buried in noise (expired RFPs, news articles about RFPs, "how to write an RFP" guides).

RFPScout solves the discovery half of that problem. It searches systematically, extracts the fields an agency actually cares about, ranks the results, and optionally drafts outreach emails so the human can review drafts in Gmail rather than write outreach from scratch.

### 2. Value

For an agency BD person, RFPScout collapses a multi-hour task into a single CLI command. Concretely:

- **Speed.** The live test during development took ~3 minutes for 35 search results — most of that is HTTP fetching.
- **Triage.** The 0-100 confidence score surfaces fresh, well-specified RFPs first.
- **Action-ready output.** CSV/JSON exports plug into Salesforce, HubSpot, or a spreadsheet workflow. Drafts in Gmail mean the rep's next click is "send."
- **Coverage.** Search + LLM generalises across any nonprofit CMS, no per-site scrapers.

### 3. Why This Approach

I considered three directions before picking this one.

- **Bespoke scrapers per nonprofit website.** Highest extraction quality but doesn't scale — every new nonprofit means new code. Wrong fit for a V1.
- **Pre-aggregated RFP databases (RFPdb, BidNet, etc.).** Fastest to build, but agencies already use these, and the most valuable RFPs (small/medium nonprofits) often skip the aggregators entirely.
- **Search + LLM extraction.** Generalises across any nonprofit's site, catches RFPs the aggregators miss. Trade-off: per-page LLM cost and slower than scrapers, but the cost is small (~$0.0001 per page with `gpt-4o-mini`) and the agent runs in the background.

The third option also has the best path to V2: the same architecture supports more sectors, more services, more search providers, and richer extraction without rewriting the core.

### 4. MVP

V1 is a CLI agent that runs the full pipeline end-to-end:

1. Build search queries from a `(sector, service)` pair using six query templates
2. Send those queries to Brave Search
3. Fetch each result URL (HTML or PDF)
4. Send the cleaned text to a small LLM (`gpt-4o-mini`) for structured field extraction
5. Score each extracted record 0-100 using four weighted signals
6. Deduplicate near-duplicates by URL hash and fuzzy org-name matching
7. Persist everything to SQLite, export CSV/JSON
8. Optionally generate outreach drafts via a stronger LLM (`gpt-4o`) and save them to Gmail's Drafts folder

There's a `--demo` flag that runs the entire pipeline against fixtures, so a reviewer with no API keys can see real output in seconds.

### 5. Methodology

The pipeline is sequential, single-threaded, and modular. Each step is its own file with a narrow responsibility:

```
query_builder.py  →  searcher.py  →  fetcher.py  →  extractor.py  →
scorer.py  →  deduper.py  →  storage.py  →  writer.py  →  drafter.py
```

**`query_builder.py`** — Six query templates per `(sector, service)` pair, mixing broad keywords, `site:.org` restrictions, `filetype:pdf` for formal RFPs, deadline phrases ("proposals due"), and `sam.gov` targeting. Aliases let `"marketing comms"` and `"comms"` both resolve to the canonical `marketing` service.

**`searcher.py`** — Brave Search API client. Header-based auth, exponential backoff on 429/5xx, URL deduplication across queries. Provider abstraction means swapping Brave for another search API only touches this file.

**`fetcher.py`** — HTTP/PDF downloader. Detects PDFs via Content-Type or URL path. Strips noise tags (nav, header, footer, script, style, form) from HTML. Truncates to 8000 chars to control LLM input size. Never raises — failures return an error dict so the agent skips the URL and continues.

**`extractor.py`** — LLM extraction. Sends cleaned text to `gpt-4o-mini` with a strict JSON-only system prompt. Validates controlled vocabulary fields (`org_type`, `service_type`), filters anti-scrape email placeholders (`[email protected]`, "spambots" obfuscation), and falls back to a sentinel record on parse failure. Temperature 0 for determinism. Distinguishes "this isn't an RFP" (`{"not_rfp": true}`) from "the page is unreadable" (parse error).

**`scorer.py`** — Four-component score, max 100:

| Component | Max | Logic |
|---|---|---|
| Field completeness | 40 | Linear with 7 tracked fields filled |
| Deadline usefulness | 30 | Tiered: 0 (past/<14 days), 30 (15-60), 15 (61-120), 8 (>120), 5 (missing) |
| Budget presence | 20 | Binary on `budget_raw` |
| Source quality | 10 | Direct PDF (10) > direct HTML (7) > aggregator (4) > unknown (2) |

Weights and thresholds are constants — easy to tune.

**`deduper.py`** — Two-pass merge. First pass groups by `rfp_id` (SHA-256 of normalised URL) for exact matches. Second pass uses `rapidfuzz.token_set_ratio ≥ 85` plus matching `service_type` to catch suffix variations like `"ABC Foundation"` vs `"ABC Foundation Inc."`. Higher confidence score wins on conflict; `sources_json` lists are union-merged.

**`storage.py`** — SQLite with WAL mode. Upsert is conditional: a new record only overwrites an existing one if its score is higher (or the existing one was a parse error). This means reruns don't degrade good data. There's also a `runs` table that logs every agent invocation with start/finish timestamps and counts.

**`writer.py`** — Regenerates `rfps.csv` and `rfps.json` from SQLite after every run. CSV uses utf-8-sig for clean Excel opening; JSON pretty-prints with `sources_json` deserialised to a real array (not a string-of-JSON).

**`drafter.py`** — Optional. For records ≥ DRAFT_THRESHOLD (default 60) with a `contact_email`, calls `gpt-4o` with a brief outreach prompt. Appends to `data/email_drafts.json` (never overwrites). With `--drafts gmail`, saves each draft via the Gmail API.

**`gmail_client.py`** — Thin wrapper around Google's Gmail API: OAuth on first run (browser-based), `save_draft()` for everything else. Adapted from a previous outreach project; the send/reply/label features were stripped because RFPScout never sends — only drafts. Drafts wait in Gmail's Drafts folder for a human to review and send.

### 6. Tools & Tech

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.13 | Standard for AI tooling, mature LLM SDKs |
| Search | Brave Search API | Independent index, free tier, single-file adapter |
| LLM (parse) | `gpt-4o-mini` via GitHub Models | Cheap (~$0.0001/page), accurate enough for structured extraction |
| LLM (draft) | `gpt-4o` via GitHub Models | Better tone for outreach, only called for high-confidence records |
| HTTP | `requests` | Stable, plays well with retries |
| PDF | `pdfplumber` | Better text extraction than pypdf for structured documents |
| HTML | `beautifulsoup4` | Standard, easy to strip noise tags |
| Fuzzy match | `rapidfuzz` | C++ backend, ~10x faster than `fuzzywuzzy`, no GPL dependency |
| Storage | SQLite (stdlib) | Zero-setup, ACID; swap to Postgres if scale demands it |
| Drafts | Gmail API | Lets a human review before sending; never auto-sends |
| CLI | `argparse` + `questionary` | Flags for repeatability, prompts for interactive use |

The OpenAI SDK works against any OpenAI-compatible endpoint. GitHub Models (Azure-hosted) is currently free for small-scale use; switching to OpenAI direct, Anthropic, or any other compatible provider only changes `.env`.

### 7. Cost / Scale / Feasibility

**Per-run cost** (one `(sector, service)` pair, `--pages 1`), estimated from published GitHub Models pricing:

- 6 Brave queries (~5-8 seconds with rate limit)
- ~30 fetches (highly variable; 10-15 actual RFPs after `not_rfp` filtering)
- ~30 LLM extractions at `gpt-4o-mini` ≈ $0.003 total
- 0-5 LLM drafts at `gpt-4o` ≈ $0.025 if all candidates qualify

Order of magnitude: **~3 cents per run**, dominated by the optional drafter.

**Scaling**:

- **Brave free tier (1,000 queries/month)** = ~166 runs/month. Fine for a single user. Production would need the paid tier or a queue worker that batches discovery jobs across days to stay under quota.
- **SQLite** comfortably handles the scale of this V1; swap to Postgres if record counts grow into the hundreds of thousands. `storage.py` is the only file that changes.
- **LLM cost** scales linearly with results. At ~$0.003/run, even 1,000 runs/month is only a few dollars.
- **Concurrency**: V1 is single-threaded for simplicity. Async fetches (with rate limit awareness) would cut runtime by ~70%.

**Real-world feasibility**: I ran one live test during development — 35 search results, 9 fetch failures (mostly 403s on aggregator PDFs), 14 `not_rfp` classifications, 12 saved RFPs including 4 with budgets and named contacts at scores ≥ 67. The pipeline ran end-to-end without crashing, and the top-scoring records were genuine, current nonprofit RFPs.

### 8. Limitations

- **Scanned PDFs are skipped.** No OCR in V1. The agent surfaces them as `error="scanned_pdf"` so they're visible for debugging.
- **Anti-scraping blocks some sources.** Several nonprofit aggregators 403'd the agent on PDF endpoints despite a browser-like User-Agent. In the single live test, this hit roughly a quarter of the PDF URLs.
- **Currency conversion is not real.** Budgets in non-USD currencies are stored faithfully in `budget_raw`, but `budget_min_usd` / `budget_max_usd` rely on the LLM's training-data sense of FX rates — fine for ballpark scoring, not real numbers.
- **Expired deadlines aren't auto-excluded.** They score 0 for the deadline component, but pass the inclusion threshold on completeness. Triage by sorting CSV by `deadline_iso` (when the user wants only fresh ones).
- **`deadline_iso` is currently null in exports.** The schema field exists but isn't populated; `dateutil.parse(deadline_raw, fuzzy=True)` would do it but I prioritised the drafter for V1.
- **Non-public RFPs require outbound channels.** See [Considerations for non-public RFPs](#considerations-for-non-public-rfps) below.
- **Brave free tier rate limit (1 req/sec)** caps per-run speed. The bottleneck is search, not LLM.
- **LLM can hallucinate edge cases.** Mitigated by temperature 0, controlled vocabulary validation, the `not_rfp` flag, and the `parse_error` fallback.
- **Six query templates is a small surface.** The agent finds RFPs that match common 2026-style search phrasings. Older or less standard phrasings may slip through.

---

## Trade-offs

A few choices I'd flag explicitly because they affect anyone reading the code:

- **Synchronous over async.** Every step in the pipeline waits on the previous one. Async fetching and LLM calls would speed up live runs by ~70%, but it adds complexity around rate limits, error aggregation, and partial failures. Wrong call for V1; right call for V2.
- **SQLite over Postgres.** Zero setup is worth more than horizontal scale at this stage. The schema is small enough that migrating to Postgres is a one-day job.
- **One LLM call per page.** I considered batching multiple pages into a single LLM call to save tokens, but each call has its own retry logic and the per-page isolation makes failures easier to debug.
- **Six query templates, hardcoded.** A configurable templating system (YAML-driven, Jinja-based) would let non-engineers add patterns. Worth the refactor in V1.5.
- **Drafts via Gmail, not SMTP.** Gmail's Drafts folder is a built-in human review step. SMTP send-on-create would be faster but removes the safety net.
- **Score weights as constants, not env vars.** A real product would let agencies tune their own weights (some want urgent RFPs, others long-lead). Easy refactor, deferred.

---

## Considerations for non-public RFPs

The assignment explicitly asks how an agent could find RFPs that aren't published publicly. RFPScout V1 doesn't address this, but here's how I'd approach it:

1. **Direct nonprofit outreach.** Build a sister agent that emails 501(c)(3)s on a watchlist asking "are you currently planning any vendor procurement?". A simple form-link in the email lets nonprofits respond in 60 seconds. This is essentially a contact-data agent (the assignment's other track) layered on top of RFPScout's discovery output.

2. **LinkedIn signal mining.** Nonprofits often hint at upcoming procurement on LinkedIn ("excited to begin our website refresh") weeks before the RFP drops. A LinkedIn-aware agent could surface these as "warm leads" and prep an agency for the formal RFP.

3. **Job postings as a leading indicator.** When a nonprofit posts a "marketing director" or "head of digital" job, an RFP is often 3-6 months out. Scraping job boards (Indeed, NTEN, etc.) and cross-referencing nonprofit names against an agency's prospect list catches these signals early.

4. **Foundation funding announcements.** When a foundation announces a multi-year grant to a nonprofit (Gates, MacArthur, Robert Wood Johnson), procurement follows. Tracking these announcements gives agencies advance notice.

5. **Vendor relationship intelligence.** Many nonprofits change website agencies every 4-6 years. Tracking which agency built which nonprofit's current website (visible via "site by X" footers, GitHub commits, etc.) and surfacing nonprofits whose sites are 4+ years old gives agencies a non-public signal.

The common thread: non-public RFPs become "public" if you watch the right adjacent signals. RFPScout's architecture (search + extract + score + draft) extends naturally to any of these once V1 is live.

---

## What I'd improve next

In rough priority order, weighted by impact-to-effort.

### High impact

1. **OCR for scanned PDFs.** Many older nonprofit RFPs are image-only PDFs. Adding `pytesseract` as a fallback when `pdfplumber` returns empty text would recover ~10-20% of currently-skipped RFPs. Medium effort (~1 day), high yield.

2. **Async fetching.** The biggest single performance win. Live runs spend most of their time waiting on HTTP. `aiohttp` + a semaphore for politeness would drop a typical run from ~3 min to ~30 sec. Costs: more complex error handling, rate limit awareness across coroutines.

3. **`deadline_iso` derivation in `storage.py`.** Right now exports show `deadline_raw: "April 21, 2026 at 5:00 PM CT"` and `deadline_iso: null`. Parsing the raw string with `dateutil.parser.parse(fuzzy=True)` and storing the ISO version would let users filter exports by date — critical for triage. Half-day job.

4. **Auto-exclude expired RFPs by default.** Currently, an expired RFP with full details still passes the inclusion threshold. Adding `expired_at < today` as an exclusion (with a `--include-expired` flag for completeness) would clean up the default output significantly. Trivial change once `deadline_iso` is populated.

5. **Provider fallback chain.** Search APIs change pricing and availability often (Google CSE just demonstrated this). Wrapping `searcher.py` in a chain of providers (Brave → Tavily → SerpAPI) with automatic failover would harden the agent against any one provider going down. ~1 day.

### Medium impact

6. **Configurable scoring weights.** Move the four weights, deadline tier thresholds, and aggregator domain list from `scorer.py` constants to `config.py` env vars. Lets users tune per-customer without touching code. A real product would A/B-test these per-agency.

7. **YAML-driven query templates.** The six templates in `query_builder.py` are hardcoded. A `queries.yaml` with `{sector}` / `{service}` / `{year}` placeholders would let non-engineers add patterns. Especially valuable for sector-specific queries (faith-based RFPs are phrased differently than environmental ones).

8. **Headless browser for JS-heavy pages.** Sites that require JavaScript to render (or use Cloudflare anti-bot) currently 403 the agent. Falling back to Playwright when `requests` fails would recover another ~5-10% of URLs. Costs: container size, speed.

9. **Prompt evals.** The extractor prompt was written by hand and tested manually. Building a small eval suite (golden dataset of 20 known RFPs, measure recall and precision per field) would let me iterate on the prompt safely. Especially valuable for `org_type` (currently produces too many `unknown`s).

10. **Real currency conversion.** Pin a daily FX snapshot from `exchangerate.host` and convert in `extractor.py`. Removes the LLM-estimated USD figures from the schema entirely. ~2 hours.

### Lower impact / nice-to-have

11. **Token usage accounting.** Log per-call token counts to the `runs` table so cost reporting is from real data, not OpenAI's published price.

12. **Shared LLM client.** `extractor.py` and `drafter.py` each build their own OpenAI client. A single `llm_client.py` with connection pooling and unified retry/circuit-breaker logic would clean this up.

13. **Personalised draft tone.** Pass an agency's voice profile (formal, casual, mission-aligned) into `OUTREACH_SYSTEM_PROMPT` so drafts sound like the agency, not generic. ~1 day with good examples.

14. **Slack / HubSpot / Salesforce integrations.** New high-confidence RFPs auto-post to Slack, sync to HubSpot pipelines, or create Salesforce leads. Out of V1 scope but would close the BD loop.

15. **Web UI dashboard.** A small Flask/FastAPI dashboard showing the SQLite contents, run history, and a "approve and send" button for drafts. Useful once the CLI loop is too slow for daily review.

16. **Remaining unit tests.** Scoring and dedup are covered (`tests/test_scorer.py`, `tests/test_deduper.py`). The other modules are integration-tested via the live demo, but proper unit tests for `query_builder.py`, `extractor.py` (fallback paths), `storage.py`, and `writer.py` would catch regressions earlier.

17. **More sectors and services.** Currently 8 sectors and 8 service types. Adding sub-categories (`marketing → email marketing`, `consulting → strategy vs ops`) would give agencies finer targeting.

---

## Drafts

Drafts are off by default. With `--drafts local`, the agent generates draft email bodies and writes them to `data/email_drafts.json`. With `--drafts gmail`, drafts also save to your Gmail Drafts folder (where a human can review and click "send").

For Gmail integration:

1. Create an OAuth client at [Google Cloud Console](https://console.cloud.google.com/apis/credentials) (Desktop app type)
2. Download the JSON, save as `credentials.json` at the project root
3. Run: `python agent.py --sector education --service website --drafts gmail`
4. First run opens a browser for consent. Subsequent runs use `token.json`.

The agent never sends; only drafts. To use the drafter without going through the full pipeline:

```bash
python -m tests.test_drafter
```

This pulls existing draft candidates from SQLite and runs only the drafter step. Cheaper for iteration.

---

## Tests

Unit tests live in `tests/`:

```bash
python -m pytest tests/ -v
```

Current coverage: 28 tests, all passing.

- `test_scorer.py` (15 cases): perfect/empty/expired/aggregator records, all deadline tiers, fuzzy date parsing
- `test_deduper.py` (13 cases): both passes, tie-breaking, merge semantics, edge cases

Other modules (extractor, storage, writer, query_builder) are integration-tested via the demo run. Adding proper unit tests for them is in the [What I'd improve](#what-id-improve-next) list.

---

## Full example output

A complete demo CSV for reference. This is what `python agent.py --demo` produces (formatted for readability; the actual file is one row per record).

| score | org_name | service | budget_raw | deadline_raw | source_type | contact |
|---|---|---|---|---|---|---|
| 97 | Accountability Lab | website | $45,000 - $65,000 | June 20, 2026 | html | procurement@accountabilitylab.org |
| 97 | International Planned Parenthood Federation | website | GBP 80,000 - 120,000 | May 30, 2026 | html | communications@ippf.org |
| 71 | SeamlessAccess | marketing | (none) | May 15, 2026 | html | rfp@seamlessaccess.org |
| 69 | Enterprise Community Partners | consulting | $20,000+ | August 28, 2026 | html | (none) |
| 56 | City of Willow Springs | website | (none) | July 15, 2026 | html | clerk@willowsprings.gov |

For the full schema, see `data/rfps.json` after running the demo. For the drafter output schema, see `examples/sample_email_drafts.json`.

---

## File structure

```
RFPScout/
├── agent.py              # CLI entry point, pipeline orchestrator
├── config.py             # Env vars, paths, thresholds
├── query_builder.py      # 6 query templates, sector/service aliases
├── searcher.py           # Brave Search API client + demo fixture loader
├── fetcher.py            # HTTP/PDF downloader, noise-tag stripping
├── extractor.py          # LLM field extraction, validation, fallbacks
├── scorer.py             # 4-component confidence score (0-100)
├── deduper.py            # 2-pass URL + fuzzy org-name dedup
├── storage.py            # SQLite schema, upserts, run audit
├── writer.py             # CSV + JSON exports
├── drafter.py            # Outreach email generation
├── gmail_client.py       # Gmail API: OAuth + save_draft
├── templates.py          # All LLM prompt strings
├── requirements.txt
├── .env.example
│
├── tests/
│   ├── test_scorer.py    # 15 cases
│   ├── test_deduper.py   # 13 cases
│   └── test_drafter.py   # Manual drafter smoke test
│
├── examples/
│   ├── sample_search_results.json   # Demo search fixtures
│   ├── sample_rfp_texts.json        # Demo extraction fixtures
│   └── sample_email_drafts.json     # Draft schema example
│
└── data/                 # Created at runtime; gitignored, not committed
    ├── rfps.db           # SQLite database
    ├── rfps.csv          # Latest export
    ├── rfps.json         # Latest export (with deserialised arrays)
    └── email_drafts.json # Accumulated draft history
```

---

## License & contact

Built as a take-home assessment. Code is mine; feedback and questions welcome.
