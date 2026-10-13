#!/bin/bash
# Builds dist/a2k-frontend-lambda.zip for deploy/lambda/frontend-lambda.yaml:
# the app (grc/) plus requirements-lambda.txt, installed as Linux x86_64
# wheels for Python 3.13 whatever machine builds it (pydantic-core is
# compiled, so a macOS or Windows install would not run on Lambda).
set -euo pipefail
cd "$(dirname "$0")/../.."  # frontend/
rm -rf build/lambda dist/a2k-frontend-lambda.zip
mkdir -p build/lambda dist
python3 -m pip install --quiet --target build/lambda --upgrade \
  --platform manylinux2014_x86_64 --implementation cp --python-version 3.13 --only-binary=:all: \
  -r requirements-lambda.txt
cp grc/*.py build/lambda/
cp -R grc/static grc/templates build/lambda/
find build/lambda -name __pycache__ -prune -exec rm -rf {} +
(cd build/lambda && zip -qr ../../dist/a2k-frontend-lambda.zip .)
echo "dist/a2k-frontend-lambda.zip ($(du -h dist/a2k-frontend-lambda.zip | cut -f1))"
