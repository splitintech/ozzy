# Implementation audit against the approved twelve-step plan

Audit updated: 2026-10-25. **Engineering code status: implemented locally**, with the local evidence and platform limits below. This is not completion of the plan's external acceptance criteria or stable-release qualification.

## October 25 continuation

All twelve steps have implementation code or executable qualification/release
tooling. The remaining locally reproducible gaps found during this continuation
were implemented and verified:

- Frozen inference now includes lazy/JIT source modules and transitive package
  metadata; Numba caches stay outside the bundle. The packaged API and worker
  completed reference import, audition, activation, generation, all exports, and
  deletion using the existing application-generated synthetic fixture. Separate
  frozen inference also completed with socket connections disabled. These full
  workflow results are from the earlier repaired runtime; the final rebuild
  passed import/API smoke and its complete one-thread replay. Its initial
  four-thread replay timed out; both outcomes and host limits are recorded in
  the verification notes.
- Runtime installation runs readiness before activation. Installation/rollback
  share locks, preserve the previous version, reject sequence replay after
  rollback, and support repair with identical verified bytes.
- Model downloads share an OS lock between CLI/API processes, preserve existing
  files on failed downloads, and recover interrupted activation.
- Desktop cloud status is wired through native credential storage to the CLI;
  status inspection does not create a session or count as owner activity.
- Invalid/nonfinite budgets, reservations, lifetimes and resource limits are
  rejected at configuration load. Benchmark/job timing phases are separately
  recorded, and measured profile comparison has an executable selection tool.
- Native build arguments now reach Tauri. A current macOS preview `.app` and
  checksum-verified `.dmg` were built; headless DMG packaging avoids Finder
  automation. Source release packaging rejects dirty checkouts rather than
  mixing working-tree wheels with an older committed source archive.
- macOS packaging detects external libraries and OS-minimum mismatches. The
  local development runtime requires macOS 26 because of Homebrew libraries;
  distributable builds must satisfy the advertised macOS 13 minimum.

Current verification is in [local verification](verification.md). Windows,
MPS/CUDA, other model variants, signed download/install/update, live Daytona,
owner quality, and measured hosted performance remain acceptance work requiring
their actual environments or inputs. No paid provisioning, publication, push,
or stable release occurred.

The October 24 reconciliation and historical evidence follow.

The [approved plan](approved-plan.md) is preserved verbatim alongside this audit.

## Commit and repository reconciliation

- SplitIn commit `6c78ffccf`, merged by `2ebdb396a` through PR #110, implements its existing **Voice-to-Prompt transcription** service: private Daytona runtime, authenticated BFF, readiness, and CPU Whisper integration. Its verification document explicitly says no Daytona deployment was performed. This is related infrastructure, not the standalone Chatterbox synthesis application.
- SplitIn's Slybroadcast and map-studio Daytona commits are separate consumers and are not counted toward this plan's voice-generation acceptance.
- The standalone implementation from October 20 was recovered from its saved source-edit records after its temporary directory disappeared. At the start of this continuation the new `RIP/voice-studio` repository had no commits. Its source is now preserved separately from SplitIn, with new lockfiles and fresh checks. Earlier test reports are not treated as current proof.
- No SplitIn source or existing worktree changes were included in this standalone delivery.
- This continuation created standalone local commit `351b642` with 167 source/configuration files. Models, recordings, credentials, and build artifacts are ignored. No remote push or public release was performed.

## Plan coverage

