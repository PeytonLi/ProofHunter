# Verification report

| Gate | Result | Errors | Warnings |
|---|---|---|---|
| production_frozen | PASS | 0 | 0 |
| schemas_frozen | PASS | 0 | 0 |
| schema:ProtocolV1:protocol-v1.json | PASS | 0 | 0 |
| schema:SpendPolicy:spend-policy.json | PASS | 0 | 0 |
| schema:CaseManifest | PASS | 0 | 0 |
| schema:GeneratorExample | PASS | 0 | 0 |
| schema:LabelFile | PASS | 0 | 0 |
| schema:LedgerEntry | PASS | 0 | 0 |
| schema:RankingObservation | PASS | 0 | 0 |
| source_hashes | PASS | 0 | 0 |
| split_leakage | FAIL | 3 | 1 |
| label_provenance | PASS | 0 | 0 |
| test_label_unreachable | PASS | 0 | 0 |
| row_splits:observations | PASS | 0 | 0 |
| observation_provenance | PASS | 0 | 0 |
| row_splits:generator | PASS | 0 | 0 |
| generator_example_provenance | PASS | 0 | 0 |
| cost_ledger | PASS | 0 | 14 |
| teardown_ledger | PASS | 0 | 0 |

## schemas_frozen

```json
{
  "models": 11
}
```

## schema:ProtocolV1:protocol-v1.json

```json
{
  "invalid": 0,
  "rows": 1
}
```

## schema:SpendPolicy:spend-policy.json

```json
{
  "invalid": 0,
  "rows": 1
}
```

## schema:CaseManifest

```json
{
  "invalid": 0,
  "rows": 1
}
```

## schema:GeneratorExample

```json
{
  "invalid": 0,
  "rows": 2300
}
```

## schema:LabelFile

```json
{
  "invalid": 0,
  "rows": 3
}
```

## schema:LedgerEntry

```json
{
  "invalid": 0,
  "rows": 946
}
```

## schema:RankingObservation

```json
{
  "invalid": 0,
  "rows": 36411
}
```

## source_hashes

```json
{
  "sources": 3807
}
```

## split_leakage

```json
{
  "bench_source_overlap": 0,
  "counts": {
    "test": {
      "cases": 650,
      "clean": 150,
      "existing_defect": 58,
      "families": 9,
      "introduced_regression": 442
    },
    "train": {
      "cases": 2333,
      "clean": 333,
      "existing_defect": 205,
      "families": 21,
      "introduced_regression": 1795
    },
    "validation": {
      "cases": 360,
      "clean": 60,
      "existing_defect": 29,
      "families": 6,
      "introduced_regression": 271
    }
  },
  "defect_origin_cross_split": 0,
  "family_id_cross_split": 0,
  "head_source_sha256_cross_split": 0,
  "near_dup_cluster_cross_split": 0,
  "near_duplicate_pairs": 0,
  "normalised_near_duplicate_pairs": 3,
  "template_id_cross_split": 0
}
```
- WARNING below_clean_target: train: 333 < 400
- ERROR near_duplicate_normalised_change: a-1ec6c295a9da6e2521e2 (validation) ~ a-ec3656b89a5898523ddf (test) jaccard=1.0 after comment/whitespace/identifier normalisation
- ERROR near_duplicate_normalised_change: a-361514a4ab44b37d7711 (test) ~ a-63451b5132493288b190 (train) jaccard=1.0 after comment/whitespace/identifier normalisation
- ERROR near_duplicate_normalised_change: a-710323f91a061f997429 (test) ~ a-7db40b3d9a06b65c5971 (train) jaccard=1.0 after comment/whitespace/identifier normalisation

## label_provenance

```json
{
  "counts": {
    "test": {
      "clean": 150,
      "existing_defect": 58,
      "introduced_regression": 442
    },
    "train": {
      "clean": 333,
      "existing_defect": 205,
      "introduced_regression": 1795
    },
    "validation": {
      "clean": 60,
      "existing_defect": 29,
      "introduced_regression": 271
    }
  },
  "unlabelled": 0
}
```

