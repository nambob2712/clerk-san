# Clerk-san eval fixtures

Layout consumed by `eval/run_eval.py`:

```
eval/fixtures/
├── receipts/            # independently generated synthetic images (jpg/png)
│   ├── 0001.jpg
│   └── 0001.json        # label (same schema as synthetic labels)
├── pdfs/                # incl. at least one hybrid text+scan PDF and utility bills
├── docs/                # md / docx / xlsx fixtures for adapter tests
├── synthetic/           # optional output of eval/synthetic.py (git-ignored, regenerated)
├── queries.json         # retrieval + routing eval set
└── ../results/baseline.json  # selected D3 EvalReport — the regression gate
```

Label JSON schema (per document):

```json
{
  "class": "receipt",
  "transaction_date": "2026-06-01",
  "total_amount": 1234,
  "tax_rate_lines": [{"rate": 10, "amount": 112}],
  "counterparty": "セブン-イレブン",
  "registration_number": "T1234567890123",
  "currency": "JPY"
}
```

`queries.json` entries: `{"question": "...", "gold_document": "receipts/0001",
"expected_route": "sql|semantic|hybrid"}`.

Only independently generated, non-personal fixtures may be committed. Real or private
documents, plus any anonymized, redacted, or otherwise derived versions of them, must stay
in ignored local paths and must never be committed.

## Safe deterministic fixtures

`python -m eval.run_eval` works on a clean checkout: when neither `--fixtures` nor
`--generate-synthetic` is supplied, it generates eight deterministic, non-personal
receipts in a temporary directory, verifies their image links/checksums, reports
fixture integrity, and removes that directory afterwards. It does **not** claim an
extraction accuracy result unless `--predictions` is supplied.

To keep a reproducible local corpus for a separate benchmark, use an ignored path:

```bash
python -m eval.run_eval --generate-synthetic eval/fixtures/synthetic --fixture-count 50
```

## Expense-document application smoke corpus

`eval.expense_documents` creates a separate 12-file, non-personal corpus for exercising
the complete application workflow. It covers receipts, one-off bills, and recurring
electricity, water, and gas bills across English, Japanese, and Vietnamese, with the
nine canonical expense kinds represented in `manifest.json`.

```bash
.venv/bin/python -m eval.expense_documents --out .clerksan-expense-fixtures
```

The output path is ignored by Git. Replacing a populated fixture directory requires an
explicit `--reset`; the files are visibly marked `CLERK-SAN SYNTHETIC - NON-PERSONAL`.
The corpus is suitable for local API/worker smoke tests and human-review exercises. It is
not a substitute for owner-controlled, ignored local real-world evaluation data.

## Prediction and R2 query evidence

An extraction prediction file is a JSON array (or `{"predictions": [...]}`) aligned
with the fixture labels:

```bash
python -m eval.run_eval --fixtures /path/to/fixtures --predictions predictions.json
```

For retrieval/routing evidence, pass `--query-results` and choose the cutoff with
`--k`. Each array entry can carry either or both evidence types:

```json
{
  "gold_document": "receipts/0001",
  "retrieved_documents": ["receipts/0007", "receipts/0001"],
  "expected_route": "hybrid",
  "actual_route": "hybrid"
}
```

```bash
python -m eval.run_eval --query-results query-results.json --k 3
```

The resulting report has `retrieval_hit_rate_at_k`, `k`, and `routing_accuracy` only
when real query evidence is present. A baseline may gate extraction and/or query
metrics, but cannot be used for fixture integrity alone.

## Extraction model baseline gate

The extraction benchmark writes the selected candidate's real `EvalReport` field
counts, not echoed labels. That artifact is compatible with `eval.run_eval --baseline`.

On the first run, when no accepted baseline exists, omit `--baseline` and create one:

```bash
python -m eval.benchmark_extraction_models \
  --out eval/results/extraction-model-decision.json \
  --baseline-out eval/results/baseline.json
```

On every repeat run, provide the existing baseline as input as well as the output path:

```bash
python -m eval.benchmark_extraction_models \
  --baseline eval/results/baseline.json \
  --out eval/results/extraction-model-decision.json \
  --baseline-out eval/results/baseline.json
```

The repeat command compares the selected candidate with the input baseline before it
writes either path. If any field falls by more than two percentage points, it exits
with status 1 and leaves both the decision report and baseline unchanged.
