#!/bin/sh
# Runs every test suite: backend (Python), frontend logic (Node), browser smoke test (Chrome).
# Usage: tests/run.sh        (from anywhere; exits non-zero if any suite fails)
set -e
cd "$(dirname "$0")/.."

echo "== Backend + browser smoke tests (python3 -m unittest) =="
python3 -m unittest discover -s tests -v

echo
if command -v node >/dev/null 2>&1; then
  echo "== Frontend logic tests (node --test) =="
  node --test tests/frontend/
else
  echo "== Frontend logic tests skipped: Node.js 18+ not installed =="
fi
