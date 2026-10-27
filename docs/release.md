# Release and native runtime runbook

Stable is a qualification state, not a successful compiler invocation. No stable publication is authorized by ordinary unit tests, a synthetic voice smoke test, or this document. All release tools leave external publication to the operator.

## Artifact and toolchain contract

Commit `uv.lock`, `package-lock.json`, and `apps/desktop/src-tauri/Cargo.lock`. Use Python 3.11, the repository Rust toolchain, Node 24 and the versions resolved in those locks. Build macOS Apple Silicon on arm64 macOS and Windows x64 on x64 Windows. PyInstaller is not a cross-compiler. Model files are pinned by `model-lock.json` and are downloaded separately; references and generated audio must never enter installers.

The stable release set contains source distributions/wheels, source archive, dependency notices, CycloneDX SBOM, lockfiles/model manifest, platform runtime ZIPs and signed runtime manifests, DMG and NSIS EXE, updater artifacts/signatures, release notes and qualification evidence. `deploy/build_sources.py` creates source artifacts/inventories and checksums from a committed checkout. Its lock inventory includes development dependencies; it is not a substitute for reviewing actual bundled dependencies and FFmpeg distribution obligations.

## Ordinary verification and previews

`ci.yml` runs lightweight Python contracts, React checks, native Rust tests and source packaging on both native platforms without downloading models. `containers.yml` builds the lightweight controller and pinned CPU worker and verifies their dependency separation; it does not push images. `preview.yml` builds native engine packs and installers only on manual dispatch.

Provide reviewed ZIP URLs and SHA-256 hashes in `FFMPEG_MACOS_URL`, `FFMPEG_MACOS_SHA256`, `FFMPEG_WINDOWS_URL`, and `FFMPEG_WINDOWS_SHA256`. Archives must contain exactly one ffmpeg and ffprobe, plus distribution license/notices and any source-offer material. `deploy/fetch_ffmpeg.py` checks the hash, archive limits and path safety, then atomically installs only the binaries and notices. Record the source provenance. Do not assume a local Homebrew binary is redistribution-ready.

Set `RUNTIME_URL_BASE` to the intended immutable HTTPS download location. `scripts/build_runtime.py` constructs the native PyInstaller directory, embeds FFmpeg, model metadata and dependency notices, then runs the packaged `doctor` and native-import/FFmpeg self-test. It validates every ZIP member, rejects symlinks/escapes, model/user audio files and duplicate case-folded names, and writes byte sizes and SHA-256. The Rust installer separately verifies the signed manifest, platform, size, checksum and extraction paths before atomic activation.

Preview installers use `deploy/tauri.preview.conf.json`, a distinct application identity and visible **Voice Studio Preview** name. They remain unsigned and unqualified. The production installer deliberately rejects unsigned runtime manifests; a developer must configure a separate preview verification key and sign a preview manifest to exercise downloaded preview installation. Do not reuse production signing keys or claim unsigned packs can pass the stable installer.

For a local-only packaging smoke check, `build_runtime.py build --development --no-pack ...` permits locally available FFmpeg without distribution notices. It writes a `DEVELOPMENT_ONLY_NOT_REDISTRIBUTABLE.txt` marker. Such a directory cannot pass archive packaging/release gates. It is useful solely for proving frozen dependencies without a user's Python or shell tools.

The build now runs `deploy/smoke_runtime.py` automatically: it clears `PATH`,
imports the packaged inference stack, checks bundled FFmpeg, starts the frozen
loopback API, and queries authenticated capabilities. With `--models`,
`--reference`, and `--authorized`, the smoke tool additionally imports the test
recording, auditions and activates it, generates and checksum-verifies every
export, deletes the test voice, verifies denied reads, and runs a separate
inference process with socket connections blocked. This is engineering
verification, never human voice approval.

PyInstaller collects Librosa/Resampy sources for JIT/lazy imports and transitive
inference distribution metadata. Numba caches live outside the signed bundle.
Before smoke testing/signing, the macOS audit removes stale build-only absolute
rpaths and refreshes ad-hoc signatures, rejects external library dependencies,
and checks the app's advertised minimum macOS against all native libraries.
Development bundles report their actual requirement. The local Homebrew-based
test runtime requires macOS 26 and is not a macOS 13 distribution artifact.

