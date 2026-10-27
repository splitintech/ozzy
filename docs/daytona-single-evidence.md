# Single-sandbox hosted studio: implementation evidence

Status: **implemented locally; Daytona unqualified**.

This report covers the new `hosted_daytona_single` profile. No live Daytona account was used. The existing preview and its local recordings were not migrated or replaced.

## Implemented source

- Explicit deployment/storage profiles, versioned non-secret deployment manifests, fixed 4 vCPU / 8 GiB / 20 GiB CPU allocation and quota checks.
- Pinned combined PostgreSQL 17/Nano image; approved source allowlist, source fingerprints, packaged desktop source archive and remote snapshot orchestration using the existing Daytona SDK 0.225.0.
- OS credential-store integration, restricted runtime bootstrap, encrypted recovery export/import, idempotent setup and stateful lifecycle commands.
- Immutable volume objects, verified completion receipts, encrypted authenticated PostgreSQL checkpoints, migration 0002, durable operation receipts and publication coordination.
- Restore, attempt fencing, latest-two checkpoint retention, expiry, durable deletion, orphan cleanup and graceful idle shutdown.
- Desktop and localhost setup/start actions, local-only authenticated launcher APIs, saving/error states, updated OpenAPI and generated TypeScript contracts.
- Isolated candidate validation, journaled upgrades, compatible-image rollback using the newest data, explicit destruction and redacted operational status.

## Local verification

| Gate | Evidence |
| --- | --- |
| Python and PostgreSQL | 179 tests pass, including real PostgreSQL 17 dump/restore, concurrent claims and stale-attempt fencing. |
| Failure injection | Partial volume writes, missing/delayed completion records, checksum corruption, dump/upload/marker failures and lost responses; successful acknowledgments retain replayable operation receipts. |
| Setup and lifecycle | Repeated setup, lost create response, uncertain provider errors, stopped-sandbox reuse, duplicate-writer rejection, idle/busy behavior and checkpoint-failure shutdown prevention. Provider calls use SDK-shaped fixtures. |
| Deletion and expiry | Deleted operation replay is rejected; 30-day expiry is enforced on reads; failed expiry checkpoints preserve the physical reference until its tombstone is durable. |
| UI | 17 Vitest tests, TypeScript check and production build pass. Six Chromium browser tests pass across desktop and mobile sizes, including keyboard/a11y checks and separate-tab hosted launch. Browser synthesis and provider navigation use explicit fixtures. |
| Native | Seven Rust tests pass and the native code compiles. React native-bridge tests cover connection behavior. This does not qualify a signed installed desktop release. |
| Packaging | 16 packaging tests pass. A frozen macOS launcher built with the approved source archive completes setup dry-run from an empty directory without a Git checkout. The smoke launcher excludes inference dependencies and is not a release installer. |
| Upgrade candidate | Real isolated `voice single validate` passes: PostgreSQL 17 initialization, migrations, API readiness, encrypted checkpoint, model checksum validation and orderly shutdown. |
| Container | Final-image real Nano end-to-end and repeated compute-deletion recovery pass. Containers use synthetic recordings and disposable Docker volumes; they do not prove Daytona FUSE durability. |

Sanitized logs and JSON reports are stored in `artifacts/daytona-single/` in the local checkout. Generated evidence files are not required build inputs.

## Container provenance and limits

The repository Dockerfile's `single` target built successfully with immutable base-image digests and checksum-verified Nano assets. The initial combined image is `sha256:9676676e5373e06a041aa6c182879ed6550d7650e111e4c86360df43df1958f6`. Final source fixes and the rebuilt UI were applied as a small source overlay to that image for acceptance; the report identifies the resulting image. This is not a fresh full rebuild of every final Dockerfile stage.

The test VM uses 4 CPUs, 8 GiB RAM and a 20 GiB Docker data disk. Initial build-cache accumulation exhausted this disk during extraction. Removing only the isolated build cache allowed runtime startup without increasing the allocation. AMD64 runs under Rosetta on the development Mac, so these timings are not Daytona performance estimates. An earlier QEMU inference run was intentionally interrupted because model loading approached the normal 900-second job deadline; the final run retains that deadline and the original resource limits. The test harness disables only provider monitoring; PostgreSQL, API, checkpoint encryption, storage and worker are real.

## Reproduce local checks

```sh
uv run pytest -q
uv run pytest -q deploy/tests
npm run check
npm run test:e2e
cargo test --manifest-path apps/desktop/src-tauri/Cargo.toml
```

PostgreSQL tests require `VOICE_TEST_POSTGRES_URL` pointing at a disposable PostgreSQL 17 database. Actual dump/restore failure-boundary tests additionally use the disposable Docker container specified by `VOICE_TEST_PG_CONTAINER` and `VOICE_TEST_DOCKER`. Never point these fixtures at a production database.

Build and exercise a disposable combined image with local Docker for engineering qualification:

```sh
docker buildx build --platform linux/amd64 --target single --load -t voice-studio:single -f deploy/Dockerfile .
uv run python scripts/test_single_container.py --image voice-studio:single --inference
```

Local Docker is a test requirement here, not an end-user prerequisite for remote Daytona setup.

## Daytona qualification still required

Status remains **Daytona unqualified**. Use a disposable deployment with the actual provider to verify remote builds without local Docker, quotas, private preview access, FUSE permissions and cross-sandbox visibility, import → audition → approve → generate → export, stop/start, sandbox deletion recovery, cleanup and upgrade/rollback. Record actual resource allocation, startup time, checkpoint overhead and provider costs.

Do not claim the no-loss recovery target from local tests alone. If a completed checkpoint or referenced object is not visible from an isolated replacement sandbox, keep this profile unqualified and report that limitation. Account-wide or volume deletion is outside the recovery guarantee. Stable signing, clean-machine desktop acceptance and owner voice-quality approval remain separate gates.

## Approved source provenance

The final packaged source context has SHA-256 `6b9d3ee37cdac9f6eadea4de1766548d99fea7f7055b15a5add48cd0a19f8c0a`. The frozen launcher dry-run reports this fingerprint from its embedded archive while running outside any checkout. The archive excludes local recordings, credentials, databases, models, build outputs and unrelated worktree files.

## Final container results

Final acceptance image: `sha256:1d1882e2b9a2855e72c352022eb6e079a63c024c389dcacab876822860b605bf` (`voice-studio:single-final`). All checks pass:

1. Automatic PostgreSQL initialization, migrations and API/checkpoint readiness.
2. Application DDL denied, backup-role writes denied, PostgreSQL TCP listening disabled, bootstrap payload consumed.
3. Actual CPU Nano audition → approval → message generation → export download.
4. Idempotent API-key creation across renewed owner sessions and durable revocation.
5. Clean PostgreSQL shutdown.
6. Compute-container deletion and recovery of the reference, completed export and revoked-key rejection.
7. Voice deletion followed by another compute-container deletion/restore; the voice does not reappear.

On this Rosetta test VM, initial API readiness took 14.45 seconds; replacement restores took 17.73 and 16.48 seconds. The first synthetic audition, including model loading, took 128.92 seconds; the warm approved take took 18.54 seconds. These are local fixture measurements, not hosted performance or voice-quality qualification. The container retained 4 CPUs / 8 GiB memory with a roughly 20 GiB backing filesystem and about 8.7 GiB free at the final measurement. No larger allocation, GPU or increased generation timeout was used.

Machine-readable results: `artifacts/daytona-single/summary.json` and `container-final.json`. The final source fingerprint in this report matches the tested frozen launcher's embedded build archive.
