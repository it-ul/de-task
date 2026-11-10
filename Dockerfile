FROM python:3.12-slim

WORKDIR /app

# psql нужен pipeline.sh для миграций после ETL
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["bash", "pipeline.sh"]
