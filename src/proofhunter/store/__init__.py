"""Transactional SQLite persistence for the control plane and leased worker."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel

from proofhunter.config import Settings
from proofhunter.contracts import (
    ACTIVE_STATES,
    AttemptRecord,
    Budget,
    BudgetUsage,
    CheckIntent,
    CostRecord,
    CostStatus,
    Enrollment,
    GithubWebhookEvent,
    ProbeRecord,
    Profile,
    PrTarget,
    QuotaDecision,
    QuotaReservation,
    ReasonCode,
    ReceiptV1,
    Replay,
    ReplayRequest,
    RunCheckpoint,
    RunEvent,
    RunEventKind,
    RunKind,
    RunLease,
    RunRequest,
    RunState,
    RunStatus,
    WorkerHeartbeat,
    canonical_json,
    canonical_sha256,
)
from proofhunter.ports import (
    AdapterError,
    Clock,
    IdempotencyConflictError,
    IdFactory,
    LeaseLostError,
    RunRecord,
)

_MIGRATION = (
    "CREATE TABLE runs (run_id TEXT PRIMARY KEY, kind TEXT NOT NULL, target TEXT NOT NULL, "
    "seed INTEGER NOT NULL, scope TEXT NOT NULL, status TEXT NOT NULL, state TEXT NOT NULL, "
    "parent TEXT, replay TEXT, owner TEXT, epoch INTEGER NOT NULL DEFAULT 0, expires REAL)",
    "CREATE TABLE idempotency_keys (requester_scope TEXT, key TEXT, body TEXT NOT NULL, "
    "run_id TEXT NOT NULL REFERENCES runs(run_id), PRIMARY KEY(requester_scope,key))",
    "CREATE TABLE inbound_messages (requester_scope TEXT, message_id TEXT, "
    "run_id TEXT NOT NULL REFERENCES runs(run_id), PRIMARY KEY(requester_scope,message_id))",
    "CREATE TABLE webhook_deliveries (delivery_id TEXT PRIMARY KEY, event TEXT NOT NULL, "
    "run_id TEXT NOT NULL REFERENCES runs(run_id))",
    "CREATE TABLE run_events (run_id TEXT REFERENCES runs(run_id), seq INTEGER, "
    "body TEXT NOT NULL, PRIMARY KEY(run_id,seq))",
    "CREATE TABLE attempts (run_id TEXT REFERENCES runs(run_id), attempt_id TEXT, "
    "body TEXT NOT NULL, PRIMARY KEY(run_id,attempt_id))",
    "CREATE TABLE probes (run_id TEXT REFERENCES runs(run_id), probe_no INTEGER, "
    "input_sha256 TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY(run_id,probe_no), "
    "UNIQUE(run_id,input_sha256))",
    "CREATE TABLE observations (run_id TEXT REFERENCES runs(run_id), input_sha256 TEXT, "
    "side TEXT, attempt_id TEXT NOT NULL, PRIMARY KEY(run_id,input_sha256,side))",
    "CREATE TABLE receipts (run_id TEXT PRIMARY KEY REFERENCES runs(run_id), body TEXT NOT NULL)",
    "CREATE TABLE check_outbox (run_id TEXT PRIMARY KEY REFERENCES receipts(run_id), "
    "body TEXT NOT NULL, publish_state TEXT NOT NULL DEFAULT 'pending')",
    "CREATE TABLE quota_reservations (run_id TEXT PRIMARY KEY REFERENCES runs(run_id), "
    "reserved REAL NOT NULL, body TEXT NOT NULL)",
    "CREATE INDEX quota_window ON quota_reservations(reserved)",
    "CREATE INDEX active_runs ON runs(state,scope)",
)


class UtcClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class UuidFactory:
    def new_run_id(self) -> str:
        return str(uuid.uuid4())

    def new_attempt_id(self) -> str:
        return str(uuid.uuid4())


def _json(value: Any) -> str:
    return canonical_json(
        value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    ).decode()


class SQLiteStore:
    def __init__(
        self, settings: Settings, *, clock: Clock | None = None, ids: IdFactory | None = None
    ) -> None:
        self.settings = settings
        self.clock = clock or UtcClock()
        self.ids = ids or UuidFactory()
        settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
        with self._transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
            version = db.execute("SELECT COALESCE(MAX(version),0) FROM schema_version").fetchone()[
                0
            ]
            if version not in (0, 1, 2):
                raise ValueError("unsupported database schema")
            if version == 0:
                for statement in _MIGRATION:
                    db.execute(statement)
                db.execute("INSERT INTO schema_version VALUES (1)")
            if version < 2:
                db.execute("ALTER TABLE runs ADD COLUMN checkpoint TEXT")
                db.execute(
                    "CREATE TABLE worker_heartbeats (worker_id TEXT PRIMARY KEY, "
                    "body TEXT NOT NULL)"
                )
                db.execute("INSERT INTO schema_version VALUES (2)")

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.settings.db_path, timeout=0.1, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=100")
            yield db
        finally:
            db.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as db:
            for retry in range(3):
                try:
                    db.execute("BEGIN IMMEDIATE")
                    break
                except sqlite3.OperationalError as error:
                    if error.sqlite_errorcode != sqlite3.SQLITE_BUSY or retry == 2:
                        raise
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def ready(self) -> bool:
        with self._connection() as db:
            return bool(db.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 2)

    def write_heartbeat(self, heartbeat: WorkerHeartbeat) -> None:
        heartbeat = WorkerHeartbeat.model_validate(heartbeat.model_dump())
        ttl = heartbeat.expires_at - heartbeat.observed_at
        with self._transaction() as db:
            now = self.clock.now()
            row = db.execute(
                "SELECT body FROM worker_heartbeats WHERE worker_id=?", (heartbeat.worker_id,)
            ).fetchone()
            if row is not None:
                previous = WorkerHeartbeat.model_validate_json(row[0])
                if now < previous.observed_at:
                    raise ValueError("trusted heartbeat time cannot regress")
                if now == previous.observed_at:
                    if (heartbeat.profile, heartbeat.config_fingerprint) != (
                        previous.profile,
                        previous.config_fingerprint,
                    ):
                        raise ValueError("changed heartbeat requires advancing trusted time")
                    return
            stored = WorkerHeartbeat(
                worker_id=heartbeat.worker_id,
                profile=heartbeat.profile,
                config_fingerprint=heartbeat.config_fingerprint,
                observed_at=now,
                expires_at=now + ttl,
            )
            db.execute(
                "INSERT INTO worker_heartbeats VALUES (?,?) "
                "ON CONFLICT(worker_id) DO UPDATE SET body=excluded.body",
                (stored.worker_id, _json(stored)),
            )

    def read_heartbeat(self, profile: Profile, config_fingerprint: str) -> WorkerHeartbeat | None:
        with self._connection() as db:
            now = self.clock.now()
            matches = [
                h
                for row in db.execute("SELECT body FROM worker_heartbeats")
                if (h := WorkerHeartbeat.model_validate_json(row[0])).is_fresh(
                    profile, config_fingerprint, now
                )
            ]
            return max(matches, key=lambda h: (h.observed_at, h.worker_id), default=None)

    def _quota(self, db: sqlite3.Connection, scope: str, now: datetime) -> QuotaDecision:
        recent = db.execute(
            "SELECT COUNT(*) FROM quota_reservations WHERE reserved > ?", (now.timestamp() - 3600,)
        ).fetchone()[0]
        active = db.execute("SELECT scope FROM runs WHERE state NOT IN ('completed','failed')")
        scopes = [row[0] for row in active]
        policy = self.settings.quota
        if (
            recent >= policy.accepted_runs_per_hour
            or len(scopes) >= policy.max_active_runs
            or scopes.count(scope) >= policy.max_active_per_requester
        ):
            return QuotaDecision(
                allowed=False, reason_code=ReasonCode.RATE_LIMITED, retry_after_seconds=60
            )
        return QuotaDecision(allowed=True, reason_code=None, retry_after_seconds=None)

    def reserve_quota(self, requester_scope: str, run_kind: RunKind) -> QuotaDecision:
        del run_kind
        with self._transaction() as db:
            return self._quota(db, requester_scope, self.clock.now())

    def _new_run(
        self,
        db: sqlite3.Connection,
        scope: str,
        kind: RunKind,
        target: PrTarget,
        seed: int,
        parent: str | None = None,
        replay: Replay | None = None,
    ) -> RunStatus:
        now = self.clock.now()
        if not self.settings.enable_intake or not self._quota(db, scope, now).allowed:
            raise AdapterError(
                "store", ReasonCode.RATE_LIMITED, "intake quota exceeded", retryable=True
            )
        run_id = self.ids.new_run_id()
        reservation = QuotaReservation(
            run_id=run_id,
            requester_scope=scope,
            run_kind=kind,
            reserved_at=now,
            window_started_at=now,
        )
        budget = Budget(
            limits=self.settings.budget,
            used=BudgetUsage(
                pairs=0,
                confirmations=0,
                sandbox_invocations=0,
                meta_requests=0,
                snap_requests=0,
                candidates_generated=0,
            ),
            deadline_at=now + timedelta(seconds=self.settings.budget.deadline_seconds),
            wall_clock_ms=None,
            cost=CostRecord(status=CostStatus.UNKNOWN, amount_usd_micros=None),
        )
        status = RunStatus(
            run_id=run_id,
            status=RunState.QUEUED,
            created_at=now,
            updated_at=now,
            is_stale=None,
            attempts_used=0,
            budget=budget,
            verdict=None,
            reason_code=None,
            receipt_sha256=None,
        )
        db.execute(
            "INSERT INTO runs(run_id,kind,target,seed,scope,status,state,parent,replay,checkpoint) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                kind.value,
                _json(target),
                seed,
                scope,
                _json(status),
                status.status.value,
                parent,
                None if replay is None else _json(replay),
                _json(RunCheckpoint()),
            ),
        )
        db.execute(
            "INSERT INTO quota_reservations VALUES (?,?,?)",
            (run_id, now.timestamp(), _json(reservation)),
        )
        return status

    def _existing(
        self, db: sqlite3.Connection, scope: str, key: str, body: str
    ) -> RunStatus | None:
        row = db.execute(
            "SELECT body,run_id FROM idempotency_keys WHERE requester_scope=? AND key=?",
            (scope, key),
        ).fetchone()
        if row is None:
            return None
        if row["body"] != body:
            raise IdempotencyConflictError(key)
        return self._status(db, row["run_id"])

    def create_run(
        self,
        request: RunRequest,
        *,
        idempotency_key: str,
        requester_scope: str,
        target: PrTarget,
        enrollment: Enrollment,
    ) -> RunStatus:
        if (
            target.repo_id != enrollment.repo_id
            or target.installation_id != enrollment.installation_id
        ):
            raise ValueError("target does not match enrollment")
        with self._transaction() as db:
            body = request.canonical_sha256()
            existing = self._existing(db, requester_scope, idempotency_key, body)
            if existing is not None:
                return existing
            status = self._new_run(
                db,
                requester_scope,
                RunKind.VERIFICATION,
                target,
                17 if request.seed is None else request.seed,
            )
            db.execute(
                "INSERT INTO idempotency_keys VALUES (?,?,?,?)",
                (requester_scope, idempotency_key, body, status.run_id),
            )
            db.execute(
                "INSERT INTO inbound_messages VALUES (?,?,?)",
                (requester_scope, idempotency_key, status.run_id),
            )
            return status

    def create_replay(
        self, parent_run_id: str, request: ReplayRequest, *, idempotency_key: str
    ) -> RunStatus:
        with self._transaction() as db:
            body = canonical_sha256({"parent": parent_run_id, **request.model_dump(mode="json")})
            existing = self._existing(db, "operator", idempotency_key, body)
            if existing is not None:
                return existing
            parent = db.execute(
                "SELECT target FROM runs WHERE run_id=?", (parent_run_id,)
            ).fetchone()
            row = db.execute(
                "SELECT body FROM receipts WHERE run_id=?", (parent_run_id,)
            ).fetchone()
            receipt = None if row is None else ReceiptV1.model_validate_json(row[0])
            if (
                parent is None
                or receipt is None
                or receipt.replay is None
                or receipt.replay.probe_no != request.probe_no
            ):
                raise AdapterError(
                    "store",
                    ReasonCode.REPLAY_UNAVAILABLE,
                    "no eligible pinned input",
                    retryable=False,
                )
            status = self._new_run(
                db,
                "operator",
                RunKind.REPLAY,
                PrTarget.model_validate_json(parent[0]),
                receipt.seed,
                receipt.receipt_sha256,
                receipt.replay,
            )
            db.execute(
                "INSERT INTO idempotency_keys VALUES (?,?,?,?)",
                ("operator", idempotency_key, body, status.run_id),
            )
            return status

    def _status(self, db: sqlite3.Connection, run_id: str) -> RunStatus | None:
        row = db.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return None if row is None else RunStatus.model_validate_json(row[0])

    def get_status(self, run_id: str) -> RunStatus | None:
        with self._connection() as db:
            return self._status(db, run_id)

    def get_receipt(self, run_id: str) -> ReceiptV1 | None:
        with self._connection() as db:
            row = db.execute("SELECT body FROM receipts WHERE run_id=?", (run_id,)).fetchone()
            return None if row is None else ReceiptV1.model_validate_json(row[0])

    def quota_reservations(self, run_id: str) -> Sequence[QuotaReservation]:
        with self._connection() as db:
            return tuple(
                QuotaReservation.model_validate_json(row[0])
                for row in db.execute(
                    "SELECT body FROM quota_reservations WHERE run_id=?", (run_id,)
                )
            )

    def record_webhook_delivery(self, event: GithubWebhookEvent) -> bool:
        with self._transaction() as db:
            if db.execute(
                "SELECT 1 FROM webhook_deliveries WHERE delivery_id=?", (event.delivery_id,)
            ).fetchone():
                return False
            target = PrTarget(
                owner=event.owner,
                repo=event.repo,
                number=event.number,
                repo_id=event.repo_id,
                installation_id=event.installation_id,
            )
            status = self._new_run(db, f"webhook:{event.repo_id}", RunKind.VERIFICATION, target, 17)
            db.execute(
                "INSERT INTO webhook_deliveries VALUES (?,?,?)",
                (event.delivery_id, _json(event), status.run_id),
            )
            return True

    def claim(self, worker_id: str, lease_seconds: int) -> RunLease | None:
        if lease_seconds <= 0:
            raise ValueError("lease duration must be positive")
        with self._transaction() as db:
            now = self.clock.now()
            row = db.execute(
                "SELECT run_id,epoch FROM runs WHERE state NOT IN "
                "('completed','failed') AND (expires IS NULL OR expires<=?) "
                "ORDER BY rowid LIMIT 1",
                (now.timestamp(),),
            ).fetchone()
            if row is None:
                return None
            lease = RunLease(
                run_id=row[0],
                owner=worker_id,
                epoch=row[1] + 1,
                expires_at=now + timedelta(seconds=lease_seconds),
            )
            db.execute(
                "UPDATE runs SET owner=?,epoch=?,expires=? WHERE run_id=?",
                (worker_id, lease.epoch, lease.expires_at.timestamp(), lease.run_id),
            )
            return lease

    def _check(self, db: sqlite3.Connection, lease: RunLease) -> RunStatus:
        row = db.execute(
            "SELECT owner,epoch,expires,status FROM runs WHERE run_id=?", (lease.run_id,)
        ).fetchone()
        if (
            row is None
            or row[0] != lease.owner
            or row[1] != lease.epoch
            or row[2] is None
            or row[2] <= self.clock.now().timestamp()
        ):
            raise LeaseLostError(lease.run_id)
        status = RunStatus.model_validate_json(row[3])
        if status.status not in ACTIVE_STATES:
            raise LeaseLostError(lease.run_id)
        return status

    def renew(self, lease: RunLease, lease_seconds: int) -> RunLease:
        with self._transaction() as db:
            self._check(db, lease)
            if lease_seconds <= 0:
                raise ValueError("lease duration must be positive")
            renewed = RunLease(
                run_id=lease.run_id,
                owner=lease.owner,
                epoch=lease.epoch,
                expires_at=self.clock.now() + timedelta(seconds=lease_seconds),
            )
            db.execute(
                "UPDATE runs SET expires=? WHERE run_id=?",
                (renewed.expires_at.timestamp(), lease.run_id),
            )
            return renewed

    def _save_status(self, db: sqlite3.Connection, status: RunStatus) -> None:
        status = RunStatus.model_validate(status.model_dump())
        db.execute(
            "UPDATE runs SET status=?,state=? WHERE run_id=?",
            (_json(status), status.status.value, status.run_id),
        )

    def reserve_progress(self, lease: RunLease, checkpoint: RunCheckpoint) -> RunCheckpoint:
        with self._transaction() as db:
            status = self._check(db, lease)
            progress = RunCheckpoint.model_validate(checkpoint.model_dump())
            row = db.execute(
                "SELECT checkpoint,target,replay FROM runs WHERE run_id=?", (lease.run_id,)
            ).fetchone()
            if row["checkpoint"] is None:
                raise ValueError("legacy run has no recoverable progress ledger")
            previous = RunCheckpoint.model_validate_json(row["checkpoint"])
            pin = progress.pinned_revision
            if previous.pinned_revision is not None and pin != previous.pinned_revision:
                raise ValueError("pinned revision is immutable")
            target = PrTarget.model_validate_json(row["target"])
            if pin is not None and any(
                getattr(pin, name) != getattr(target, name) for name in PrTarget.model_fields
            ):
                raise ValueError("pinned revision must match trusted target")
            if row["replay"] is not None and pin is not None:
                replay = Replay.model_validate_json(row["replay"])
                if pin.base_sha != replay.base_sha or pin.head_sha != replay.head_sha:
                    raise ValueError("replay pin must match original revisions")
            before, after = previous.provider_allocations, progress.provider_allocations
            if after.meta < before.meta or after.snap < before.snap:
                raise ValueError("provider allocations cannot decrease")
            limits = status.budget.limits
            if after.meta > limits.max_meta_requests or after.snap > limits.max_snap_requests:
                raise ValueError("provider allocations exceed run caps")
            if progress == previous:
                raise ValueError("progress must advance; duplicate reservation authorizes no I/O")
            used = status.budget.used.model_dump()
            used.update(meta_requests=after.meta, snap_requests=after.snap)
            budget = Budget.model_validate({**status.budget.model_dump(), "used": used})
            db.execute(
                "UPDATE runs SET checkpoint=? WHERE run_id=?", (_json(progress), lease.run_id)
            )
            self._save_status(db, status.model_copy(update={"budget": budget}))
            return progress

    def checkpoint(self, lease: RunLease, event: RunEvent) -> None:
        with self._transaction() as db:
            status = self._check(db, lease)
            event = RunEvent.model_validate(event.model_dump())
            seq = (
                db.execute(
                    "SELECT COUNT(*) FROM run_events WHERE run_id=?", (lease.run_id,)
                ).fetchone()[0]
                + 1
            )
            if event.run_id != lease.run_id or event.seq != seq or event.at < status.updated_at:
                raise ValueError("invalid checkpoint identity, sequence or timestamp")
            if event.kind is RunEventKind.STATE_CHANGED:
                if event.from_state is not status.status:
                    raise ValueError("checkpoint does not match current state")
                status = status.model_copy(
                    update={"status": event.to_state, "updated_at": event.at}
                )
            self._save_status(db, status)
            db.execute("INSERT INTO run_events VALUES (?,?,?)", (lease.run_id, seq, _json(event)))

    def record_attempt(self, lease: RunLease, attempt: AttemptRecord) -> None:
        with self._transaction() as db:
            status = self._check(db, lease)
            attempt = AttemptRecord.model_validate(attempt.model_dump())
            if attempt.run_id != lease.run_id or attempt.lease_epoch != lease.epoch:
                raise ValueError("attempt is not bound to current lease")
            row = db.execute(
                "SELECT body FROM attempts WHERE run_id=? AND attempt_id=?",
                (lease.run_id, attempt.attempt_id),
            ).fetchone()
            if row is not None:
                old = AttemptRecord.model_validate_json(row[0])
                if (old.side, old.input_sha256, old.created_at, old.lease_epoch, old.probe_no) != (
                    attempt.side,
                    attempt.input_sha256,
                    attempt.created_at,
                    attempt.lease_epoch,
                    attempt.probe_no,
                ) or (old.charged and not attempt.charged):
                    raise ValueError("attempt identity and charges are immutable")
            db.execute(
                "INSERT INTO attempts VALUES (?,?,?) ON CONFLICT(run_id,attempt_id) "
                "DO UPDATE SET body=excluded.body",
                (lease.run_id, attempt.attempt_id, _json(attempt)),
            )
            charged = sum(
                AttemptRecord.model_validate_json(r[0]).charged
                for r in db.execute("SELECT body FROM attempts WHERE run_id=?", (lease.run_id,))
            )
            used = status.budget.used.model_copy(update={"sandbox_invocations": charged})
            budget = status.budget.model_copy(update={"used": used})
            self._save_status(
                db, status.model_copy(update={"attempts_used": charged, "budget": budget})
            )

    def commit_probe(self, lease: RunLease, probe: ProbeRecord) -> None:
        with self._transaction() as db:
            status = self._check(db, lease)
            probe = ProbeRecord.model_validate(probe.model_dump())
            number = (
                db.execute(
                    "SELECT COUNT(*) FROM probes WHERE run_id=?", (lease.run_id,)
                ).fetchone()[0]
                + 1
            )
            if probe.probe_no != number:
                raise ValueError("probe must have the next sequence number")
            digest = probe.candidate.input_sha256
            db.execute(
                "INSERT INTO probes VALUES (?,?,?,?)", (lease.run_id, number, digest, _json(probe))
            )
            for observation in (probe.base, probe.head, probe.confirmation):
                if observation is not None:
                    db.execute(
                        "INSERT INTO observations VALUES (?,?,?,?)",
                        (lease.run_id, digest, observation.side.value, observation.attempt_id),
                    )
            confirmations = db.execute(
                "SELECT COUNT(*) FROM observations WHERE run_id=? AND side='confirm'",
                (lease.run_id,),
            ).fetchone()[0]
            used = status.budget.used.model_copy(
                update={"pairs": number, "confirmations": confirmations}
            )
            self._save_status(
                db,
                status.model_copy(
                    update={"budget": status.budget.model_copy(update={"used": used})}
                ),
            )

    def finish(self, lease: RunLease, receipt: ReceiptV1, check: CheckIntent | None) -> None:
        with self._transaction() as db:
            status = self._check(db, lease)
            receipt = ReceiptV1.model_validate_json(_json(receipt))
            if receipt.run_id != lease.run_id or receipt.receipt_sha256 is None:
                raise ValueError("finish requires a sealed receipt for this run")
            row = db.execute(
                "SELECT kind,target,seed,parent FROM runs WHERE run_id=?", (lease.run_id,)
            ).fetchone()
            target = PrTarget.model_validate_json(row["target"])
            revision = receipt.pinned_revision
            if (
                receipt.run_kind.value != row["kind"]
                or receipt.seed != row["seed"]
                or receipt.parent_receipt_sha256 != row["parent"]
                or (
                    revision is not None
                    and (revision.owner, revision.repo, revision.number, revision.repo_id)
                    != (target.owner, target.repo, target.number, target.repo_id)
                )
            ):
                raise ValueError("receipt provenance does not match run")
            if check is not None:
                check = CheckIntent.model_validate(check.model_dump())
                if (
                    check.run_id != lease.run_id
                    or check.receipt_sha256 != receipt.receipt_sha256
                    or receipt.execution_mode.value != "live"
                    or revision is None
                    or check.head_sha != revision.head_sha
                    or check.repo_id != target.repo_id
                    or check.installation_id != target.installation_id
                ):
                    raise ValueError("check must bind the live receipt")
            db.execute("INSERT INTO receipts VALUES (?,?)", (lease.run_id, _json(receipt)))
            if check is not None:
                db.execute(
                    "INSERT INTO check_outbox(run_id,body) VALUES (?,?)",
                    (lease.run_id, _json(check)),
                )
            state = RunState.FAILED if receipt.verdict is None else RunState.COMPLETED
            self._save_status(
                db,
                status.model_copy(
                    update={
                        "status": state,
                        "updated_at": receipt.completed_at,
                        "verdict": receipt.verdict,
                        "reason_code": receipt.reason_code,
                        "receipt_sha256": receipt.receipt_sha256,
                        "budget": receipt.budget,
                        "is_stale": False,
                    }
                ),
            )
            db.execute("UPDATE runs SET owner=NULL,expires=NULL WHERE run_id=?", (lease.run_id,))

    def load_run(self, run_id: str) -> RunRecord:
        with self._transaction() as db:
            row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            return RunRecord(
                run_id=run_id,
                run_kind=RunKind(row["kind"]),
                target=PrTarget.model_validate_json(row["target"]),
                seed=row["seed"],
                status=RunStatus.model_validate_json(row["status"]),
                parent_receipt_sha256=row["parent"],
                checkpoint=(
                    None
                    if row["checkpoint"] is None
                    else RunCheckpoint.model_validate_json(row["checkpoint"])
                ),
                replay=None if row["replay"] is None else Replay.model_validate_json(row["replay"]),
                probes=tuple(
                    ProbeRecord.model_validate_json(r[0])
                    for r in db.execute(
                        "SELECT body FROM probes WHERE run_id=? ORDER BY probe_no", (run_id,)
                    )
                ),
                attempts=tuple(
                    AttemptRecord.model_validate_json(r[0])
                    for r in db.execute(
                        "SELECT body FROM attempts WHERE run_id=? ORDER BY rowid", (run_id,)
                    )
                ),
                events=tuple(
                    RunEvent.model_validate_json(r[0])
                    for r in db.execute(
                        "SELECT body FROM run_events WHERE run_id=? ORDER BY seq", (run_id,)
                    )
                ),
            )


def create_store(settings: Settings) -> SQLiteStore:
    return SQLiteStore(settings)
