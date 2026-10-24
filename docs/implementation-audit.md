# Implementation audit against the approved twelve-step plan

Audit date: 2026-10-24. This file distinguishes source implementation, locally verified behavior, and external acceptance. The implementation remains an engineering preview until the required owner, hosted, and signed-platform evidence exists.

## Commit and repository reconciliation

- SplitIn commit `6c78ffccf`, merged by `2ebdb396a` through PR #110, implements its existing **Voice-to-Prompt transcription** service: private Daytona runtime, authenticated BFF, readiness, and CPU Whisper integration. Its verification document explicitly says no Daytona deployment was performed. This is related infrastructure, not the standalone Chatterbox synthesis application.
- SplitIn's Slybroadcast and map-studio Daytona commits are separate consumers and are not counted toward this plan's voice-generation acceptance.
- The standalone implementation from October 20 was recovered from its saved source-edit records after its temporary directory disappeared. At the start of this continuation the new `RIP/voice-studio` repository had no commits. Its source is now preserved separately from SplitIn, with new lockfiles and fresh checks. Earlier test reports are not treated as current proof.
- No SplitIn source or existing worktree changes were included in this standalone delivery.

## Plan coverage

| Step | Implemented source | Current acceptance boundary |
|---|---|---|
| 1. Foundations | MIT license; npm and uv workspaces; lightweight API/inference split; source/model pins; lockfiles; versioned schema; development/contribution docs | Fresh Python and JS resolution/build checks; actual OS/container image builds are reported separately |
| 2. Qualification | Twenty scripts, multi-seed benchmark, CPU controls, quality/performance gate, cost inputs | Owner reference and scores absent; Daytona quota/capacity and Original/Turbo/Nano comparison remain unqualified |
| 3. Reference preparation | Decoding, bounded imports, excerpt selection, silence/clipping checks, checksums, immutable IDs, authorization and audition activation | Real MP3/WAV pipeline tested with synthetic fixtures; owner audition acceptance pending |
| 4. Portable engine | Lazy Original/Turbo/Nano adapters; local-file verification; CPU/MPS/CUDA selection; serialization; segmentation; metadata | Nano real offline smoke completed with bundled default conditioning; owner voice, other variants, MPS/CUDA, Windows, and Daytona inference need qualification |
| 5. Finishing/export | Lossless master, edge fades, LUFS normalization, true-peak validation, MP3/WAV/8kHz profiles, decode checks, separate takes/manifests | Synthetic and real Nano output checked locally; owner message completeness and watermark regression are separate evidence |
| 6. API/queue/storage | Versioned API/generated TS contracts; SQLite/PostgreSQL; S3; leases, attempts, retries, cancellation, idempotency, durable completion, expiry | Current unit/contract tests pass; PostgreSQL and browser results recorded in their verification documents; remote storage recovery still needs live acceptance |
| 7. Daytona lifecycle | Launcher commands; controller/worker separation; private signed preview; secret mappings; idle/TTL handling; orphan recovery; resource checks | Fault-injection tests cover failures; actual account start/generate/download/recreate sequence awaits credentials and qualified images |
| 8. Shared UI | Create, Voice, History, Settings; explicit hosted transfers; progress phases; playback/export; controls and keyboard handling | Component/browser checks exercise real local API plumbing with labeled test inference; desktop clean-machine workflow remains a gate |
| 9. Native runtime | Tauri process/credential management; platform/device inspection; signed, bounded runtime packs; atomic install; local token; rollback/update handling | Native Rust compile/tests verified; actual preview builds reported separately; signed first-use/offline Windows/macOS acceptance pending |
| 10. Access/lifecycle | Owner login/throttling; private sessions; API-key revocation; origin checks; bounded uploads; expiry/deletion including S3 versions; child-secret isolation | Tested locally, including deleted/expired content denial; live infrastructure permissions and native OS credential behavior require deployment checks |
| 11. Release pipeline | Source/dependency inventories; CPU Dockerfile; PyInstaller packaging; native signing/notarization; preview/stable workflows; cryptographic release gates | No stable release is authorized by source tests. Missing signing credentials or qualification evidence blocks stable artifacts |
| 12. Pilot/publication | Benchmark reports, cost forecast across all categories, compute admission limit and spend alerts | Actual owner pilot, measured hosted bill, approved budget, and publication are outstanding |

## Required inputs to finish external acceptance

1. Local path to the authorized owner reference and agreed voicemail scripts.
2. Daytona account configuration and quota; independent PostgreSQL and private S3 settings.
3. macOS Developer ID/notarization and Windows signing credentials, runtime/update signing keys, and release artifact hosting.
4. Clean macOS and Windows test environments and owner review of all acceptance takes.

Do not enter secret values into this document or chat. Supply configuration paths or environment-variable names. Source-level gaps continue to be fixed without waiting for these inputs; evidence-dependent gates remain explicitly pending.
