FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml requirements.txt ./
COPY sn_reg_bot/ sn_reg_bot/
COPY main.py ./

RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir -e .

RUN useradd --create-home --uid 1000 botuser \
    && mkdir -p /app/data \
    && chown -R botuser:botuser /app

USER botuser

ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/app/data

CMD ["python", "main.py"]
