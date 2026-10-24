# Contributing

Use Python 3.11, Node 24, the checked-in Rust toolchain, and the npm/uv/Cargo lockfiles. Run the README's checks for the affected components before submitting a change. Keep the API import path free of PyTorch; inference imports belong in the engine subprocess.

Changes to public request/response schemas require regeneration of `contracts/openapi.json` and `apps/studio/src/api.generated.ts`. Schema migrations are ordered and transactional; never rewrite a migration after a released version has used it. Add a new migration and test upgrade/restart behavior.

Model/source updates require immutable source commits, exact model revisions and hashes, dependency lock updates, offline-load checks, and renewed quality/performance qualification. Do not substitute a development-branch feature for a feature absent from the pinned source.

Tests without models must remain fast and independent of network access. Use generated synthetic signals for audio pipeline tests, clearly named test engines for queue tests, and separately authorized recordings for inference tests. Do not commit voices, scripts containing private information, model weights, credentials, signing keys, local databases, or diagnostic dumps that include these items.

Respect the separation of local and hosted workspaces. No fallback may upload local data implicitly. Model inference must be serialized, cancellation must stop computation, and stale attempts must never publish output. Frontend changes must preserve keyboard navigation, labeled controls, phase-based progress, and explicit hosted transfers.

Source changes alone cannot mark a deployment or stable release qualified. Attach measured evidence for platform, runtime revision, model revision, owner quality review, offline generation, lifecycle recovery, signatures, updates, and rollback as appropriate.