| Step | Implemented source | Current acceptance boundary |
|---|---|---|
| 1. Foundations | MIT license; npm and uv workspaces; lightweight API/inference split; source/model pins; lockfiles; versioned schema; development/contribution docs | Fresh Python and JS resolution/build checks; actual OS/container image builds are reported separately |
| 2. Qualification | Twenty scripts, multi-seed benchmark, CPU controls, quality/performance gate, cost inputs | Owner reference and scores absent; Daytona quota/capacity and Original/Turbo/Nano comparison remain unqualified |
| 3. Reference preparation | Decoding, bounded imports, excerpt selection, silence/clipping checks, checksums, immutable IDs, authorization and audition activation | Real MP3/WAV pipeline tested with synthetic fixtures; owner audition acceptance pending |
| 4. Portable engine | Lazy Original/Turbo/Nano adapters; local-file verification; CPU/MPS/CUDA selection; serialization; segmentation; metadata | Nano offline generation and synthetic-reference conditioning both completed; owner voice, other variants, MPS/CUDA, Windows, and Daytona inference need qualification |
| 5. Finishing/export | Lossless master, edge fades, LUFS normalization, true-peak validation, MP3/WAV/8kHz profiles, decode checks, separate takes/manifests | Synthetic and real Nano exports checked; Perth response regression completed on one generated sample. Owner message completeness remains unqualified |
| 6. API/queue/storage | Versioned API/generated TS contracts; SQLite/PostgreSQL; S3; leases, attempts, retries, cancellation, idempotency, durable completion, expiry | Current unit/contract tests pass; PostgreSQL and browser results recorded in their verification documents; remote storage recovery still needs live acceptance |
| 7. Daytona lifecycle | Launcher commands; controller/worker separation; private signed preview; secret mappings; idle/TTL handling; orphan recovery; resource checks | Fault-injection tests cover failures; actual account start/generate/download/recreate sequence awaits credentials and qualified images |
| 8. Shared UI | Create, Voice, History, Settings; explicit hosted transfers; progress phases; playback/export; controls and keyboard handling | Component/browser checks exercise real local API plumbing with labeled test inference; desktop clean-machine workflow remains a gate |
| 9. Native runtime | Tauri process/credential management; platform/device inspection; signed, bounded runtime packs; atomic install; local token; rollback/update handling | Native Rust compile/tests verified; actual preview builds reported separately; signed first-use/offline Windows/macOS acceptance pending |
| 10. Access/lifecycle | Owner login/throttling; private sessions; API-key revocation; origin checks; bounded uploads; expiry/deletion including S3 versions; child-secret isolation | Tested locally, including deleted/expired content denial; live infrastructure permissions and native OS credential behavior require deployment checks |
| 11. Release pipeline | Source/dependency inventories; CPU Dockerfile; PyInstaller packaging; native signing/notarization; preview/candidate/stable workflows; cryptographic release gates | Four Python wheels/sdists, source archive, SBOM and notices built. Docker daemon unavailable locally. Missing signing credentials or qualification evidence blocks stable artifacts |
| 12. Pilot/publication | Benchmark reports, cost forecast across all categories, compute admission limit and spend alerts | Actual owner pilot, measured hosted bill, approved budget, and publication are outstanding |

## Defects corrected during this continuation

- Runtime readiness now distinguishes a loaded model from a merely running process; inference children do not inherit application/cloud secrets.
- A failed model switch no longer reuses an unloaded cached model. Reference-conditioning cache identity is invalidated before preparation.
- One lease guardian covers reference downloads, inference, finishing and uploads. Lease loss retries safely; cancellation and replacement attempts retain their distinct outcomes.
- Concurrent deletion tolerates already-removed descendants while surfacing permission/I/O failures. Browser deletion also restores usable keyboard focus.
- Daytona startup/stop races, uncertain creation cleanup, election retries, hung bootstrap and CPU-thread allocation have fault-injection coverage. Launcher mappings and credentials honor environment/`.env` configuration.
- First-use setup displays verified size and disk requirements, uses a configurable release manifest URL, and offers only detected devices. Hosted spend alerts, readable contrast and explicit transfer controls are wired to the shared UI.
- Stable packaging binds qualification evidence to actual artifact hashes and trusted runtime/updater keys. CI includes native platform checks and a lightweight browser job.

Current evidence and practical limits are recorded in [local verification](verification.md), [browser verification](browser-verification.md), and [Daytona qualification](daytona-qualification.md).

## Required inputs to finish external acceptance

1. Local path to the authorized owner reference and agreed voicemail scripts.
2. Daytona account configuration and quota; independent PostgreSQL and private S3 settings.
3. macOS Developer ID/notarization and Windows signing credentials, runtime/update signing keys, and release artifact hosting.
4. Clean macOS and Windows test environments and owner review of all acceptance takes.

Do not enter secret values into this document or chat. Supply configuration paths or environment-variable names. Source-level gaps continue to be fixed without waiting for these inputs; evidence-dependent gates remain explicitly pending.
