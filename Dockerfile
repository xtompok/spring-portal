FROM python:3.12-slim

# postgresql-client provides pg_dump / pg_isready for the scheduler's backup job.
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# supercronic: cron for containers (no root cron daemon needed)
ARG SUPERCRONIC_VERSION=v0.2.33
ARG SUPERCRONIC_SHA1SUM=71b0d58cc53f6bd72cf2f293e09e294b79c666d8
RUN curl -fsSLo /usr/local/bin/supercronic \
        "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-amd64" \
    && echo "${SUPERCRONIC_SHA1SUM}  /usr/local/bin/supercronic" | sha1sum -c - \
    && chmod +x /usr/local/bin/supercronic

WORKDIR /app
COPY pyproject.toml ./
COPY portal ./portal
COPY db ./db
COPY scheduler ./scheduler
RUN pip install --no-cache-dir .

# The package installs into site-packages, so point the migration runner at the
# migrations copied into the image rather than a package-relative path.
ENV PORTAL_MIGRATIONS_DIR=/app/db/migrations

ENTRYPOINT ["portal"]
