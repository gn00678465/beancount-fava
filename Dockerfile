# Both stages share this base: the venv is copied between them and its
# interpreter path must resolve identically in each.
FROM python:3.13-slim-trixie@sha256:3dd7cc108ec1493442514f5c2a871af6af0ec31d768ff6e378a93340c3b3db5f AS base

FROM base AS builder
COPY --from=ghcr.io/astral-sh/uv:0.13.0@sha256:cdc6093146eb3ff6a40107b38f008b789e050e77ad87865e381d9917da55a168 /uv /bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=0 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1
WORKDIR /build
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM base
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends git tini \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --create-home fava \
    && install -d -o fava -g fava /ledger
# Everything Python in this image comes from uv.lock; the base image's pip
# would let the runtime user install more.
RUN python -m pip uninstall --yes --root-user-action ignore pip
COPY --from=builder /opt/venv /opt/venv
COPY --chmod=755 docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
ENV PATH="/opt/venv/bin:$PATH"
ENV FAVA_HOST=0.0.0.0
USER 1000:1000
WORKDIR /ledger
EXPOSE 5000
ENTRYPOINT ["tini", "--", "docker-entrypoint.sh"]
CMD ["beancount-fava-serve"]
