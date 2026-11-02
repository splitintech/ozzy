# Build an in-app voice agent on Ozzy

Ozzy is the **voice**: clone an authorized reference, audition it, then speak scripts as that person. It is not a call graph builder, not Dograh/Vapi/Retell, and not OpenAI Realtime.

Your application owns the rest:

1. **Hear** — your STT (browser speech, Whisper, Speaches, …).
2. **Think** — your LLM as text. Do not send live audio to OpenAI Realtime.
3. **Speak** — Ozzy generates audio from that text with the approved voice.
4. **Carry** — your transport (in-app player, WebSocket, or a phone stream).

Hosts and end users never open this repository, Daytona, or model pickers. They record once, hear an audition, and approve. Your product UI can be a single panel.

## Why this is cheaper than OpenAI Live

OpenAI Realtime bills audio in and audio out for the whole session. A cascade bills a transcript, a short text completion, and one Ozzy take per turn. Ozzy runs Chatterbox on hardware you control.

Do not wrap Ozzy with Dograh Realtime mode. That puts the same OpenAI audio meter back on the invoice and adds a second agent builder.

## Locked Original profile

For an in-app agent, send Chatterbox **Original** with Resemble Labs’ published voice-agent defaults. Do not expose these as sliders.

| Field | Value |
| --- | --- |
| `model` | `original` |
| `exaggeration` | `0.5` |
| `cfg_weight` | `0.5` |
| `temperature` | `0.8` |
| `seed` | `42` |

CPU Nano remains the local studio default so a laptop can preview without a GPU. Product agents should still request Original.

## HTTP flow

Authenticate with `Authorization: Bearer` and an `Idempotency-Key` on mutations. Schema: [contracts/openapi.json](../contracts/openapi.json).

1. `POST /v1/voices` — multipart `file`, `name`, `authorized=true`, `start`, `end` (excerpt longer than 5 s and at most 30 s).
2. `POST /v1/generations` — `{ "voice_id", "text": "ignored", "purpose": "audition", "options": { …Original profile } }`. Poll `GET /v1/generations/{id}` until `completed`.
3. Play the audition artifact. If the speaker accepts it, `POST /v1/voices/{id}/activate` with `{ "audition_job_id", "approved": true }`.
4. Each in-app turn: your STT → your LLM (plain text, ≤ 2000 characters / 280 words) → `POST /v1/generations` with `purpose: "message"` and the same options → play `speech` or `telephone` (8 kHz µ-law / WAV) from the job artifacts.

One generation at a time per studio. Do not create a Daytona sandbox per user.

## What does not belong in Ozzy

- Organization membership, cookies, or product RBAC
- Phone carrier accounts, SMS, 10DLC, RCS
- OpenAI Realtime / GPT-Live WebRTC sessions
- Workflow graphs and tool-call builders

Those stay in the host application. A working client is in [examples/in-app-agent](../examples/in-app-agent).
