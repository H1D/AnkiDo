# Multi-arch (amd64/arm64). The `anki` wheels are manylinux_2_35, so Debian 12 (glibc 2.36) is fine.

FROM python:3.12-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.12-slim-bookworm
LABEL org.opencontainers.image.title="Ankido" \
      org.opencontainers.image.description="Headless, multi-account HTTP API for AnkiWeb collections" \
      org.opencontainers.image.source="https://github.com/H1D/AnkiDo" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later"
RUN groupadd -g 1000 ankido && useradd -u 1000 -g ankido -m -s /usr/sbin/nologin ankido \
    && mkdir -p /data /config && chown ankido:ankido /data /config
COPY --from=build --chown=ankido:ankido /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    ANKIDO_CONFIG=/config/ankido.yaml
USER ankido
WORKDIR /data
VOLUME ["/data", "/config"]
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=3).status == 200 else 1)"
ENTRYPOINT ["ankido"]
CMD ["serve", "--bind", "0.0.0.0"]
