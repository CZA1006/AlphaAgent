# Productization P0 — Work Order

> Self-contained work order for a coding agent (Codex).  Read
> [`../AGENTS.md`](../AGENTS.md) first — it is the persistent project
> contract and every rule in it applies to this work.  This document
> defines **what** to build, **in what order**, and **what "done"
> means**; implementation details within the stated constraints are the
> implementing agent's choice.

## Context (why this work exists)

AlphaAgent is a self-improving quant-research harness: LLM proposes
factor expressions, a safe DSL compiles them, a deterministic
walk-forward + holdout evaluator scores them, a six-gate judge promotes
or rejects, and every promotion is reproducible via a config-hash
trail.  The research program has established (see
[`PROJECT_STATUS.md`](PROJECT_STATUS.md)) that the harness *measures*
honestly; the product thesis is exactly that honesty: **an agentic
research harness that cannot lie to you about backtests.**

An architecture review (2026-07-15) found the core product-grade
(typed contracts, static boundary audit, fail-closed writes,
reproducibility trails, ~830 tests) but **single-tenant and
market-hardcoded**: HK-IPO and one GCP project are baked into layers
that claim to be generic.  P0 makes the harness market-agnostic,
configurable, CI-guarded, and callable as a typed SDK — the
prerequisites for any product form (local-first workbench or hosted).

## Invariants (violating any of these fails the whole work order)

1. **Do not change statistical semantics.**  Evaluators, gates,
   thresholds, Bonferroni scaling, holdout construction, and trail
   hashing must produce byte-identical results for existing
   configurations.  Refactors are moves, not edits.  If a hash input
   unavoidably changes shape, bump the relevant schema version
   explicitly and document it — never silently.
2. **Never rewrite or delete historical artifacts** under
   `artifacts/` (they are gitignored but locally precious).
3. **The two-layer boundary holds**: nothing in `alpha_harness/`
   imports Hermes runtime internals; no network/LLM/subprocess access
   from evaluators.  `make audit` must stay green and should be
   *extended*, not weakened.
4. **`make check-full` green at every commit** (ruff check, mypy,
   audit, unit tests, integration smoke).  New code ships with tests.
5. **No secrets in the repo.**  Keys come from env / `.env`
   (gitignored).  Never print key values in logs.
6. Code, comments, and docs in English.  Small typed modules, Pydantic
   or dataclasses for schemas — per AGENTS.md.
7. Update docs in the same commit as behavior: `PROJECT_STATUS.md`
   (status bullet), `README.md` if the architecture story changes, and
   this file's checkboxes.

## Hardcoding inventory (the debt being paid)

Verified 2026-07-15:

- `alpha_harness/director/research_policy.py` — the *generic* post-run
  policy hardcodes `next_topic_id="hk_ipo_cost_realism_oos"` and
  `"hk_ipo_event_truth_review"` transitions.
- `alpha_harness/director/research_director.py` — `build_hk_ipo_context`
  and HK topic construction live inside the core director module.
- `alpha_harness/data/bigquery_loader.py` — defaults to project
  `bloomberg-database-0629`, dataset `hk_ipo_research`, HK table names;
  `_MICRO_COLUMNS` / `_EVENT_FEATURE_COLUMNS` / `_INTRADAY_COLUMNS`
  are HK-specific field lists in a generically-named loader.
- `alpha_harness/factors/dsl_parser.py` — `ALLOWED_FIELDS` mixes the
  universal OHLCV base with ~40 HK-specific microstructure/event/
  intraday fields.
- `alpha_harness/llm/config.py` — `DEFAULT_MODEL` is
  `anthropic/claude-sonnet-4.6`, which is region-blocked (403) in the
  operating environment; every live run needs a manual override.
