# syntax=docker/dockerfile:1
# Mobile Agent: web UI + orchestrator + A2A + vault + phone gateway. Emulator / BlueStacks / Lima phones are NOT inside the image (see docs/docker.md).
ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.10.12 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project

FROM python:${PYTHON_VERSION}-slim AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends android-tools-adb tini procps default-jre-headless \
       libpulse0 libgl1 libnss3 libx11-6 libxkbfile1 libxcomposite1 libxdamage1 libxrandr2 libxfixes3 libxcursor1 libxi6 libxtst6 libasound2 libdbus-1-3 bash \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 app \
    && mkdir -p /app/data && chown -R app:app /app
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    ANDROID_HOME=/app/data/android-sdk ANDROID_AVD_HOME=/app/data/android-avd EMU_GPU=swiftshader_indirect EMU_CORES=2 \
    MOBILE_AGENT_DOCKER=1 HOST=0.0.0.0 A2A_HOST=0.0.0.0 PORT=8000 PHONE_GATEWAY_PORT=8765 GAMES_PHONE=off
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app agent ./agent
COPY --chown=app:app web ./web
COPY --chown=app:app prompts ./prompts
COPY --chown=app:app scripts ./scripts
COPY --chown=app:app docker/ /opt/docker/
RUN chmod +x /opt/docker/entrypoint.sh
USER app
VOLUME /app/data
EXPOSE 8000 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/api/health', timeout=4)"]
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/docker/entrypoint.sh"]
CMD ["python", "-m", "agent.service"]

# Unit tests in the image (they need no phone or key; about 4 min):
#   docker build --target test -t mobile-agent:test . && docker run --rm --entrypoint python mobile-agent:test -m unittest discover -s tests
FROM runtime AS test
USER root
COPY --chown=app:app tests ./tests
COPY --chown=app:app docs ./docs
COPY --chown=app:app pyproject.toml ./
USER app
ENV HOST=127.0.0.1 A2A_HOST=127.0.0.1 PHONE_SLEEP=on GAMES_PHONE=on

# keep the plain `docker build .` result the runtime image
FROM runtime
