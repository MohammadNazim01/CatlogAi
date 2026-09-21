#!/bin/sh
# One-shot: create the private bucket. Retries until MinIO accepts connections.
set -eu
i=0
until mc alias set local http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null 2>&1; do
  i=$((i+1)); [ "$i" -ge 30 ] && { echo "minio not reachable" >&2; exit 1; }
  sleep 1
done
mc mb --ignore-existing "local/$S3_BUCKET"
mc anonymous set none "local/$S3_BUCKET"
echo "bucket $S3_BUCKET ready (private)"
