# Local verification evidence

## Current local engineering verification — October 25, 2026

**Implemented locally on Apple Silicon.** This denotes implemented engineering
code and the checks below, not the plan's human, hosted, Windows, or signed-release
acceptance. The historical October 24 results remain below for traceability.

| Check | Current result |
|---|---|
| Locked lightweight workspace | Offline `uv sync --locked --all-packages`; API imports with Torch absent |
| Python unit/contract/packaging suites | **135 passed**, five PostgreSQL cases skipped here and run separately |
| PostgreSQL 14 | **5 passed** on a new UTF-8 database with private Unix socket; server stopped afterward |
| React | TypeScript, **13 component/native-boundary tests**, production build passed |
| Browser | **4 Chromium scenarios** passed at desktop/mobile sizes, including keyboard/accessibility checks and API exports |
| Native Rust | **7 tests** passed, including activation failure recovery, repair, and anti-replay after rollback |
| Static checks | Ruff and Rust formatting passed |
| Python packaging | All four packages built wheels and source distributions from the current working files |
| macOS application | Current Tauri release-mode **preview .app** built |
| Preview disk image | Headless DMG built and `hdiutil verify` passed; no OS-signing qualification claimed |
| Frozen runtime | Final PyInstaller development build passed automatic cleared-PATH imports/FFmpeg/local API smoke |

Reports live in `artifacts/verification/`, with browser evidence under
`playwright-report/`/`test-results/`. Native build artifacts are development
previews. The frozen runtime uses local Homebrew FFmpeg libraries and its native
dependency audit requires **macOS 26.0**, so it is deliberately excluded from
distribution packaging. Release builds now reject a dependency requiring a newer
OS than the advertised minimum.

The earlier repaired packaged runtime completed the workflow recorded in
`artifacts/frozen-workflow/report.json`. It uses a twelve-second reference derived from previously
application-generated synthetic audio. It imports, auditions, activates,
generates, downloads WAV/MP3/telephone/master/manifest, verifies checksums, deletes
the voice, and confirms that deleted audio returns 404. The smoke separately
generates through the frozen inference runner with all socket connections blocked.
These checks establish the runtime wiring, not voice similarity, spoken-content
accuracy, or the 30–60-second performance gate.

The final rebuilt runtime passed import/API smoke, but its initial four-thread
full-workflow replay timed out after 900 seconds during audition under observed
heavy host memory pressure. This is retained in
`artifacts/verification/frozen-final.log`; it is not a passing inference result.
The saved final runtime then passed the complete replay with one CPU thread: import, audition, activation, generation, all five exports, deletion, and separate frozen inference with socket connections blocked. Evidence is in `artifacts/frozen-final-cpu1/report.json`. Both the failed four-thread run and successful one-thread run are retained; neither is a hosted performance benchmark.

The initial PostgreSQL test cluster defaulted to SQL_ASCII; driver setup failed
before tests ran. The retained final run used explicit UTF-8. Earlier frozen
packaging failed on Librosa/Numba source lookup and missing transitive package
metadata; both defects were reproduced and fixed. An older Node executable was
also detected; checks use the pinned Node 24.13.0, now available through `.nvmrc`.

Remaining acceptance inputs: the owner's authorized recording and quality
review; live Daytona quota/images plus independent PostgreSQL/S3 recovery and
costs; Windows/MPS/CUDA environments; release-compatible FFmpeg, signing keys,
notarization, and clean-machine install/update/rollback/offline checks. No live
Daytona session, paid provisioning, public release, push, or new commit occurred.

## Running the saved local development build

From this repository on the tested Apple Silicon/macOS 26 host:

```sh
VOICE_MODE=local VOICE_MODEL_DIR="$PWD/models" VOICE_CPU_THREADS=1 \
  ./dist/runtime-development/voice/voice local
```

Open the loopback URL printed by the process and enter its per-launch local
access key. Keep that key private. This directory bundle includes Python,
FFmpeg, and the built web interface; Nano weights are separately stored under
`models/`. The preview `.app`/DMG remains subject to signed-runtime first-use
setup and is not a substitute for this local development command.

Reproduce the packaged integration check with the private synthetic fixture:

```sh
python3 deploy/smoke_runtime.py dist/runtime-development/voice/voice \
  --output artifacts/frozen-recheck --models models \
  --reference artifacts/engine-conditioning/reference.wav --authorized --cpu-threads 1
```

## Historical verification — October 24, 2026

