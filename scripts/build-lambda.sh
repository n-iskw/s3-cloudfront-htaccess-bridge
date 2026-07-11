#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="${ROOT_DIR}/package"
OUTPUT_FILE="${ROOT_DIR}/htaccess_bridge.zip"

rm -rf "${BUILD_DIR}"
rm -f "${OUTPUT_FILE}"
mkdir -p "${BUILD_DIR}"

python3 -m pip install \
  --platform manylinux2014_x86_64 \
  --implementation cp \
  --python-version 3.13 \
  --only-binary=:all: \
  --target "${BUILD_DIR}" \
  boto3 'botocore[crt]'

cp "${ROOT_DIR}/lambda/htaccess_bridge.py" "${BUILD_DIR}/"

(
  cd "${BUILD_DIR}"
  find . -type d -name __pycache__ -prune -exec rm -rf {} +
  zip -qr "${OUTPUT_FILE}" .
)

echo "Created ${OUTPUT_FILE}"
