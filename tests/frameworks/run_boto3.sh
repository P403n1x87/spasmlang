#!/usr/bin/env bash
# Run boto3's unit tests with spasm's bytecode round-trip import hook active.
# Creates a fresh venv, installs boto3 + pytest, then runs with
# PYTHONPATH pointing at our sitecustomize.py.
set -euo pipefail

PYTHON=${PYTHON:-$(command -v python3)}
VENV=/tmp/spasm-boto3-venv
PROJECT=$(cd "$(dirname "$0")/../.." && pwd)
HOOK_DIR="$PROJECT/tests/frameworks"

echo "=== Setting up venv at $VENV ==="
"$PYTHON" -m venv "$VENV" --clear

PY="$VENV/bin/python"
PIP="$PY -m pip"

$PIP install --quiet --upgrade pip
$PIP install --quiet boto3 pytest

# Installing spasmlang compiles spasm._core against the venv's interpreter,
# which is what the hook round-trips through.
echo "=== Building and installing spasmlang ==="
$PIP install --quiet "$PROJECT"

echo "=== Running boto3 tests with the round-trip hook ==="
BOTO3_DIR=$("$PY" -c "import boto3, os; print(os.path.dirname(boto3.__file__))")
BOTO3_TESTS="$BOTO3_DIR/../tests"

if [ ! -d "$BOTO3_TESTS" ]; then
    # boto3 from PyPI doesn't ship tests; clone just the tests
    echo "boto3 tests not found in site-packages; cloning from GitHub..."
    BOTO3_VERSION=$("$PY" -c "import boto3; print(boto3.__version__)")
    BOTO3_SRC=/tmp/boto3-src
    # Check for actual test files rather than just the directory: a partially
    # cleaned /tmp can leave the tree behind with everything in it gone, which
    # then silently collects zero tests.
    if [ -z "$(find "$BOTO3_SRC/tests/unit" -name 'test_*.py' -print -quit 2>/dev/null)" ]; then
        rm -rf "$BOTO3_SRC"
        git clone --quiet --depth 1 --branch "$BOTO3_VERSION" \
            https://github.com/boto/boto3.git "$BOTO3_SRC" 2>/dev/null || \
        git clone --quiet --depth 1 \
            https://github.com/boto/boto3.git "$BOTO3_SRC"
    fi
    BOTO3_TESTS="$BOTO3_SRC/tests/unit"
fi

echo "Tests dir: $BOTO3_TESTS"

# PYTHONPATH prepends the hook directory so sitecustomize.py is found
PYTHONPATH="$HOOK_DIR:${PYTHONPATH:-}" \
    "$PY" -m pytest "$BOTO3_TESTS" \
        -x -q --tb=short \
        2>&1
