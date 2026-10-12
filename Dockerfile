# PeakWhale Berth: web page, HTTP API and command line in one image.
#   docker build -t berth .
#   docker run --rm -p 8000:8000 berth            # then open http://localhost:8000
#   docker run --rm berth berth plan --example delivery-vans --no-csv
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY examples ./examples
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"
RUN useradd --create-home berth
USER berth

EXPOSE 8000
CMD ["berth", "serve", "--host", "0.0.0.0", "--port", "8000"]
