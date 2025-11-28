#!/usr/bin/env bash
set -euo pipefail

# Systemd services do not always populate HOME; derive it from passwd entry when unset.
if [[ -z "${HOME:-}" ]]; then
  HOME="$(getent passwd "$(id -un)" | cut -d: -f6)"
fi
export HOME

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

export PATH="${HOME}/.local/bin:${PATH}"

if [[ ! -d ".venv" ]]; then
  echo "Missing .venv virtualenv. Run python scripts/setup.py first." >&2
  exit 1
fi

source .venv/bin/activate

exec python scripts/bluesnap_service.py --config config/bluesnap.yaml

