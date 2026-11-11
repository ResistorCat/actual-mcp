FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_DEV=1 \
    ACTUAL_DATA_DIR=/data \
    HOST=0.0.0.0 \
    PORT=8000

# Install dependencies using cached mount
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project

# Copy project source and install application
COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked

# Create persistent storage directory for Actual Budget SQLite cache
RUN mkdir -p /data

EXPOSE 8000

CMD ["uv", "run", "--no-dev", "uvicorn", "actual_mcp.server:app", "--host", "0.0.0.0", "--port", "8000"]
