#!/bin/sh
set -e

READY_FLAG_PATH="${YOLO_READY_FLAG_PATH:-/tmp/worker_ready}"

touch "$READY_FLAG_PATH"

exec "$@"
