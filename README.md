# EvidenceDelta

**Change-data capture and CI regression gates for live search evidence.**

An answer pipeline can fail because its sources disappeared, even when its prompt and model stayed the same. EvidenceDelta versions the evidence returned by SerpApi, separates ranking movement from snippet changes, and produces a deterministic diff that CI can gate on. The local workbench explains each failure; an exported bundle reproduces the comparison offline.

[Watch the narrated local demo (90 seconds)](https://github.com/amrrs/evidence-delta/releases/tag/demo-v1).

Built for the SerpApi India Hackathon 2026 **Open-Source Integrations** track. MIT licensed, Python 3.9+, zero runtime dependencies. No LLM or embedding API is needed.

## Run locally

```sh
git clone https://github.com/amrrs/evidence-delta.git
cd evidence-delta
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
cp .env.example .env
# Edit .env and set SERPAPI_API_KEY.
evidencedelta serve
```

Open **http://127.0.0.1:8767**. Capture two observations of the same query, or capture once and run the clearly labeled **controlled replay**. The workbench includes source-rank alignment, per-depth overlap, before/after snippets, optional identity hypotheses, policy controls, provenance, ledger verification, and bundle export. Command-K opens a keyboard-accessible command palette.

The server binds to loopback. API keys stay in the server environment; snapshots redact the key and credential fields. The UI uses optional Google Fonts and remains usable with local fallback fonts.

## CLI and CI integration

```sh
evidencedelta capture "India semiconductor fabrication policy"
# After the observation interval, or explicitly bypass both caches:
evidencedelta capture "India semiconductor fabrication policy" --force
evidencedelta history
evidencedelta compare 1 2 --gate --max-content 0 --max-removed 0 --min-rbo 0.8
evidencedelta export > bundle.json
evidencedelta verify --bundle bundle.json
evidencedelta compare 1 2 --bundle bundle.json --strict --gate
```

All commands return JSON. Exit **0** means success / gate pass, **1** means a valid comparison failed policy, and **2** means an invalid operation, incomplete capture, or integrity failure. `compare` operates locally; the bundle path supports offline CI without any API credential. Preserve the database or export between runs. Use the actual snapshot IDs printed by capture/history, rather than assuming IDs 1 and 2.

Gate thresholds apply **per engine**. Defaults fail on any changed normalized title/snippet or removed source. Rank-only movement is separately counted; minimum RBO can gate ranking instability. Added sources and URL-alias hypotheses are reported but do not independently fail the default gate. An empty ranking has no usable RBO and fails the gate.

## Technical design

```text
SerpApi Google + Google News (parallel observations)
        │ raw response + provider search ID + timestamps + latency
        ▼
SQLite WAL: immutable-by-convention snapshots + transactional SHA-256 ledger
        │ query / engines / country / language / depth must agree
        ▼
Canonical URL matches → eligible same-host title pairs → exact DP assignment
        │ independently classified evidence / source / ranking changes
        ▼
Shared-depth extrapolated RBO + JSON report → policy → CI exit code
        └ workbench + complete offline bundle + integrity verifier
```

**Identity matching.** Normalize HTTP(S), host casing, `www`, trailing slashes, fragments, and known tracking parameters; preserve semantic query parameters. Deduplicate canonical URLs, then cap each engine at ten valid unique results. Unmatched results may use same-host title-token Jaccard ≥ 0.65. These are explicit hypotheses, disabled with `--strict`. The residual assignment maximizes total eligible similarity with a memoized bitmask dynamic program, **O(n·m·2^m)** for n,m ≤ 10. Partial matching permits unmatched sources; greedy matching can lose a globally better pair.

**Change classification.** A canonical JSON SHA-256 fingerprint of case-folded, whitespace-normalized title + snippet detects content changes. Each matched source gets one primary category: content, URL alias, rank-only, or unchanged; rank movement is also retained as an independent flag. Added / removed sources are separate. This measures **search-returned text**, not article edits, truth, or source relevance. Google News often returns titles without snippets; title changes still participate.

**Ranking metric.** Let A_d be the overlap fraction of the top-d prefixes. At shared depth k:

```text
RBO_ext = (1 − p) · Σ[d=1..k] A_d · p^(d−1) + A_k · p^k
```

Default p=0.9 emphasizes the top of the ranking. The report exposes every A_d, shared depth, and extrapolated tail weight. This uses the equal-depth extrapolation at the smaller observed depth; unequal-length tails are excluded. It is a stability metric, not a quality score. Based on [Webber, Moffat & Zobel, A Similarity Measure for Indefinite Rankings (2010)](https://www.codalism.com/research/papers/wmz10_tois.pdf).

**Integrity and concurrency.** Snapshot insertion, canonical body hash, and ledger-link insertion share a SQLite `BEGIN IMMEDIATE` transaction. Budget reservations also serialize transactionally across processes. Each ledger event commits the snapshot hash and previous event hash. Offline verification checks content, links, sequence, and snapshot/ledger coverage. This is an **unanchored local hash chain**: a full rewrite or removal of a valid suffix can evade it. Save the head hash externally if you need an independent anchor; the tool does not claim a signed, immutable transparency log.

**API controls.** Each uncached capture attempts one Google and one Google News request. A complete same-watch snapshot is reused for 60 seconds, with no new snapshot or API call. `--force` bypasses local TTL and passes `no_cache=true` to SerpApi. A persistent UTC-day cap reserves at most 24 calls by default; failed requests consume reservations conservatively. Partial captures are retained for diagnostics and excluded from baselines. Provider caches and local observation time are exposed separately. Sequential repeat captures may legitimately return no changes.

## Controlled verification

```sh
evidencedelta replay --source 1
# Use the returned before/after IDs:
evidencedelta compare 3 4 --gate
```

Replay copies a complete live snapshot into a **separate controlled-replay watch**. It swaps two ranks, adds a labeled snippet mutation, changes only a tracking parameter, injects a same-host URL-alias hypothesis, and—when enough results exist—replaces the final source with a clearly synthetic `example.invalid` source. It makes zero API calls and cannot become a live baseline. These are counterfactual test injections, not observed historical events.

## Validation

```sh
python3 -m unittest discover -s tests -v
```

20 tests pass locally and in GitHub CI on Python 3.9 and 3.12. [Recorded validation summary](VALIDATION.json) documents a real live pair: ranking overlap stayed 1.000 while one Google snippet changed.

Tests cover adversarial matching that beats greedy, comparison with an independent brute-force optimum, URL semantics, top-weighted RBO, content/rank separation, strict identities, recursive credential redaction, local TTL reuse, partial failures, concurrent budget reservations, controlled-replay isolation, snapshot and malformed-ledger tampering, full exports beyond 100 snapshots, and CLI gate exit codes. Development tests establish implementation behavior, not general identity accuracy or production readiness.

Limitations: no scheduler, remote multi-tenant service, signed external anchoring, relevance labels, page-content fetching, or claim entailment. URL and title matching can merge distinct versions; use strict identity for conservative gates. Locale validation currently accepts alphabetic country/language codes and leaves provider-specific support to SerpApi.

AI disclosure: Codex assisted implementation and validation. The demo uses a fal.ai ElevenLabs synthetic voiceover. Runtime comparison is deterministic and requires no generative model.
