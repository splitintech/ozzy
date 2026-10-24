# Browser and shared interface verification

The shared React interface is exercised with component tests and Chromium at desktop (1440 × 1080) and Pixel 7 viewport sizes. The browser suite launches a disposable loopback FastAPI service, real SQLite persistence, the real queue and audio finishing pipeline, and a clearly labeled synthetic tone engine. It does not download models or establish voice quality, hosted speed, or desktop OS integration.

## Reproduce

Use Node 24 and the repository's Python 3.11 development environment:

```sh
npm ci
npm run check
npx playwright install chromium
npm run test:e2e
```

Browser artifacts, screenshots, exported files, failure traces, and the HTML report are written beneath `artifacts/browser/` (ignored by Git). The server binds only `127.0.0.1:18765`, uses an explicitly test-only access key and temporary data directory, and shuts down after the suite.

The workflow covers reference import, authorization, excerpt selection, audition playback before approval, message submission, model-specific controls, no autoplay, MP3/WAV/8 kHz downloads, immutable takes, reference ZIP export, history deletion, API-key creation/revocation, invalid audio, authentication recovery, and keyboard navigation. Modal tests verify focus trapping, Escape cancellation, and focus restoration after deletion. Axe checks the initial workspace against WCAG 2 A/AA, 2.1 AA, and 2.2 AA rules; this automated check supplements manual keyboard and screen-reader qualification and is not a claim of complete WCAG certification.

Component tests additionally cover local-to-hosted workspace isolation, clearing selected private recordings on mode switch, explicit runtime download after size/disk inspection, detected CPU/MPS/CUDA choices, signed update actions, update failures, budget alerts, and a fresh unauthenticated connection without an erroneous expiry warning. Native calls are mocked here; native package tests and clean-machine acceptance remain separate.

## Runtime download defaults

Signed desktop builds set `VITE_RUNTIME_MANIFEST_URL` to the platform-specific signed runtime manifest URL when building the shared frontend. This value is public release configuration, never a credential. The owner reviews the verified download size, required free space, and platform before starting installation. The manual URL override lives under **Advanced download source**. Previews without a configured URL explain that the release manifest must be supplied. Installed runtimes are inspected through the native `runtime_status` command; CPU is the default and only reported accelerators are selectable.

## Current run

Verified 2026-10-24 on Apple Silicon macOS, Node 24.13.0, Playwright 1.64.1, Chromium revision 1248:

- `npm run check`: TypeScript passed, 12 component/native-boundary tests passed, production build passed.
- `npm run test:e2e`: all 4 scenarios passed in 1.9 minutes across desktop and mobile viewports.
- Automated WCAG-tagged checks on the initial workspace found no violations after correcting secondary-text contrast.
- Desktop and mobile screenshots were inspected for the generated-message layout. Both retained visible navigation, reference management, playback, and export controls without horizontal overflow.
- The end-to-end run exercised actual authenticated file downloads and checked WAV headers/native and telephone sample rates. Audio remained a synthetic fixture, not an owner voice.

Browser testing caught and corrected successful-deletion focus loss, low-contrast secondary text, and the model selector test's implicit-label mismatch. It also exposed a concurrent local storage cleanup race, fixed and regression-tested in the Python storage package. Earlier recovered test records are not counted as current proof.

Current screenshots include `artifacts/browser/chromium-desktop-generated.png`, `artifacts/browser/chromium-mobile-generated.png`, and Create/Voice/History/Settings views for each viewport. The HTML report is `artifacts/browser/report/index.html`.
