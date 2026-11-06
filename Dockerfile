# Both stages share this base: the venv is copied between them and its
# interpreter path must resolve identically in each.
FROM python:3.13-slim-trixie@sha256:09a17cad02668ae61ece136b4fd36ac3849e14f670ebedb637ea3e15580525bc AS base

FROM base AS builder
COPY --from=ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 /uv /bin/uv
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
