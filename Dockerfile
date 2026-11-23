FROM python:3.11.15-slim-bookworm@sha256:d29f48a31a8b408ed19272ca1e7b10ebae13b240a27e862d3d4217c528e2e0c3

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    INVOICEMATCH_DB_PATH=/data/reviews.sqlite3

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src/ src/
RUN python -m pip install --no-cache-dir --disable-pip-version-check . \
    && install -d -o 10001 -g 10001 /data

USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "invoicematch.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
