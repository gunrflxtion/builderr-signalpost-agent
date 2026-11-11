# Signalpost agent — Builderr $2,500 Norwegian company research challenge

Deterministic company-research agent built on Builderr's reference agent
(`signalpost-starter-kit`), extended with free external connectors and
deterministic (template-based, no-LLM) synthesis.

- **One terminal JSON envelope per input organisation number. Zero silent drops.**
- **Precision-first:** exact-entity match only. Parent, brand, franchise and
  similarly named companies are NOT exact matches — when in doubt the agent
  returns `ambiguous` / `not_available`, never a wrong-company fact.
- **No models, no API keys, $0 third-party cost.** No LLM anywhere in the
  pipeline; synthesis is deterministic templates over structured facts.
- **Every claim carries** `source_url` + `retrieved_at` + `content_sha256`.

## Install

Requires Python 3.12+ and [`uv`](https://docs.astral.sh/uv/).

```bash
uv sync
```

That installs the pinned dependencies from `uv.lock`. A clean install in an
empty venv works: `uv sync --frozen` is the first step of `run.sh`.

## One command to run

```bash
bash run.sh --organisations <batch.jsonl> --bulk <brreg-enheter.csv> \
  --run-id <run-id> --expected-count <N> [--out-dir out/<run-id>]
```

Example (100-company smoke test):

```bash
curl -L 'https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv' -o brreg-enheter.csv

uv run python select_entry_batch.py \
  --universe signalpost-universe.jsonl.gz --count 100 \
  --output smoke-companies.jsonl

bash run.sh --organisations smoke-companies.jsonl --bulk brreg-enheter.csv \
  --run-id smoke-001 --expected-count 100 --out-dir out/smoke-001
```

Outputs in `--out-dir`:

| File | Contents |
| --- | --- |
| `final-envelopes.jsonl` | **One terminal envelope per input** (the scored artifact) |
| `final-profiles.jsonl` | Merged company profiles incl. external evidence |
| `run-report.json` | Machine-readable run report (runtime, requests, cost, coverage, validation) |
| `batch-report.json` | Baseline batch report |
| `report-{news,reviews,linkedin-discovery,linkedin-jobs,youtube}.json` | Per-connector reports |
| `profiles.jsonl`, `envelopes.jsonl` | Baseline-only outputs (before merge) |

The run exits 0 only when the merged envelopes validate: exactly
`--expected-count` envelopes, one per input, all states terminal, zero
silent drops. A connector that fails never drops an envelope — its module is
recorded as `source_error` / `not_found` / `not_applicable` and the run
continues.

## Pipeline

1. **Baseline batch** (`scripts/run_competition_batch.py`, evaluator-owned
   contract): registry (bulk snapshot), accounting obligation, live registry,
   financials, roles, group, locations, website — all from official Brønnøysund
   sources.
2. **Free external connectors** (no keys, no accounts, rate-limited):
   - Google News RSS — exact-title mentions, last 2 years.
   - Fagfolkguiden — embedded Google aggregate rating from the exact
     directory page (a missing page is `not_found`, not an error).
   - LinkedIn company discovery — typeahead + domain/location gates; 429s are
     retried with backoff, and the request rate is kept polite (2 workers,
     2.0 s delay).
   - LinkedIn guest jobs — only for verified handles, 1 page.
   - YouTube — exact-name channel search via yt-dlp, only for companies with
     a verified publishable website (identity anchor).
   - Brave Search discovery — runs **only** when `BRAVE_SEARCH_API_KEY` is set
     in the environment; otherwise the module is `not_applicable`, never a
     failure.
3. **Merge + deterministic synthesis** (`scripts/merge_external_observations.py`):
   validates every observation (id, `source_url`, `retrieved_at`,
   64-hex `content_sha256`, `exact_entity`, identity proof); invalid
   observations are rejected and counted, never published. The synthesis is
   templates over structured facts only: what the company does (industry code
   + facts), what changed (annual-accounts comparison), external footprint,
   and explicit unknowns ("not established by this run"). Nothing is invented.

The JBOX bonus site is intentionally skipped (its deadline passed before this
entry was built).

## Models / APIs / licences

- **Models: none.** No LLM, no embeddings, no ML inference in the run path.
  (The kit ships optional Scrapy/Playwright and sentiment extras; they are
  not used by `run.sh`.)
- **APIs / paid services: none.** All sources are free and public; expected
  third-party cost is **$0 per 100-company run** (and $0 per 300-company run).
- **Data licences:** Brønnøysund Register Centre open data (bulk CSV + APIs);
  Google News RSS; Fagfolkguiden public directory pages; logged-out LinkedIn
  guest endpoints; public YouTube search. Each source's terms and rate limits
  are respected; a source that refuses returns `blocked` / `not_available`.
- **Code licence:** MIT (see `LICENSE`).

## Verified results

### Smoke test — 100 companies (`smoke-001`, 11 Nov 2026)

- **100/100 terminal envelopes, validation passed, zero silent drops.**
- Runtime **487 s** (~8 min); **801 requests**; third-party cost **$0**.
- Baseline: 500 requests, p50 3.2 s, p95 3.8 s (live Brønnøysund APIs).
- `pytest`: **125 passed, 5 subtests passed** (105 kit tests + 20 new tests
  for the merge/synthesis and the connector fixes below).
- Refresh-replay sample: `qualification_passed: true`, 2/2 expected changes,
  0 false positives/negatives, precision/recall 1.0.

Module status counts (100 companies):

| Module | available | not_found | not_applicable | source_error |
| --- | --- | --- | --- | --- |
| news | 0 | 100 | 0 | 0 |
| reviews | 0 | 100 | 0 | 0 |
| linkedin_profile | 1 | 98 | 0 | 1 |
| linkedin_jobs | 0 | 1 | 99 | 0 |
| youtube | 0 | 0 | 100 | 0 |
| brave_discovery | 0 | 0 | 100 | 0 |
| synthesis | 100 | 0 | 0 | 0 |

External observations accepted: 1 (LinkedIn profile for AIOTA AS,
org 933787141 — exact legal-name + domain match). Coverage on a random
100-company sample is thin because most sampled companies are tiny (7/100
have any website at all; 0 news mentions under the exact-title gate). The
baseline official modules carry the score on such samples; the connectors
add recall only where an exact external match genuinely exists — by design,
never at the cost of a wrong-company claim.

Two fixes landed after the first smoke run (both covered by new tests):

1. **Fagfolkguiden HTTP 404 is `not_found`, not `source_error`.** A missing
   directory page means "no page exists", not a fetch failure. First run:
   89 `source_error`; after fix: 0.
2. **LinkedIn 429 retry with backoff + gentler rate** (2 workers, 2.0 s
   delay, up to 3 attempts honouring `Retry-After`, 10 s / 30 s backoff).
   First run: 10/100 companies lost to 429s; after fix: 1/100 (BRANDMAKER
   AS, 982942942, still 429 after retries — recorded honestly as
   `source_error`).

Two more fixes landed after the 300-company scale test:

3. **Stub profiles for organisation numbers absent from the frozen bulk
   snapshot.** The scale test found 3/300 orgs that exist in the live
   registry (API returns 200) but are missing from the bulk CSV; the kit
   raised `ValueError` and killed the whole run. The agent now emits a stub
   profile (identity filled from the input batch's own fields) whose
   registry evidence honestly says `not_found`-in-snapshot, while the live
   modules still run — all 3 companies ended `complete` with live registry
   and financials available. Without this fix a single such org in the
   official batch would zero the entire run.
4. **LinkedIn jobs 429 retry** (same backoff policy, in `frozen_fetch`).
   Scale run: 4/7 verified-handle companies errored on jobs typeahead 429s.
   Standalone re-run of the jobs stage on the same 7 handles after the fix:
   **0 errors**, same 1 exact job found (STEINLY HYDRAULIKK AS).

Full report: `smoke-test/smoke-001-run-report.json`.

### Scale test — 300 companies (`scale-300`, 11 Nov 2026)

- **300/300 terminal envelopes, validation passed, zero silent drops.**
- Runtime **1611 s** (~27 min); **2407 requests**; third-party cost **$0**.
- Baseline: 1500 requests, p50 3.4 s, p95 4.1 s.
- Module status counts (300 companies): news available 1 / not_found 299;
  reviews available 2 / not_found 298; linkedin_profile available 7 /
  not_found 292 / source_error 1; linkedin_jobs not_applicable 293 /
  not_found 3 / source_error 4 (see fix 4 above — re-run of that stage
  alone: 0 errors); youtube / brave_discovery not_applicable 300;
  synthesis available 300.
- Coverage: **9 companies with external observations, 15 accepted
  observations** (news 1, reviews 6, LinkedIn 8), 1 company with
  observations on 2+ platforms.
- Registry snapshot gap: 3 orgs (922564183 RAKNES UTLEIESERVICE AS,
  912653277 HELGELAND SKYSSBÅT AS, 966478071 BRIGANTINA AS) absent from the
  bulk CSV — all 3 got stub profiles, entity state `complete`, live
  registry + financials `complete` (see fix 3 above).
- Full report: `scale-test/scale-300-run-report.json`.

## Known environment notes

- **Website / YouTube in this sandbox:** the kit's website fetcher has an
  SSRF guard that rejects hostnames resolving to non-global IPs. In the
  sandbox where the smoke tests ran, DNS resolves every hostname to CGNAT
  (`198.18.x.x`), so all 7 website fetches were `blocked` and YouTube had 0
  eligible companies. This is an artifact of that sandbox's DNS, not of the
  agent — on a network with normal DNS the same code fetches company sites
  (the guard itself is intentionally left untouched). The official run
  environment is Builderr's, not this sandbox.
- **Envelope shape:** `OUTPUT_CONTRACT.md` describes a claims/evidence/
  changes/errors/operations shape, while the kit's `run_competition_batch.py`
  (described in the kit as the "Evaluator-owned Signalpost batch contract")
  emits `run_id` / `organisation_number` / `state` / `modules` / `profile`.
  This repo keeps the kit's emitted shape and uses the kit's state
  vocabulary (`available` / `not_found` / `not_applicable` / `source_error` /
  `blocked`), with `not_available` = `not_found` and `failed` = `source_error`.

## Tests

```bash
uv run --with pytest pytest -q
```

125 passed, 5 subtests passed. Kit tests untouched; new tests in
`tests/test_signalpost_agent.py` cover observation validation, deterministic
synthesis, financial-change synthesis, unknowns without fabrication, merge
end-to-end, merge determinism, the Fagfolkguiden 404 rule, the LinkedIn
discovery/jobs 429 retries, and the bulk-snapshot stub profiles.

## Layout

- `run.sh` — the one command.
- `scripts/run_competition_batch.py` — baseline batch (kit).
- `scripts/merge_external_observations.py` — merge + deterministic synthesis.
- `scripts/run_google_news_rss_connector.py`,
  `scripts/run_fagfolkguiden_reviews_connector.py`,
  `scripts/discover_linkedin_company_profiles.py`,
  `scripts/run_linkedin_guest_jobs_connector.py`,
  `scripts/run_youtube_search_connector.py`,
  `scripts/run_brave_discovery.py` — free connectors.
- `src/norway_company_agent/` — kit library (registry, official APIs,
  website fetch + identity gate, evidence, batch).
- `docs/` — kit docs (competition control loop, external connectors,
  Norway sources, PoC design, architecture options).
- `tests/` — kit tests + new agent tests.
- `smoke-test/`, `scale-test/` — run reports from the verified runs above.
