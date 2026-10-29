FROM python:3.13-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends openssl \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

RUN useradd --system --no-create-home unwrapper
USER unwrapper
ENV PATH="/app/.venv/bin:$PATH" PUSH_HOST=0.0.0.0
EXPOSE 8090
CMD ["python", "-m", "cf_email_unwrapper"]
