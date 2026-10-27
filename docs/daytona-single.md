# One-command hosted studio

This profile runs the API, studio UI, PostgreSQL 17 and one CPU Chatterbox Nano worker in one Daytona sandbox. Defaults are 4 vCPU, 8 GiB RAM and 20 GiB local disk. Setup never enables GPU or increases these allocations automatically. The existing local SQLite and external PostgreSQL/S3 profiles remain available.

## Setup

From a source checkout with the project's locked Python environment installed:

```sh
uv run voice cloud setup --deployment my-studio --source .
```

The CLI prompts for the Daytona API key and an owner password (at least 12 characters), builds remotely, creates two persistent volumes, initializes PostgreSQL and opens a private signed studio URL. It does not require Docker on the user's computer or a registry account. Packaged desktop runtimes include the approved source archive, so `--source` is unnecessary in those releases.

Review the exact deployment without provisioning:

```sh
uv run voice cloud setup --deployment my-studio --source . --dry-run --json
```

For noninteractive setup, provide a private JSON file (`chmod 600`) with `api_key`, `owner_password`, and optionally `region`, then use `--config /path/to/private.json --no-open --json`. Credentials are stored in the OS credential store. A machine without a functioning credential store fails setup; plaintext credential storage is not a fallback. Remove the input file after its intended use. Do not put credentials in command arguments or the repository.

Rerunning setup uses the same secrets, snapshot and volumes. Source changes require explicit `cloud update`; ordinary starts never build or download model weights. Provider timeouts must be reconciled, not treated as permission to replace a sandbox.

## Daily use

```sh
voice cloud start --deployment my-studio --open
voice cloud status --deployment my-studio
voice cloud backup --deployment my-studio
voice cloud stop --deployment my-studio
```

The desktop connection screen and authenticated localhost workspace Settings provide **Set up hosted studio** and **Start hosted studio**. The desktop connects to the hosted workspace; localhost opens a separate browser tab. Local recordings are not transferred. Use voice export and explicit import to transfer a recording.

Readiness distinguishes a ready API from a loaded model. Nano loads on the first generation. Setup/start progress shows account validation, remote build, volume preparation, bootstrap and readiness. Status polling does not count as owner activity.

Ten idle minutes with no queued/running jobs triggers cleanup, a verified checkpoint, inference shutdown, clean PostgreSQL shutdown and sandbox stop. Active work refreshes the provider deadline. The provider's 30-minute stop timer is a backstop, not the application's graceful-stop mechanism. Auto deletion is disabled.

## Storage and recovery

The database cluster lives at `/var/lib/voice/postgresql` on sandbox-local disk. Only Unix-socket peer authentication is configured; PostgreSQL does not listen on TCP. Migration, application and read-only backup database roles have separate grants. PostgreSQL and the application run under separate unprivileged OS identities; the lifecycle supervisor alone retains provider credentials.

The object volume holds original/prepared references, scripts and finished exports. Local scratch space holds decoding and inference intermediates. Each immutable volume object has a checksum and completion receipt; a partial object cannot be read as published content.

The checkpoint volume holds authenticated encrypted custom-format PostgreSQL dumps and authenticated completion manifests. A deployment-wide publication lock binds owner mutations to verified checkpoints. Database transactions do not span inference or transfer. The latest two complete checkpoints are retained. A corrupt latest checkpoint stops recovery instead of silently choosing older acknowledged data. Intermediate job progress and heartbeats do not trigger full dumps.

Clients should supply `Idempotency-Key` for mutations and retain it when retrying an uncertain response. Successful responses carry `X-Voice-Operation-Id`; retryable failures include `operation_id` when known. `/v1/operations/{id}` reports saved status; `/v1/durability` reports storage availability without exposing backup keys. The UI retains operation keys after failed requests. An interrupted mutation before its receipt is recorded blocks further publication until runtime recovery.

Create an encrypted recovery bundle before relying on sandbox-loss recovery:

```sh
voice cloud recovery-export my-studio.vsr --deployment my-studio
```

Enter a separate recovery passphrase of at least 16 characters. Keep the bundle and passphrase separately. Recovery requires both the existing Daytona volumes and these keys. The bundle is encrypted with a passphrase-derived key; the manifest by itself contains no secrets. An account-wide deletion, deletion of the volumes, or loss of all recovery keys is outside the recovery guarantee.

On a replacement launcher:

```sh
voice cloud recovery-import my-studio.vsr
voice cloud restore --deployment my-studio
```

`restore` requires confirmed sandbox absence. Use `start` for an existing stopped sandbox. Recovery verifies the newest checkpoint and every referenced object, restores PostgreSQL, fences old attempts, and applies existing retry limits. Normal cold startup also reconciles against the verified checkpoint before serving writes. Unacknowledged mutations may be discarded.

References, scripts and generated audio in this profile expire after 30 days. Reads enforce expiry; cleanup runs while active and before serving a restarted workspace. Files can remain physically stored longer while the studio is stopped. Deletion is checkpointed before physical cleanup. Older checkpoints are not automatically selectable, preventing them from resurrecting deleted content.

## Updates and rollback

```sh
voice cloud update /path/to/approved/source --deployment my-studio
voice cloud rollback --deployment my-studio
```

Update builds and validates a candidate without production volumes or credentials. It drains outstanding work, creates a verified checkpoint, confirms the old writer is stopped, replaces the compute runtime, restores the latest checkpoint and validates readiness. Each boundary is journaled in the deployment manifest. Retry the same command after an interruption. Authentication failures and ambiguous provider responses never authorize a second writer.

Rollback selects the recorded schema-compatible image and the newest checkpoint. It does not select an older database. Incompatible schema versions fail closed. Historical data-discarding rollback is intentionally unavailable through these commands; it requires a separately reviewed recovery procedure. Candidate validation failures leave the current writer intact.

## Qualification

Source tests and local container tests do not establish Daytona volume durability. The profile remains **Daytona unqualified** until a real account passes remote build, import → audition → approve → generate → export, shutdown/restart, sandbox deletion recovery, cross-sandbox checkpoint visibility and measured startup/checkpoint cost tests. Do not treat local readback on a FUSE mount as proof of cross-sandbox durability. Review `daytona-single-evidence.md` for the actual gates run.

Application compute estimates charge one combined sandbox. They are not provider billing and exclude persistent volume, snapshot and network charges.

Provider references: [Daytona persistence](https://www.daytona.io/docs/en/persistence/), [FUSE volumes and limitations](https://www.daytona.io/docs/en/volumes/), [remote snapshot API](https://www.daytona.io/docs/en/python-sdk/sync/snapshot/), [secret placeholder semantics](https://www.daytona.io/docs/en/python-sdk/sync/secret/), [PostgreSQL consistent dumps](https://www.postgresql.org/docs/17/app-pgdump.html).

## Explicit destruction

`voice cloud destroy --deployment my-studio` removes stopped compute while retaining the recovery volumes and keys. It refuses to interrupt normal outstanding work. `--purge-data --confirm my-studio` additionally deletes the matching deployment volumes; that action is irreversible and outside the recovery guarantee. Use a new deployment name after an explicit purge.

If an interrupted mutation is reported as `recovery_required`, stop and start the studio. Shutdown verifies the last acknowledged checkpoint without publishing the uncertain mutation; startup restores that checkpoint. This can discard unacknowledged work, and it preserves the stated boundary for previously acknowledged actions.

For a localhost service running directly from a development checkout, choose **Advanced setup → Source directory** and enter that checkout path. Signed desktop runtime builds contain the approved archive automatically; the development form never silently scans a different checkout.
