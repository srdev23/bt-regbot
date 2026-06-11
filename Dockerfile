FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY regbot/ regbot/
COPY main.py .

RUN useradd --create-home --uid 1000 botuser \
    && mkdir -p /app/data/orders \
    && chown -R botuser:botuser /app

USER botuser

ENV PYTHONUNBUFFERED=1 \
    ORDERS_DIR=/app/data/orders

CMD ["python", "main.py"]