- `scripts/sql/*.sql` — `bloomberg-database-0629` literals (note
  `micro_features_intraday_v1.sql` already uses `{{PROJECT}}` /
  `{{DATASET}}` templating via
  `alpha_harness/data/tick_materialization.py` — extend that pattern,
  don't invent a second one).
- `scripts/analysis/*.py`, `scripts/doctor_hk_ipo_*.py`,
  `scripts/autonomous_researcher.py` (`--market hk_ipo` only),
  Makefile `*-hk-ipo` targets — acceptable as market-specific *edges*,
  but they must consume configuration rather than repeat literals.
- 14 files total contain the literal `bloomberg-database-0629`.

---

## Stage 0 — CI (do first; independent; ~half a day)

**Goal:** every push/PR runs the existing quality gates automatically.

- Add `.github/workflows/ci.yml`: checkout → install `uv` → Python
  3.11 → `uv sync --extra dev` → `make check` (lint + typecheck +
  audit + test-unit), and a second job for `make smoke`
  (integration).  Cache the uv environment keyed on `uv.lock`.
- CI must need **no secrets**: the suite already passes without GCP or
  LLM keys (760+ passed / 20 skipped locally).  If any test turns out
  to require credentials, mark it skip-without-env rather than adding
  keys to CI.
- Add a status badge to `README.md`.

**Acceptance:** a PR with a deliberate lint error fails CI; a clean PR
is green; no secrets configured in the workflow.

---

## Stage 1 — MarketPack configuration layer (the core of P0)

**Goal:** all market-specific knowledge lives in versioned, typed
**market packs**; the core harness consumes packs and contains zero
market literals.

### Design constraints

- New module `alpha_harness/markets/`:
  - A Pydantic `MarketPack` model, roughly:
    `market_id`, `display_name`, `universe_file`,
    `data` (loader kind + kwargs: project/dataset/tables for BigQuery,
    base path for parquet), `extra_dsl_fields`
    (name → short description), `mock_presets`,
    `director_topics` (typed `ResearchTopicPlan` inputs),
    `post_run_transitions` (see Stage 2), optional `sql_templates`
    metadata.
  - A registry: `load_market_pack(market_id)` reading from
    `configs/markets/<market_id>.yaml` (or `.json` — pick one, YAML
    preferred for humans; add the parser dependency explicitly).
  - Ship two real packs: `hk_ipo` (moving every value currently
    hardcoded) and `us_equities_daily` (SP-50 parquet, OHLCV only —
    this pack must be *trivial*, proving the base case).
- **DSL fields:** `dsl_parser.ALLOWED_FIELDS` shrinks to the universal
  base (`open, high, low, close, volume, vwap`).  Parsing accepts an
  optional `extra_fields: frozenset[str]` (constructor or function
  parameter) supplied from the active market pack.  Preserve a
  backward-compatible default so existing tests and stored expressions
  keep parsing: a module-level helper that resolves "base + all
  registered packs" is acceptable *for parsing only* — execution
  already fails loudly on missing columns, which stays the safety net.
- **Loader:** `BigQueryEquitiesLoader` takes its project/dataset/table
  names and join column lists from constructor args (it mostly already
  does) — the *defaults* move into the `hk_ipo` pack, not the class.
  `loader_factory.create_equities_loader` gains a
  `market_pack` (or `market_id`) path that wires everything.
- **LLM default model** moves to config with an explicit error message
  listing `OPENROUTER_MODEL` when unset — no silently region-blocked
  default.
- **SQL templates:** replace `bloomberg-database-0629` literals in
  `scripts/sql/*.sql` with the existing `{{PROJECT}}`/`{{DATASET}}`
  convention and render through the existing helper; scripts read
  project/dataset from the pack (env override preserved).
- **Extend the static audit** (`alpha_harness/audit/`): add a check
  that fails the build if `alpha_harness/` outside
  `alpha_harness/markets/` contains the literals `hk_ipo` or
  `bloomberg-database-0629` (regex, source-inspection style, mirroring
  the existing auditors).  This makes the de-hardcoding permanent.

### Reproducibility note

Moving defaults must not change what runs *with the same effective
configuration*: a `validate_strict` run on hk_ipo before and after
this stage must produce the same data fingerprint and regime trail id
for the same inputs.  Add a regression test that pins one known
fingerprint/trail id from a fixed synthetic configuration.

**Acceptance:**
- `rg "hk_ipo|bloomberg-database-0629" alpha_harness --glob '!alpha_harness/markets/**'`
  returns nothing, and the new audit check enforces it.
- `make validate-hk-ipo-events ARGS="--no-write --json"` (offline
  smoke) behaves exactly as before.
- A **third toy pack** (`synthetic_smoke`, pointing at the synthetic
  parquet path used in tests) can be added *in the test suite* with
  zero changes outside `configs/markets/` + fixtures — write this as
  an explicit test: load pack → build loader → parse an expression
  using a pack field → run one evaluation cycle end-to-end with the
  mock LLM.

Implementation note (2026-07-16): the first command also matches the Director
APIs and transition literals that this work order assigns to Stage 2. Stage 1
therefore audits all generic core modules except `markets/` and `director/`;
removing that named exemption is part of the Stage 2 acceptance, not this
stage.

---

## Stage 2 — Director and post-run policy become pack-driven

**Goal:** topic selection and post-run transitions are data, not code.

- `ResearchTopicPlan` construction moves from
  `build_hk_ipo_context` into the `hk_ipo` market pack; the director
  becomes `ResearchDirector.plan(pack, context)`.
- `ResearchPostRunPolicy` transition rules (currently `if promoted →
  replay topic X; if all rejected → audit topic Y`) become a typed
  transition table carried by the pack: e.g.
  `on_promotion → topic_id`, `on_no_promotion → topic_id`,
  `on_data_gap → topic_id`, with the *decision logic* (when to stop,
  budget exhaustion, no-progress detection) staying generic in code.
- `scripts/autonomous_researcher.py` accepts any registered
  `--market`, not just `hk_ipo`.
- Preserve current hk_ipo behavior exactly: the existing unit tests
  for director/policy must pass unmodified (or with mechanical import
  updates only), plus new tests exercising a second pack's transitions.

**Acceptance:** `make autonomous-researcher-hk-ipo` (dry run) output
is unchanged; a `us_equities_daily` dry-run plan works and selects a
topic defined purely in YAML.

---

## Stage 3 — Typed SDK facade + artifact store abstraction

**Goal:** the capabilities currently spread across 20+ CLI scripts are
callable as a typed library; scripts become thin shims.

- New module `alpha_harness/sdk.py` (or `alpha_harness/api/`) exposing
  typed entry points, at minimum:
  - `run_validation(market_id, ValidationRequest) -> ValidationReport`
    (what `scripts/validate_strict.py` does),
  - `combine(market_id, CombinationRequest) -> CombinationReport`
    (what `scripts/combine_factors.py` does),
  - `plan(market_id) -> ResearchDirectorPlan`,
  - `run_autonomous(market_id, AutonomousRunnerConfig) -> AutonomousRunRecord`,
  - `list_reports(...)` / `get_report(...)` over the artifact store.
  Reuse the existing Pydantic models; do not create parallel schemas.
- **ArtifactStore protocol** (`alpha_harness/artifacts/store.py`):
  `write(kind, id, payload)`, `read(kind, id)`, `list(kind)` — with
  `LocalArtifactStore` implementing today's `artifacts/{validations,
  promoted, trails, autonomous_runs, research_tasks}` layout
  byte-compatibly.  All writers/readers route through it (S3/GCS
  implementations are explicitly out of P0 scope; the seam is the
  deliverable).
- Scripts keep their CLIs and output formats (people and docs depend
  on them) but their bodies call the SDK.  Any behavior drift is a
  bug; the integration smoke plus one golden-output test per major
  script guards this.
- **No HTTP server in P0.**  The SDK is the product surface; a FastAPI
  wrapper is P1 and must not be started here.

**Acceptance:** `import alpha_harness.sdk as aa; aa.plan("hk_ipo")`
works from a REPL; all existing CLI invocations byte-match their
previous stdout on the mock/synthetic paths; `make check-full` green.

---

## Stage 4 (optional, P1 — only after Stages 0–3 are merged)

- OpenRouter client: bounded retry with backoff on transport
  timeouts/5xx (a live study lost 1 of 12 cells to a single read
  timeout), and a second provider implementation behind
  `llm/protocol.py` to prove the seam.
- Dockerfile + `docs/GETTING_STARTED.md` for a BYO-keys user.

---

## Suggested commit sequence

One stage = one or more focused commits; never mix stages.  Suggested
messages: `Add GitHub Actions CI`, `Introduce MarketPack registry and
hk_ipo/us_equities packs`, `Move DSL market fields into packs`,
`Route SQL templates through pack config`, `Extend audit: no market
literals in core`, `Make director topics pack-driven`, `Add typed SDK
facade`, `Add ArtifactStore abstraction`.

## Progress checklist

- [x] Stage 0: CI green on GitHub
- [x] Stage 1: MarketPack registry + hk_ipo/us_equities packs
- [x] Stage 1: DSL base/extra field split
- [x] Stage 1: SQL templating + loader defaults from pack
- [x] Stage 1: audit extended + fingerprint regression test
- [x] Stage 2: director topics + post-run transitions pack-driven
- [x] Stage 3: SDK facade + ArtifactStore + scripts as shims
- [x] Docs synced (PROJECT_STATUS, README, this checklist)
