# On-demand Daytona deployment runbook

The controller and inference worker run only during a session. PostgreSQL and private object storage must survive deletion of both sandboxes. A launcher is required to restart a stopped service. No always-running gateway is included.

## 1. Qualify the account and image

Verify the Daytona account permits the chosen resources, region, snapshot size, and outbound access. Start with a 1 CPU / 1 GiB controller and 4 CPU / 8 GiB inference worker; the worker snapshot resources must match application configuration. Eight CPUs and GPU access are separate account/benchmark decisions. The shipped Dockerfile is a CPU implementation. CUDA is not a qualified release profile.

Build each immutable image from a clean commit with locked dependencies:

```sh
docker build -f deploy/Dockerfile --target controller -t voice-studio-controller .
docker build -f deploy/Dockerfile --target worker -t voice-studio-worker .
```

The worker image installs and verifies the pinned Nano model. Original and Turbo can be benchmarked locally; add their pinned assets to a reviewed worker image before selecting them for hosted work. The controller build asserts that Torch is absent. `containers.yml` performs image smoke checks; it does not push or provision anything. Build success is not Daytona integration proof. The OS package manager uses Debian repositories; retain image digests and build inventories, because rebuilding later can incorporate OS security updates.

Publish images to your approved private registry, record their actual digests, then create snapshots using those digests. These commands create billable provider resources and belong to operator deployment, not ordinary tests:

```sh
voice cloud snapshot voice-controller-VERSION registry.example/voice-controller@sha256:ACTUAL_DIGEST --cpu 1 --memory 1 --disk 10
voice cloud snapshot voice-worker-VERSION registry.example/voice-worker@sha256:ACTUAL_DIGEST --cpu 4 --memory 8 --disk 10
```

The image entrypoint preserves Daytona's command/toolbox invocation. The launcher starts the API/worker in a named process session rather than replacing the sandbox's bootstrap command. Validate this against the actual account's image support before using the snapshot in a pilot.

## 2. Configure independent durable services

Create a dedicated PostgreSQL database with TLS and a private S3-compatible bucket. Use dedicated credentials, disable public bucket/object access, and enable the provider's encryption/backups. Do not place database files on Daytona FUSE volumes. Model files may use snapshots or a separately qualified cache volume.

Use `deploy/hosted.env.example` as a list of configuration names; its `REPLACE` values are not deployable. Create the owner password hash using `voice password-hash`. Store the hash, a random session secret, database URL, bucket credentials, worker snapshot name, and the controller's Daytona API key in the Daytona secret store. Keep raw secrets out of Git, browser code, logs, images and command arguments.

Configure `VOICE_DAYTONA_SECRET_NAMES` as a JSON mapping from application environment names to Daytona secret names. Launcher credentials remain on the owner machine; worker creation passes only the durable-storage secrets it needs. Inspect the exact deployed configuration before qualification.

With hosted configuration available privately to the CLI, run `voice cloud configure-storage` to configure the expiring-object prefix. Review the resulting bucket lifecycle rule. It must expire generated scripts/audio after 30 days and must not expire retained voice references. The API denies retrieval after expiration even before provider lifecycle cleanup occurs. Provider deletion can be asynchronous; test it independently.

## 3. Start and exercise a session

```sh
voice cloud start --snapshot voice-controller-VERSION --secret-names "$VOICE_DAYTONA_SECRET_NAMES"
voice cloud status
voice cloud stop
```

The start response contains a time-limited, port-scoped signed preview URL. Treat it as sensitive and avoid copying it into logs or tickets. Application owner authentication is also required. The URL expires; reconnect using the launcher to obtain another. Never expose Daytona's API credential or sandbox-wide preview token to the browser.

Import an authorized reference using the explicit hosted upload action, generate its audition, listen and approve, submit a message, and download both formats. Local histories/references are not synchronized automatically. Readiness must distinguish API availability, installed assets and the actual inference phase.

## 4. Lifecycle and recovery qualification

Record actual sandbox IDs, image digests, timestamps and redacted provider status:

1. Start controller; generate and retrieve a complete result.
2. Observe worker creation, loading, inference, processing and durable artifact commit.
3. Verify the worker is removed after its queue drains.
4. Keep only status polling active and verify controller shutdown after ten minutes with no owner activity and no outstanding job.
5. Restart the controller and recover voice/history from PostgreSQL/S3.
6. Interrupt the worker while generating and while publishing. Verify lease/attempt recovery, bounded retries and no duplicate published result.
7. Cancel queued and active jobs. Confirm the inference process stops and cannot publish afterward.
8. Simulate provider creation failure, expired credential, missing quota and cleanup failure. Confirm useful errors and eventual orphan cleanup on the next launch.
9. Confirm expiry/deletion deny all original, preview and export retrieval paths.

The application refreshes lifecycle activity deliberately; an internal model process does not count as Daytona user activity. Finite sandbox deadlines remain a backstop. Reconcile any failed deletion manually in Daytona before assuming costs stopped. GPU sandboxes require a separately reviewed lifecycle because stopping deletes them.

## 5. Budget and incident handling

`VOICE_MONTHLY_BUDGET_USD` is an application admission limit, not a provider spending guarantee. Set per-job estimates and runtime rates from the measured configuration. Compare runtime accounting with provider invoices, including creation, load, teardown, retries, retained snapshots, database, bucket and transfer. Add provider billing alerts independently. Do not claim the $50 target until pilot results cover these categories.

For failed inference, retain redacted job/attempt/timing metadata and retry only after checking the error class. For storage outages, jobs must not become completed until all required outputs exist durably. For lost credentials, revoke them at the provider, replace secret references and restart. For rollback, restore a previously qualified immutable controller/worker image pair compatible with the current database schema; back up metadata before schema changes. Never roll back by pointing at an unpinned model branch.

Official references: [Daytona sandboxes](https://www.daytona.io/docs/en/sandboxes/), [preview access](https://www.daytona.io/docs/en/preview/), [volumes](https://www.daytona.io/docs/en/volumes/), [billing](https://www.daytona.io/docs/en/billing/), [FastAPI containers](https://fastapi.tiangolo.com/deployment/docker/).
