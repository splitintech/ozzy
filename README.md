# Voice Studio

A private, MIT-licensed studio for turning an authorized reference recording into new English speech. React/Vite supplies the shared browser and Tauri interface; separately installable Python packages provide Chatterbox inference, FastAPI, durable jobs, and the Daytona launcher.

**Status: engineering preview.** Core application paths and verification tooling are implemented. Stable distribution still requires owner-voice quality review, live Daytona qualification, and signed clean-machine macOS/Windows acceptance. See [the implementation audit](docs/implementation-audit.md) for evidence and unfinished gates. Existing SplitIn Voice-to-Prompt commits implement a separate transcription service; this repository does not alter that integration.

## Run locally from source

Prerequisites: Node 24, npm, [uv](https://docs.astral.sh/uv/getting-started/installation/), and FFmpeg/ffprobe. Python 3.11 is selected by the workspace. A packaged desktop runtime will include Python and FFmpeg.

```sh
npm ci
npm run build
uv sync --locked --all-packages --extra inference
uv run --no-sync voice models install nano
uv run --no-sync voice local --device cpu
```

Open the displayed loopback URL and enter its per-launch access key. Import a recording you are authorized to use, select a clean excerpt longer than five seconds, generate an audition, listen, and approve the voice. Enter a script in Create, generate a take, and export MP3 or WAV. Advanced model controls remain optional. For a portable voice export, unzip `voice-reference.zip` and import its `reference.wav` explicitly into the other workspace.

Model downloads are approximately 3.0 GB for Nano, 4.0 GB for Turbo, and 3.2 GB for Original, plus temporary cache/staging space. Source revisions, model file checksums, and dependency resolutions are committed. Generation uses local model files with network-disabled Hugging Face settings. Local failures do not switch to hosted inference.

For UI development, run `uv run --no-sync voice local --dev` and `npm run dev` in separate terminals. For API-only development and ordinary tests, `uv sync --locked` omits inference dependencies. After changing dependency modes, use the explicit inference sync command again before generating.

## Desktop development

Install the platform's [Tauri prerequisites](https://v2.tauri.app/start/prerequisites/) and the pinned Rust toolchain. Build on the target OS; Windows installers cannot be qualified from macOS.

On macOS, after the source setup above:

```sh
VOICE_DEV_EXECUTABLE="$PWD/.venv/bin/voice" npm run desktop:dev
```

On Windows PowerShell:

```powershell
$env:VOICE_DEV_EXECUTABLE = (Resolve-Path .venv/Scripts/voice.exe).Path
npm run desktop:dev
```

The development executable override is compiled out of release builds. Production runtime downloads require the Ed25519 public key embedded at build time. The application checks signatures, exact archive size, checksum, platform, extraction paths, disk space, and atomic activation. A native credential store holds the Daytona key. The embedded webview receives neither that key nor the loopback service token.

See [release packaging](docs/release.md) for runtime packs, preview installers, signing, notarization, updates, rollback, and the stable-release gate.

## Hosted operation

Daytona runs the controller and inference worker on demand. Metadata uses independent PostgreSQL; private objects use independent S3-compatible storage. The launcher must start the controller before requests can be accepted. There is no always-on gateway.

```sh
voice cloud start --snapshot YOUR_CONTROLLER_SNAPSHOT --secret-names '{"VOICE_DATABASE_URL":"your-database-secret-name"}'
voice cloud status
voice cloud stop
```

That one-key map is only a syntax example. Configure the complete controller/worker secret mapping and pinned images using [the deployment runbook](docs/deployment.md). Keep configured credentials out of shell history; `.env` and OS credential storage are supported. `voice cloud start` returns an expiring port-scoped URL, which is sensitive. The browser then requires the owner's application login.

Hosted scripts and output objects expire after thirty days. References remain until deleted. Local history has no automatic expiration. Switching workspaces does not transfer scripts or recordings; import is an explicit action.

## API and CLI

- `voice local`: loopback API and shared UI.
- `voice models install original|turbo|nano`: pinned, integrity-verified model installation.
- `voice models status`: deep integrity checks.
- `voice cloud start|status|stop`: on-demand Daytona sessions.
- `voice generate --text "Your message" --voice-id VERSION_ID --output message.mp3`: submit, poll, and download; use `VOICE_API_KEY` or `VOICE_LOCAL_TOKEN`.
- `voice password-hash`: hidden-input Argon2 owner password setup.
- `voice doctor --check-acceleration`: inspect runtime dependencies and available devices.

The authenticated API uses `/v1/voices`, `/v1/generations`, `/v1/artifacts`, and `/v1/capabilities`. Generation returns HTTP 202 and a job ID. Polling does not refresh owner activity. A generation is completed only after verified artifacts are stored and the current attempt wins the database completion transaction. Cancellation terminates inference. API keys are revocable.

[OpenAPI](contracts/openapi.json) generates the TypeScript contracts used by the client:

```sh
uv run python scripts/export_openapi.py
npm run client:generate
```

## Verification

```sh
uv sync --locked
uv run pytest tests deploy/tests
npm run check
npm run test:e2e
cargo test --locked --manifest-path apps/desktop/src-tauri/Cargo.toml
```

The ordinary suite uses synthetic audio and test engines solely for plumbing tests. It does not download models or assert voice quality. PostgreSQL tests require `VOICE_TEST_POSTGRES_URL` and create isolated schemas; browser tests use a disposable service. Live inference qualification uses authorized reference audio and the separate [benchmark procedure](docs/qualification.md).

## Structure

```text
apps/studio/       Shared React interface and generated API contracts
apps/desktop/      Tauri shell, native HTTP, credentials, runtime/update verification
python/core/       Contracts, configuration, migrations, audio, SQLite/PostgreSQL, S3
python/engine/     Pinned model installation and serialized Chatterbox adapter
python/service/    Authenticated API, durable queue, worker, Daytona lifecycle
python/cli/        Owner launcher and command-line client
deploy/            CPU images, native packaging/signing, source inventory
benchmarks/        Twenty-script qualification set and cost input example
scripts/           Contract generation, benchmarks, qualification and release gates
```

The application source is MIT. Chatterbox, Perth, PyTorch, FFmpeg, model assets, and other dependencies retain their own licenses and notices. Runtime releases include inventories and required redistributable notices; see [third-party notices](THIRD_PARTY_NOTICES.md). Reference recordings and generated private audio must never be added to source releases.
