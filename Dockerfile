FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN addgroup --system --gid 10001 app && \
    adduser --system --uid 10001 --ingroup app app

COPY pyproject.toml README.md ./
COPY src ./src
COPY configs ./configs
COPY scripts ./scripts

RUN python -m pip install --no-cache-dir --upgrade pip && \
    python -m pip install --no-cache-dir -e .

RUN mkdir -p /app/reports/tmp && chown -R 10001:10001 /app/reports

USER 10001

HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD python -c "import trading_ai; print('ok')"

CMD ["python", "-m", "trading_ai.cli", "--help"]