## test_label_unreachable

```json
{
  "files_scanned": 6,
  "hits_by_file": {
    "/home/ubuntu/phase2a/regen3/track_a/generator/train.jsonl": 0,
    "/home/ubuntu/phase2a/regen3/track_a/generator/validation.jsonl": 0,
    "/home/ubuntu/phase2a/regen3/track_a/labels/train.json": 0,
    "/home/ubuntu/phase2a/regen3/track_a/labels/validation.json": 0,
    "/home/ubuntu/phase2a/regen3/track_a/observations/train.jsonl": 0,
    "/home/ubuntu/phase2a/regen3/track_a/observations/validation.jsonl": 0
  },
  "test_tokens": 2089
}
```

## row_splits:observations

```json
{
  "by_manifest_split": {
    "train": 32129,
    "validation": 4282
  },
  "rows": 36411
}
```

## observation_provenance

```json
{
  "cases_with_positive": 1294,
  "relevance": {
    "positive": 3078,
    "unknown": 1203,
    "zero": 32130
  },
  "rows": 36411
}
```

## row_splits:generator

```json
{
  "by_manifest_split": {
    "train": 2000,
    "validation": 300
  },
  "rows": 2300
}
```

## generator_example_provenance

```json
{
  "examples": 2300
}
```

## cost_ledger

```json
{
  "entries": 946,
  "paid_jobs": 946,
  "raw_rows": 0,
  "raw_rows_unknown_cost": 0,
  "unknown_cost_entries": 14,
  "usd_by_category": {
    "model_inference": {
      "estimated": 0.3209116,
      "measured": 3.3090108999999974
    }
  },
  "usd_by_status": {
    "estimated": 0.3209116,
    "measured": 3.3090108999999974
  }
}
```
- WARNING job_cost_unknown (meta:baselines-45a260f6-68cd-4b5c-990d-2111355b25d6): some paid entries have unknown cost
- WARNING job_cost_unknown (meta:baselines-89a5ab49-fa76-4295-a994-193404376772): some paid entries have unknown cost
- WARNING job_cost_unknown (meta:baselines-f9159886-c6cf-497c-a9c7-2e87059f167b): some paid entries have unknown cost
- WARNING job_cost_unknown (meta:baselines-31f8b527-25c8-4d9d-a206-f6bbface33a2): some paid entries have unknown cost
- WARNING job_cost_unknown (meta:baselines-1a242aca-b5f7-44c8-b18a-0fd3075f6ea5): some paid entries have unknown cost
- WARNING job_cost_unknown (meta:baselines-470371f3-25ec-4cce-881f-05de0bd5ea70): some paid entries have unknown cost
- WARNING job_cost_unknown (meta:baselines-c07b0ce7-96b2-472f-9494-7e3e28527f47): some paid entries have unknown cost
- WARNING job_cost_unknown (meta:baselines-0d84b9f7-a76d-4264-be5b-3f4e7995e224): some paid entries have unknown cost
- WARNING job_cost_unknown (meta:baselines-4c19ca96-e0d3-440c-8c59-3120854c0f2c): some paid entries have unknown cost
- WARNING job_cost_unknown (meta:baselines-845f7ce6-5b0f-43fe-8dca-d2419870d2f5): some paid entries have unknown cost
- WARNING job_cost_unknown (meta:baselines-c9094945-b2b1-4be3-b33f-1eb00a488f4a): some paid entries have unknown cost
- WARNING job_cost_unknown (meta:baselines-3333bf2f-a85c-4a90-80c0-61bdfe36330e): some paid entries have unknown cost
- WARNING job_cost_unknown (meta:baselines-396194d9-b9c5-47a4-97ea-36849649a99d): some paid entries have unknown cost
- WARNING job_cost_unknown (meta:baselines-b71d6b2b-b3d3-4d3d-8af6-76c6b92004b5): some paid entries have unknown cost

## teardown_ledger

```json
{
  "paid_providers": [
    "meta",
    "openai"
  ],
  "resources": 0
}
```