For a headless **preview-only** DMG without Finder automation:

```sh
npm run desktop:build -- --config ../../deploy/tauri.preview.conf.json --bundles app
uv run python deploy/build_preview_dmg.py \
  "apps/desktop/src-tauri/target/release/bundle/macos/Voice Studio Preview.app" \
  "dist/preview/Voice Studio Preview.dmg"
```

The helper checks the preview application name, creates a new DMG, verifies its
disk-image checksum, and emits a checksum/status record. It does not establish
OS signing, notarization, or clean-machine acceptance. Use Node 24.13.0 (`nvm use`
reads `.nvmrc`). Standard signed installers use the native candidate workflow.

Source release packaging requires a clean committed checkout, preventing wheels
built from working files from being paired with an older `git archive HEAD`.
Use `uv build --all-packages` for development wheels/sdists, or
`deploy/build_sources.py --inventory-only` for notices without a source release.

`stable-candidate.yml` runs only on manually dispatched, protected `stable-release` jobs on pre-provisioned native signing runners tagged `voice-signing`. Provision OS certificates and the Apple notary keychain profile on those isolated runners; do not attach them to pull request jobs. It builds/signs candidate packs and apps, notarizes macOS artifacts, and uploads **UNQUALIFIED-SIGNED-CANDIDATE** artifacts without publishing. Qualification still follows.

Set `RUNTIME_MANIFEST_MACOS_URL` and `RUNTIME_MANIFEST_WINDOWS_URL` to platform-specific HTTPS signed-manifest locations; the workflow compiles the selected value as `VITE_RUNTIME_MANIFEST_URL`, giving first-use installation a default without requiring users to paste technical URLs. Configure the independent updater URL/key separately. `deploy/candidate_config.py` rejects missing trust keys or non-HTTPS release locations.

## Build stable candidates

1. Freeze the reviewed source commit, version and monotonically increasing runtime sequence. Complete genuine benchmark, owner voice review, cloud lifecycle and clean-machine qualification records for both OSes.
2. Generate source artifacts and notices from the same commit. Download the reviewed FFmpeg distribution using its pinned hash. Run `npm ci`, `npm run check`, locked Python tests and Rust tests.
3. Build each native runtime with `scripts/build_runtime.py build --no-pack --ffmpeg-dir ... --version ... --sequence ... --url ...`. Do not sign a directory that includes developer recordings, caches or credentials.
4. Sign and verify runtime native libraries/executables using `deploy/native_sign.py --bundle dist/runtime/native/voice --log ...`. On macOS provision a Developer ID Application identity in the keychain and `APPLE_SIGNING_IDENTITY`. On Windows provision the certificate and set `WINDOWS_CERTIFICATE_THUMBPRINT`, `WINDOWS_TIMESTAMP_URL` and, if necessary, `SIGNTOOL_PATH`. A timestamp service must use HTTPS. Certificate secrets never belong in repository files.
5. Pack the signed directory with `build_runtime.py pack`. Submit the macOS runtime ZIP to `deploy/notarize.py`; retain Apple's actual Accepted response. ZIPs cannot be stapled. The macOS installer/app must separately be notarized and stapled.
6. Sign the manifest with `scripts/sign_runtime.py MANIFEST --archive ZIP --output SIGNED_MANIFEST`. Provide a base64 raw 32-byte Ed25519 private key through `VOICE_RUNTIME_PRIVATE_KEY` and its matching public key through `VOICE_RUNTIME_PUBLIC_KEY`. The public key is compiled into the desktop. The manifest signs the exact ZIP bytes; never modify a pack afterward.
7. Build Tauri installers using its documented OS signing configuration. Compile `VOICE_RUNTIME_PUBLIC_KEY`, a **different** `VOICE_UPDATE_PUBLIC_KEY` and the approved HTTPS `VOICE_UPDATE_URL` into the app. Set Tauri's updater signing environment separately. Verify macOS Developer ID/notarization and Windows Authenticode signatures on the resulting installer and every downloaded executable component.
8. Install these exact candidates on clean machines. Repeat local generation with networking disabled after initial setup. Exercise startup, import, audition, approval, generation, playback, both exports, deletion, update and runtime rollback. A development-machine launch alone does not meet this gate.
9. Assemble the evidence below and run `stable-gate.yml` in the protected `stable-release` environment. Publication is a separate operator step after this gate succeeds; this repository never automatically publishes on a push.

