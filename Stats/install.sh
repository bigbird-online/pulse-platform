#!/bin/bash
# One installation entry: tested main App + actual publisher paths + live readback.
# No unverified DerivedData, broad pkill, or direct replacement of production.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
REVISION="$(git -C "$REPO" rev-parse HEAD)"
MAIN="$(gh api repos/bigbird-online/PULSE/git/ref/heads/main --jq '.object.sha')"
if [[ "$REVISION" != "$MAIN" ]]; then
  echo 'Installation requires the approved current main revision.' >&2
  exit 2
fi
PY="$HOME/.local/share/coevolution/python-env/bin/python"
"$PY" -I -c 'import pathlib,sys,yaml; root=pathlib.Path.home()/".local/share/coevolution/python-env"; assert sys.prefix == str(root) and sys.executable == str(root/"bin/python")'
EXPECTED="$($PY -c 'import json,pathlib; print(json.loads((pathlib.Path.home()/".local/state/pulse/stats/refresh.json").read_text())["source_revision"])')"
gh workflow run stats-macos-ci.yml --repo bigbird-online/PULSE --ref main \
  -f deploy=true -f approved_revision="$REVISION" -f expected_active_revision="$EXPECTED"
echo 'Installation submitted to GitHub Actions. Deployment and live readback are recorded there.'
