# Qualification procedure

Source tests, real inference smoke tests, owner voice review, hosted performance, and signed clean-machine acceptance are separate evidence. A unit test cannot substitute for the other gates.

1. Obtain the owner's authorized MP3/WAV and freeze a clean reference excerpt. Keep it out of Git and public CI artifacts.
2. Install Original, Turbo, and Nano using `voice models install MODEL`. Compare the same reference, scripts, seeds, and finishing version.
3. After installing inference dependencies, run:

   ```sh
   uv run --no-sync python scripts/benchmark.py --reference /private/path/reference.mp3 --authorized --model nano --device cpu --cpu-threads 4 --output artifacts/nano-cpu4
   ```

4. Review all twenty scripts across the three default seeds. Fill `scores.json`: naturalness, similarity, critical-name/number correctness, completeness, repetition, and owner approval. Keep generated audio private. If actual speech lengths fall outside the agreed 30–60-second range, revise and freeze the script set before qualification.
5. Compare four CPU threads first. Eight threads require verified account resources. Choose a smaller CPU profile if it still passes. Compare a GPU only after CPU results justify it. A CUDA source path does not establish GPU qualification.
6. Run hosted start → generate → download → stop → recreate → recover using independent PostgreSQL/S3. Record sandbox creation, loading, reference preparation, inference, finishing, upload, teardown, peak memory, and provider-billed durations. The local benchmark excludes sandbox startup, S3 upload, and external services.
7. Supply hosted evidence with `ready_to_artifacts_seconds`, `recovery_passed`, `start_generate_download_stop_passed`, `cost_accounting_complete`, and `owner_budget_approved`. Samples must cover the whole set with a ready engine and no earlier queued work:

   ```sh
   uv run python scripts/check_qualification.py artifacts/nano-cpu4/scores.json --hosted /private/path/hosted-evidence.json
   ```

8. Require naturalness/similarity averages ≥4/5, every critical word/number correct, complete messages, no unwanted repetition, owner approval, and hosted warm p95 ≤30 seconds. Record cold startup and queue delays separately.
9. Replace all example costs/durations in `benchmarks/cost-input.example.json` with pilot measurements. Keep inference, startup/teardown, and retry durations non-overlapping; include database, storage, and transfer. Run `scripts/cost_forecast.py`. Example zero external-service costs are not measured costs. API admission accounting estimates compute; provider billing remains authoritative.
10. On clean Apple Silicon macOS and Windows x64, test first installation, interrupted/corrupt downloads, low disk, engine/app crash, reboot, reference import, export playback, network-disabled generation, OS signatures, updater signatures, rollback, deletion, and explicit workspace transfer. Preserve revision-bound evidence for the stable gate.

After reviewing the Original/Turbo/Nano comparison, assemble a private JSON array
of measured candidates and run `uv run python scripts/select_profile.py
/private/path/candidates.json`. Each record contains a unique `id`, the full
`benchmark` report, reviewed `scores`, `hosted` evidence, and the `cost` forecast
including `cost_per_completed_message_usd`. Record an `unavailable_reason` when a
candidate could not be measured. Eight-CPU records also require
`eight_cpu_quota_verified: true`. The selector checks current model pins and the
quality/performance gate, reports rejection reasons, and selects the cheapest
eligible profile. GPU is ineligible when CPU passes or CPU review is missing.
The tool neither provisions resources nor approves a stable release, and does
not replace the requirement to compare all three models.

Generation metadata separates model loading and reference preparation. Job
timings additionally separate queue wait, reference retrieval, finishing, and
durable uploads. The hosted warm gate still includes processing and upload and
excludes preceding queue wait.

The original local Nano smoke uses the model's bundled default conditioning only to verify offline model loading and generation. It does not test the owner's voice or qualify hosted performance.

For an additional conditioning smoke, `scripts/engine_smoke.py --synthetic-source PATH` accepts previously application-generated synthetic audio and deliberately repeats it into a fixture reference. It exercises reference preparation, model conditioning, generation, and all exports with socket access blocked. Its report always leaves owner-voice, quality, and hosted qualification false. Do not substitute this fixture for an owner's reference.

Run `scripts/watermark_regression.py artifacts/engine` on a directory containing the master and three exports. It records the pinned Perth decoder's unrounded response for each file with networking disabled. These observations test export-processing regression on that sample; they are not a calibrated detection guarantee across recordings or transformations.
