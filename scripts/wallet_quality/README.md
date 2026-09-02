# wallet_quality pipeline (step-1 research gate)

Spec: `docs/research/2026-09-02-wallet-quality-cluster-spec.md`. Read-only
public data; no keys in files; all `data/wallet_quality/` gitignored; wallet
addresses pseudonymized (sha256+salt, first 10 hex).

Env vars (export before any step):

```bash
export HELIUS_API_KEY=<helius dev key>      # required, fail-closed without it
export WQ_PSEUDO_SALT=<stable per-study value>  # constant or pseudos drift
export CRYPTO_QUANT_NO_PROXY=1              # on Grok node; leave unset on the Mac
```

Run order (each step is resumable / idempotent where noted):

```bash
cd scripts/wallet_quality
python3 build_universe.py            # fallback-A top-100 -> data/wallet_quality/universe.json
python3 collect_wallet_txs.py        # 90d raw tx dumps (resumable; --force to redo)
python3 derive_buys.py               # buys.jsonl (no size floor, honest nulls)
python3 cluster_analysis.py          # clusters.csv (E1/E2/E3) + shadow_candidates.jsonl
python3 collect_price_tracks.py      # GT candles; per-mint graceful skip
python3 m1_metrics.py                # stratified delayed returns + coverage
python3 simulate_confirmations.py    # M4/M4b/M4c replay grid
```

Interpretation guardrails:

- `m1_report.md` prints coverage counts. If `dropped_no_curve` is large and
  `exact_curve_share=0`, the medians are fill-derived only — do NOT treat a
  negative median as a strategy verdict yet; land GT candles (works on the
  Grok node; GT egress from the Mac is flaky) and rerun.
- Shadow candidates never add L1 votes; they extend the collection surface.
- `complete=False` on a wallet dump means it hit `max_pages_per_wallet`;
  raise it and rerun with `--force` for that wallet before trusting its M1
  tail.
- Tests: `python3 -m unittest tests.test_wallet_quality` (pure logic, no net).
