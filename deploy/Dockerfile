# syntax=docker/dockerfile:1
# Both bases are immutable multi-platform manifest digests.
FROM node:24-bookworm-slim@sha256:a5f4366e834695d5f363b920c104953a535d3a9864d1bdf74566a0ebe01daee5 AS studio
WORKDIR /opt/voice
COPY package.json package-lock.json ./
COPY apps/studio/package.json apps/studio/package.json
COPY apps/desktop/package.json apps/desktop/package.json
RUN npm ci
COPY apps/studio/ apps/studio/
RUN npm run build

FROM python:3.11-slim-bookworm@sha256:0a310eeecf4e1f5a0743f9a6520c90c88d089c903ca5fd283f501e3a805f5f89 AS base
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PATH=/opt/voice/.venv/bin:$PATH UV_PYTHON_DOWNLOADS=never \
    VOICE_STATIC_DIR=/opt/voice/studio VOICE_MODEL_DIR=/opt/voice/models \
    VOICE_DATA_DIR=/tmp/voice-data VOICE_DEVICE=cpu \
    HF_HUB_DISABLE_TELEMETRY=1 OMP_NUM_THREADS=4 MKL_NUM_THREADS=4
WORKDIR /opt/voice
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates ffmpeg libgomp1 libsndfile1 tini bash git build-essential \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir uv==0.11.7 \
    && groupadd --gid 1000 daytona \
    && useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash daytona
COPY pyproject.toml uv.lock ./
COPY python/ python/
COPY LICENSE /opt/voice/LICENSE
COPY --from=studio /opt/voice/apps/studio/dist/ /opt/voice/studio/
# The SDK starts named process sessions. Do not replace PID 1 with voice serve/worker.
# A passed command (including a Daytona toolbox bootstrap) is executed unchanged.
COPY --chmod=755 deploy/entrypoint.sh /usr/local/bin/voice-entrypoint
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/voice-entrypoint"]
CMD ["sleep", "infinity"]
EXPOSE 8765

FROM base AS controller
RUN uv sync --locked --all-packages --no-dev --no-editable \
    && .venv/bin/python -c "import importlib.util; assert importlib.util.find_spec('torch') is None" \
    && rm -rf /root/.cache \
    && mkdir -p /opt/voice/models \
    && chown -R daytona:daytona /opt/voice
ENV VOICE_ROLE=controller VOICE_BIND_HOST=0.0.0.0
USER daytona

FROM base AS worker
RUN uv sync --locked --all-packages --extra inference --no-dev --no-editable \
    && .venv/bin/python -c "import torch; assert torch.version.cuda is None; from chatterbox.tts_turbo import ChatterboxTurboTTS" \
    && VOICE_MODE=local .venv/bin/voice models install nano \
    && .venv/bin/python -c "from voice_studio_engine.models import verify_model; assert verify_model('/opt/voice/models', 'nano')" \
    && rm -rf /opt/voice/models/.download-cache /root/.cache \
    && chown -R daytona:daytona /opt/voice
ENV VOICE_ROLE=worker HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
USER daytona
