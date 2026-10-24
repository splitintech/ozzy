# Daytona lifecycle qualification evidence

Status: application lifecycle logic and PostgreSQL coordination are locally tested. No live Daytona account, external PostgreSQL/S3 deployment, model performance benchmark, or paid sandbox allocation has been qualified. This document records what the tests establish and what still requires operator evidence.

## Verification performed on 2026-10-24

- `pytest -q tests/test_cloud.py`: **47 passed**. These are provider-free fault-injection tests using request/error classes from the pinned Daytona SDK, not calls to the Daytona service.
- `pytest -q tests/test_postgres.py`: **5 passed** against a fresh PostgreSQL 14 cluster listening only on a private Unix socket. Every test created and dropped its own random schema. The disposable server was stopped afterward.
- Ruff checks passed for the cloud implementation and both test modules.

The PostgreSQL suite exercises concurrent schema initialization, exclusive worker claiming, fencing expired attempts, idempotent simultaneous submissions, and exhausted-lease terminal recovery. It verifies real transaction/locking behavior; hosted network behavior and production database configuration remain untested.

## Implemented lifecycle guarantees under tested faults

The controller uses database ownership and durable dispatch reservations, and discovers resources by application/deployment labels. Provider failures other than an explicit 404 remain uncertain; they do not authorize a replacement controller or worker. An interrupted launch consumes the configured retry budget. A stale attempt cannot publish after its lease has been replaced.

Controller startup records a unique launch token. If creation times out after allocating a sandbox, cleanup targets that launch's resources. A stable deployment name reduces duplicate allocations when a response is lost. Cleanup failures are reported rather than counted as successful shutdown. If a provider allocation becomes visible only after reconciliation, the next launch and finite provider deadlines are the remaining backstops; this must be validated against live eventual-consistency behavior.

Transient election/reconciliation failures retry in the same controller process. Duplicate controllers retire themselves without touching the elected owner's workers. A worker process that remains alive without claiming a job has a 240-second bootstrap limit. Failed launches and bootstraps consume attempts; active generation is protected from this bootstrap timer. Snapshot and actual worker CPU, memory, disk and GPU must match the configured profile before inference begins. Worker CPU threads match its allocation.

Explicit cloud stop halts controllers before deleting workers, then lists resources again to catch a dispatch that finished during shutdown. A failed stop remains a reported failure. Worker deletion includes GPU resources; stopped controller data is never authoritative. Idle shutdown uses owner activity, not status polling, and active jobs keep the controller and worker alive.

Workers receive only database/object-storage secret references. Daytona credentials, the owner password hash and session secret remain with the launcher/controller. Browser access uses the signed URL for the application port plus application authentication.

Runtime charges include tracked allocation/startup and teardown intervals and use idempotent charge records. They are application estimates, not invoices. Independent database, bucket, transfer and retained-image charges are outside this accounting. The admission budget is not a provider-level spending cap.

## Required live evidence before hosted release

Follow [the deployment runbook](deployment.md) with an authorized reference and a disposable pilot deployment. Record redacted sandbox IDs, selected resources, account quota, image digests, model revisions, cold/warm timings and provider costs. Complete these gates:

1. Verify the account's resource quota and CPU image compatibility; confirm the worker snapshot matches the configured resources.
2. Prove start → owner login → import → audition → approve → generate → download → stop.
3. Confirm full results are in independent PostgreSQL/S3, recreate all application sandboxes, and recover the same voice/history.
4. Observe worker shutdown after queue drain and controller shutdown after ten minutes without owner activity. Keep polling during the idle test.
5. Kill a worker during inference and publication; show bounded recovery and a single published result. Verify active cancellation stops inference.
6. Exercise unavailable capacity, credential expiry, create timeout, failed deletion and controller crash. Inspect the provider for remaining resources.
7. Verify port-scoped signed-link expiry, owner session expiry, revoked API keys, and denied access to expired/deleted objects.
8. Compare actual provider charges with application estimates, then set admission estimates and provider alerts from measured costs.
9. Pass the model-quality and warm p95 performance gates in [the qualification guide](qualification.md). CPU-only source support does not prove the performance target.

Official documentation: [sandbox resources and lifecycle](https://www.daytona.io/docs/en/sandboxes/), [private previews](https://www.daytona.io/docs/en/preview/), [billing](https://www.daytona.io/docs/en/billing/), [volume limitations](https://www.daytona.io/docs/en/volumes/).
