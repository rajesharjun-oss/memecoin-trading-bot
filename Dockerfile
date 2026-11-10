FROM python:3.13-slim AS builder

WORKDIR /build
COPY pyproject.toml README.md requirements.lock ./
COPY src ./src
RUN python -m venv /venv \
    && /venv/bin/pip install --no-cache-dir --upgrade pip==26.2.1 setuptools==84.2.0 \
    && /venv/bin/pip install --no-cache-dir -c requirements.lock .

FROM python:3.13-slim
ENV PATH="/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TRADE_STATE_PATH=/app/state/trade-state.sqlite3
RUN groupadd --system bot && useradd --system --gid bot --home /app bot \
    && mkdir -p /app/state && chown -R bot:bot /app
COPY --from=builder /venv /venv
WORKDIR /app
USER bot
ENTRYPOINT ["memecoin-bot"]
CMD ["--help"]
