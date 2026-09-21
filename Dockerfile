# Both stages share this base: the venv is copied between them and its
# interpreter path must resolve identically in each.
FROM python:3.13-slim-trixie@sha256:8d9d0b8bcf6506481eae4907c18f5e3e7902e629f5f6d684f9e7c32e85e3ddf0 AS base

FROM base AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.17@sha256:10787c682e4184e4f290de1171fd4703dc63de99221f10fe1c99002ce7fa9acc /uv /bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=0 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1
WORKDIR /build
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev

FROM base
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends git tini \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --create-home fava \
    && install -d -o fava -g fava /ledger
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    FAVA_HOST=0.0.0.0
USER fava
WORKDIR /ledger
EXPOSE 5000
ENTRYPOINT ["tini", "--"]
CMD ["fava"]
