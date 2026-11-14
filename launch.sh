#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
    printf 'Virtual environment not found. Run "%s/setup-env.sh" first.\n' "$ROOT" >&2
    exit 1
fi
cd "$ROOT"
exec "$ROOT/.venv/bin/python" -m database_navigator.ui "$@"
