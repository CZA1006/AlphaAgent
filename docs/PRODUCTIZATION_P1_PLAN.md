# Productization P1 — Work Order

> Self-contained work order for a coding agent (Codex).  Read
> [`../AGENTS.md`](../AGENTS.md) and
> [`PRODUCTIZATION_P0_PLAN.md`](PRODUCTIZATION_P0_PLAN.md) first — P1
> builds directly on the P0 foundation (MarketPack registry, typed
> `alpha_harness.sdk`, `ArtifactStore`, CI).  Every P0 invariant still
> applies.  This document defines **what** to build, **in what order**,
> and **what "done" means**; implementation choices within the stated
> constraints are the implementing agent's.

## Context (why this work exists)

P0 made the harness market-agnostic, configurable, CI-guarded, and
callable as a typed SDK.  The research program has since resolved every
alpha hypothesis to a **null** across two markets under a hardened,
pre-registered methodology (see [`PROJECT_STATUS.md`](PROJECT_STATUS.md)).
**The product is therefore not a discovered edge — it is the
instrument: a research harness that cannot lie to you about backtests.**
P1 makes that instrument *usable and shareable* by a second person: a
readable report surface that renders the honesty (gates, trails,
fingerprints) already captured in artifacts, resilient LLM plumbing, and
a one-command local setup.

## Invariants (violating any fails the work order)

1. **The renderer must never recompute a number.**  The HTML report
   layer is **read-only over `ArtifactStore`**: every value it displays
   (IC, rank-IC, gate verdicts, thresholds, fingerprints, trail ids,
   costs) is copied verbatim from the stored artifact JSON.  It never
   calls an evaluator, re-parses a factor, or re-derives a metric.  A
   UI that could disagree with the artifact would defeat the entire
   product thesis.
2. **Do not change statistical semantics** — no edits to evaluators,
   gates, thresholds, hashing, or the artifact JSON schemas.  P1 adds
   *views* and *plumbing*, not new truth.
3. **Two-layer boundary holds**; `make audit` stays green (extend, not
   weaken).  The renderer lives in `alpha_harness/reports/` and must
   not import Hermes internals or reach the network.
4. **`make check-full` green at every commit.**  New code ships with
   tests.  Narrow diffs — no mass reformatting (the tree is
   format-clean and CI enforces it).
5. **No secrets in the repo**; keys from env/`.env`.  Never log key
   values.
6. English; small typed modules; Pydantic/dataclasses for schemas.
7. Update docs in the same commit as behavior (`PROJECT_STATUS.md`
   bullet, `README.md` if the story changes, this checklist).
8. **No hosted multi-tenant server, no auth, no database in P1.**  The
   report is generated as a self-contained static HTML file; a live
   server is P2 and must not be started here.

---

## Stage 1 — Self-contained HTML report renderer (highest value)

**Goal:** turn the JSON artifacts the harness already writes into a
single, shareable, offline HTML file that makes the honesty legible —
this is the product's face.

### Design constraints

- New module `alpha_harness/reports/html.py` and an SDK entry
  `render_report(kind, artifact_id) -> Path` (plus a thin
  `scripts/render_report.py` CLI and a `make report ARGS=...` target).
- **Reads only through `ArtifactStore`** (P0 Stage 3 seam).  Given a
  `kind` (`validations`, `combinations`, `robustness`,
  `autonomous_runs`) and an id, load the stored JSON and render it.
- **Self-contained output**: one `.html` file with inlined CSS (and JS
  only if genuinely needed).  **No external CDN, font, or script
  references** — it must open offline and be safe to email.  Default
  output under `artifacts/reports/<id>.html`.
- **Minimum viable views** (start with the two that carry the thesis):
  - *Validation report*: the candidate table with each factor's
    expression, decision (PROMOTE/REFINE/REJECT), the **gate that fired**
    on rejection, IC / rank-IC / holdout numbers, plus a header block
    showing the data fingerprint, regime trail id, cost, and the
    schema-v6 Bonferroni family size / multiplier.  The rejection
    reason is the point — make it prominent.
  - *Robustness study*: the predeclared grid, the per-arm and pooled
    tally (Y2 rank-IC positive / N, sign-test p, strict clears), and
    the contamination/overlap caveats carried in the record.
  - (Combination and autonomous-run views are a nice-to-have; ship them
    only if they reuse the same rendering primitives cheaply.)
