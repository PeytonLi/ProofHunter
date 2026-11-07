from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from fixtures.adapters import FixtureRepositoryValidator
from proofhunter import contracts as c
from proofhunter import oracle
from proofhunter.api import create_app
from proofhunter.ports import ApiDependencies
from proofhunter.store import SQLiteStore
from tests.store.test_progress import heartbeat
from tests.store.test_store import OPERATOR, SERVICE, client_for, clock, settings


@pytest.mark.parametrize(
    "tokens", [(None, "operator"), ("service", None), (None, None), ("same", "same")]
)
@pytest.mark.parametrize("path", ["/v1/runs", "/v1/runs/unknown", "/v1/runs/unknown/replays"])
def test_invalid_credentials_fence_all_routes(
    tmp_path: Path, tokens: tuple[str | None, str | None], path: str
) -> None:
    config = settings(tmp_path / "db").model_copy(
        update={
            "api_token": None if tokens[0] is None else SecretStr(tokens[0]),
            "operator_token": None if tokens[1] is None else SecretStr(tokens[1]),
        }
    )
    client = client_for(SQLiteStore(config, clock=clock()))
    for token in ("service", "operator", "same"):
        response = client.post(path, json={}, headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 401
        assert (
            c.ErrorEnvelope.model_validate(response.json()).error.code is c.ReasonCode.UNAUTHORIZED
        )
        assert "Bearer" not in response.text


@pytest.mark.parametrize("mode", ["live", "runner", "checks", "webhook"])
@pytest.mark.parametrize(
    "failure", [None, "store", "fingerprint", "expired", "mismatch", "profile"]
)
def test_live_readiness(tmp_path: Path, mode: str, failure: str | None) -> None:
    config = settings(tmp_path / "db")
    if mode == "live":
        config = config.model_copy(update={"profile": c.Profile.LIVE})
    elif mode == "runner":
        config = config.model_copy(
            update={"daytona": config.daytona.model_copy(update={"enable_live_runner": True})}
        )
    else:
        config = config.model_copy(
            update={"github": config.github.model_copy(update={f"enable_{mode}": True})}
        )
    timer = clock()
    store = SQLiteStore(config, clock=timer)
    beat = heartbeat(store).model_copy(update={"profile": config.profile})
    if failure == "mismatch":
        beat = beat.model_copy(update={"config_fingerprint": "b" * 64})
    if failure == "profile":
        beat = beat.model_copy(
            update={"profile": c.Profile.OFFLINE if mode == "live" else c.Profile.LIVE}
        )
    store.write_heartbeat(beat)
    if failure == "expired":
        timer._now = beat.expires_at
    client = TestClient(
        create_app(
            ApiDependencies(
                config,
                store,
                FixtureRepositoryValidator(),
                None,
                timer,
                heartbeat_store=None if failure == "store" else store,
                config_fingerprint=None if failure == "fingerprint" else "a" * 64,
            )
        )
    )
    assert client.get("/readyz").status_code == (200 if mode == "live" and failure is None else 503)
    assert client.get("/v1/runs/unknown", headers=OPERATOR).status_code == 401
    assert (
        client.post("/v1/runs/unknown/replays", json={"probe_no": 1}, headers=SERVICE).status_code
        == 401
    )


def parent_receipt(store: SQLiteStore) -> c.ReceiptV1:
    url = "https://github.com/proofhunter-demo/checkout-demo/pull/2"
    target, enrollment = FixtureRepositoryValidator().resolve(url)
    status = store.create_run(
        c.RunRequest(pr_url=url, seed=17),
        idempotency_key="parent",
        requester_scope="service:default",
        target=target,
        enrollment=enrollment,
    )
    lease = store.claim("worker", 60)
    assert lease is not None
    receipt = c.ReceiptV1.model_validate_json(
        Path("fixtures/demo/receipts/fixed.seed17.json").read_bytes()
    )
    receipt = oracle.seal(
        receipt.model_copy(
            update={
                "run_id": status.run_id,
                "created_at": status.created_at,
                "completed_at": status.created_at + timedelta(seconds=1),
                "receipt_sha256": None,
            }
        )
    )
    store.finish(lease, receipt, None)
    return receipt


def test_replay_any_eligible_original_probe(tmp_path: Path) -> None:
    store = SQLiteStore(settings(tmp_path / "db"), clock=clock())
    parent = parent_receipt(store)
    expected = oracle.validate_replay(parent, 2)
    client = client_for(store)
    path = f"/v1/runs/{parent.run_id}/replays"
    response = client.post(path, json={"probe_no": 2}, headers=OPERATOR)
    assert response.status_code == 202
    assert client.post(path, json={"probe_no": 2}, headers=OPERATOR).json() == response.json()
    run = store.load_run(response.json()["run_id"])
    assert run.replay == expected
    assert run.run_kind is c.RunKind.REPLAY
    assert run.parent_receipt_sha256 == parent.receipt_sha256
    assert len(store.quota_reservations(run.run_id)) == 1
    assert len(store.quota_reservations(parent.run_id)) == 1
    assert client.post(path, json={"probe_no": 3}, headers=OPERATOR).status_code == 409
    assert (
        client.post(
            path, json={"probe_no": 99}, headers={**OPERATOR, "Idempotency-Key": "invalid"}
        ).status_code
        == 422
    )
    assert (
        client.post(path, json={"probe_no": 2, "head_sha": "a" * 40}, headers=OPERATOR).status_code
        == 422
    )


@pytest.mark.parametrize("missing", ["receipt", "pinned_revision", "source_file_sha256", "input"])
def test_replay_missing_stored_binding(tmp_path: Path, missing: str) -> None:
    store = SQLiteStore(settings(tmp_path / "db"), clock=clock())
    parent = parent_receipt(store)
    with store._connection() as db:
        if missing == "receipt":
            db.execute("UPDATE retention_gate SET enabled=1")
            db.execute("DELETE FROM receipts WHERE run_id=?", (parent.run_id,))
            db.execute("UPDATE retention_gate SET enabled=0")
        else:
            db.execute("DROP TRIGGER immutable_receipts")
            body = parent.model_dump(mode="json")
            if missing == "input":
                del body["probes"][1]["candidate"]
            else:
                body[missing] = None
            db.execute(
                "UPDATE receipts SET body=? WHERE run_id=?", (json.dumps(body), parent.run_id)
            )
    response = client_for(store).post(
        f"/v1/runs/{parent.run_id}/replays", json={"probe_no": 2}, headers=OPERATOR
    )
    assert response.status_code == 409
    assert (
        c.ErrorEnvelope.model_validate(response.json()).error.code
        is c.ReasonCode.REPLAY_UNAVAILABLE
    )
    with store._connection() as db:
        assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM quota_reservations").fetchone()[0] == 1
