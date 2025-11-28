#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

export PATH="${HOME}/.local/bin:${PATH}"

if [[ ! -d ".venv" ]]; then
  echo "Missing .venv virtualenv. Run python scripts/setup.py first." >&2
  exit 1
fi

source .venv/bin/activate

exec python scripts/bluesnap_service.py --config config/bluesnap.yaml

