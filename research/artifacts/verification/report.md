# Verification report

| Gate | Result | Errors | Warnings |
|---|---|---|---|
| production_frozen | PASS | 0 | 0 |
| schemas_frozen | PASS | 0 | 0 |
| schema:ProtocolV1:protocol-v1.json | PASS | 0 | 0 |
| schema:SpendPolicy:spend-policy.json | PASS | 0 | 0 |
| split_leakage | FAIL | 1 | 0 |
| cost_ledger | PASS | 0 | 0 |
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

## split_leakage

- ERROR no_manifest: no CaseManifest found

## cost_ledger

```json
{
  "entries": 0,
  "paid_jobs": 0,
  "raw_rows": 0,
  "raw_rows_unknown_cost": 0,
  "unknown_cost_entries": 0,
  "usd_by_category": {},
  "usd_by_status": {}
}
```

## teardown_ledger

```json
{
  "paid_providers": [],
  "resources": 0
}
```