Date: 2026-10-24. Host: Apple Silicon Mac14,7, 8 GiB RAM, macOS 26.6.2. The host also ran unrelated user work and simultaneous native builds. These results establish local behavior, not hosted performance or clean-machine distribution acceptance.

## Completed checks

- Python: the final full suite passed **115 tests**, with five PostgreSQL cases intentionally skipped in that invocation (77.27 seconds). The retained JUnit file is `artifacts/verification/python-tests.xml`. The five real PostgreSQL 14 cases separately passed against isolated schemas in a disposable private-socket database; that database was stopped afterward.
- Cloud: 47 provider-free lifecycle/fault-injection cases passed using the pinned Daytona SDK. No paid account allocation occurred.
- Queue: 13 tests include startup lease protection, cancellation during reference download/upload, process termination, retries, idempotency and stale-attempt fencing.
- Frontend: TypeScript, 12 component/native-boundary tests and production build passed. Four Chromium browser scenarios passed across desktop/mobile viewports, including actual authenticated exports and deletion. Initial-workspace Axe checks passed; complete screen-reader/WCAG qualification remains outstanding.
- Native shell: Rust formatting and four native tests passed, covering request-origin confinement, archive traversal/expansion, and signed-manifest tampering/validation.
- Packaging: fourteen release-tool tests passed. Missing qualification/signature evidence correctly rejects stable approval.
- Dependency separation: importing the installed lightweight API succeeded while PyTorch was absent from that environment. Inference dependencies were installed into a separate environment.
- Source release: all four packages produced source distributions and wheels, plus a tracked-source archive, CycloneDX inventory, dependency notices, lockfiles and checksums. These are in the standalone repository's ignored `dist/sources/` directory.
- Clean wheel installation: the four built wheels installed into a new isolated environment using exported locked constraints. Packaged model/schema resources loaded, SQLite migrations initialized, health and authenticated capabilities succeeded, unauthorized requests were denied, and PyTorch remained absent.
- Ruff passed across Python packages, tests, deployment and verification scripts.

## Real offline inference

Nano model revision `71ccd1d0081b430592cea481f4307e764e07bc64` was downloaded and checksum-verified. Source revision is `5de7a54aa4e5e2baadb0182dde554908b48b85c2`; inference used CPU with four Torch threads. Socket connections were blocked in the smoke processes, in addition to offline Hugging Face settings.

1. Bundled-conditioning smoke: model load 70.98 seconds, generation 19.27 seconds, output 2.08 seconds. Exported mono WAV at 24 kHz PCM16, mono MP3 at 44.1 kHz/128 kbps, and 8 kHz PCM16 telephone WAV all decoded. WAV measured −19.01 LUFS/−3.63 dBTP; MP3 measured −19.45 LUFS/−4.09 dBTP. The native-rate float master was retained.
2. Reference-conditioning smoke: previously application-generated synthetic audio was repeated into a twelve-second reference. The actual portable engine prepared conditioning, generated two segments, and produced 2.92 seconds of output plus all exports. Under heavy concurrent host work, load/reference preparation took 499.08 seconds and generation took 228.48 seconds. These timings are unsuitable as an isolated hardware benchmark. WAV/MP3/telephone peaks were at or below −1 dBTP; measured loudness was −19.66/−20.12/−20.26 LUFS respectively. Peak limiting can leave an export quieter than the nominal −19 LUFS target.
3. Watermark regression: the pinned Perth decoder returned an unrounded response of 1.0 on the first smoke's master and three export profiles. This is one observed sample, not a calibrated guarantee of watermark detectability under every transformation.

Neither smoke used the owner's reference or established naturalness, similarity, critical-word accuracy, or complete spoken content by human review. Neither is a 30–60-second performance acceptance set. Reports and private synthetic outputs remain in `artifacts/engine/` and `artifacts/engine-conditioning/`, outside Git.

## External acceptance still required

- Authorized owner reference, actual voicemail scripts, multi-seed quality review and Original/Turbo/Nano selection.
- Live Daytona quota/image/resource validation, independent PostgreSQL/S3 recovery, complete cost measurement and owner-approved budget.
- Docker image builds: the local Docker CLI is installed, but its daemon socket is unavailable.
- Signed/notarized macOS and Windows installers/runtime components, clean-machine first use, offline generation, signature verification, update and rollback.
- Public release configuration and hosting. No source push, image publication, stable release or production deployment occurred.

Native preview build results are recorded separately when completed. Preview artifacts do not satisfy the signed clean-machine gate.