- Rendering is deterministic: same artifact → byte-identical HTML
  (sort keys, fixed number formatting — reuse the P0 golden-output
  float normalization convention).

### Acceptance

- `make report ARGS="--kind validations --id prereg-hk128-p1-full-window-c01"`
  writes a self-contained HTML file.
- A test asserts that every numeric value rendered for a fixture
  artifact appears **verbatim** in the source JSON (grep the rendered
  HTML for the JSON's IC/rank-IC/threshold strings) — proving invariant
  1 (the renderer copies, never recomputes).
- A test asserts the output contains **no external URLs**
  (`http(s)://` outside of inlined data), proving self-containment.
- Deterministic-output golden test (same artifact → same HTML bytes).

---

## Stage 2 — LLM provider resilience

**Goal:** a live study never loses a cell to a transient error, and the
provider seam is proven by a second implementation.

### Design constraints

- In the OpenRouter client: **bounded retry with exponential backoff**
  on transport timeouts and 5xx / 429 (the 2026-07-15 SP-50 study lost
  1 of 12 cells to a single read timeout).  Retries are capped, logged,
  and surfaced in the run record (e.g. `retries_used`), and must
  respect the existing token/cost budget — a retry cannot exceed the
  declared USD cap.
- A **second provider** implementation behind the existing
  `llm/protocol.py` interface (e.g. a direct Anthropic or a generic
  OpenAI-compatible client), wired so `--llm` / config can select it.
  The point is to prove the seam is real, not to endorse a model.
- Deterministic offline tests only — mock the transport; **no live API
  calls in the test suite** (CI has no keys).  Test the retry/backoff
  state machine and budget-respecting cutoff with a fake client that
  raises then succeeds.

### Acceptance

- Unit test: a fake transport that times out N−1 times then succeeds
  yields one successful call with `retries_used == N−1`; a fake that
  always fails stops at the cap and fails closed without exceeding the
  budget.
- The second provider passes the same protocol-conformance test as the
  OpenRouter client.
- `make check-full` green; no live calls in CI.

---

## Stage 3 — Docker + Getting Started

**Goal:** a new user with their own keys runs a mock-LLM cycle and
renders a report in one setup, no host Python needed.

### Design constraints

- A `Dockerfile` (and optional `compose` service) that installs via
  `uv sync`, runs as non-root, and bakes **no secrets** — keys arrive
  at runtime via env/`.env`.
- `docs/GETTING_STARTED.md`: clone → build → run a mock-LLM validation
  (no keys) → render its HTML report → (optional) add
  `OPENROUTER_API_KEY` + `OPENROUTER_MODEL` for a live run.  Document
  the region-blocked default-model gotcha (must set `OPENROUTER_MODEL`).
- The image must run the existing integration smoke (`make smoke`)
  successfully.

### Acceptance

- `docker build` succeeds; the container runs `make smoke` green.
- A reader following `GETTING_STARTED.md` reaches a rendered HTML
  report using only the mock LLM (no keys).
- No secret material in the image or repo.

---

## Explicitly out of scope for P1 (defer to P2)

Hosted/multi-tenant server, auth, databases, S3/GCS `ArtifactStore`
backends, scheduling/cron autonomy, and any real-money or brokerage
integration.  P1 is local-first and single-user.

## Suggested commit sequence

One stage = one or more focused commits; never mix stages.  Suggested
messages: `Add self-contained HTML report renderer`, `Render robustness
study reports`, `Add bounded LLM retry with backoff`, `Add second LLM
provider behind the protocol`, `Add Dockerfile and getting-started
guide`.

## Progress checklist

- [ ] Stage 1: HTML renderer (validation + robustness views), read-only
      over ArtifactStore, self-contained, verbatim + no-external-URL +
      determinism tests
- [ ] Stage 2: bounded LLM retry/backoff within budget + second
      provider behind the protocol, offline-tested
- [ ] Stage 3: Dockerfile runs `make smoke` green + GETTING_STARTED.md
      mock-LLM → rendered report path
- [ ] Docs synced (PROJECT_STATUS, README, this checklist)