## Evidence schema and trust

Every file reference has the form `{"path":"relative/path","sha256":"64 lowercase hex"}`. Paths must resolve inside the evidence directory and match the actual bytes. Retain private reference/audio evidence in controlled storage; publish a redacted report rather than voice recordings.

`qualification.json` requires `schema:1`, `version`, exact 40-character source `commit`, `owner_authorized:true`, a nonempty `reviewed_at`, and references named `scores`, `hosted`, `hosted_run_log`, `recovery_log`, `cost_report`, and `model_lock`. The benchmark evaluator requires twenty scripts, three seeds per script, naturalness/similarity averages at least 4/5, correct critical words, complete text, no unwanted repetition, owner approval, and p95 ≤30 seconds for the agreed 30–60 second messages, including durable artifact publication. Cold-start measurements are separate.

Its `native` map must have `aarch64-apple-darwin` and `x86_64-pc-windows-msvc`, each referencing a JSON record with matching `platform` and `commit`, a referenced `log`, the exact `runtime_archive` and `installer` file references, and `checks` containing all of: `install`, `launch`, `reference_import`, `generate`, `play`, `export_wav`, `export_mp3`, `restart`, `delete`, `runtime_rollback`, `offline_generation`, `update`, `signature_verification`. Each must be true based on an actual recorded run.

`signatures.json` requires matching `version` and `commit`, and `platforms` entries for both targets. Each references `runtime_archive`, signed `runtime_manifest`, `installer`, `native_signatures` and `updater`. Native signature evidence must have matching `platform`, `verified:true`, `runtime_sha256`, `installer_sha256`, `updater_sha256`, and referenced `runtime_verify_log`/`installer_verify_log`. Windows additionally requires `authenticode_valid:true`; macOS requires a referenced `notarization` record with accepted runtime/installer status and references to both actual Apple submission responses.

The updater record references its `archive`, `.sig` `signature` and `verification_log`, with `verified:true` and `public_key`. The gate cryptographically verifies the Minisign payload and trusted comment against the operator-configured `VOICE_UPDATE_PUBLIC_KEY`; the key supplied inside evidence alone is not trusted. Runtime signatures use the independently configured `VOICE_RUNTIME_PUBLIC_KEY`. Qualified native artifacts must match signed artifacts exactly. Missing keys, missing evidence, hash mismatches or absent checks fail closed.

The gate verifies integrity and completeness; it cannot replace human provenance review of quality scores or operating-system command logs. Restrict evidence-run creation and protected-environment approval to release maintainers. `--qualification-only` explicitly reports `stable_approved:false` and never authorizes publishing. Preview status always reports `stable_approved:false`.

## Updates and rollback

Downloaded runtimes must pass their frozen import/FFmpeg readiness check before
activation. Install/rollback share a process and filesystem lock. A separate
highest-sequence record survives explicit rollback; reusing a release sequence
for different bytes is rejected. Reinstalling identical verified bytes repairs
damaged files while preserving the previous distinct version for rollback.

Keep the prior verified runtime available until the new runtime passes readiness. Installer interruption or checksum/signature failure must leave the old runtime intact. Runtime manifests use monotonic sequences to reject unintended downgrades; use the application's explicit rollback path for a previously trusted installed version. Retain previous signed installer artifacts, immutable model revisions and schema-compatible source snapshots. Test key rotation and update rollback on clean machines before enabling a new release feed.

Official references: [Tauri sidecars](https://v2.tauri.app/develop/sidecar/), [macOS signing](https://v2.tauri.app/distribute/sign/macos/), [Windows signing](https://v2.tauri.app/distribute/sign/windows/), [Tauri updater](https://v2.tauri.app/plugin/updater/), [PyInstaller operating model](https://pyinstaller.org/en/stable/operating-mode.html), [GitHub native runner architectures](https://docs.github.com/en/actions/reference/runners/github-hosted-runners).
