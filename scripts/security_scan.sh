#!/usr/bin/env bash
# Local supply-chain scan: audit pinned deps for known CVEs and emit a CycloneDX SBOM.
# Mirrors the security-scan CI job so a developer can run the exact gate before pushing.
#   ./scripts/security_scan.sh
set -euo pipefail
cd "$(dirname "$0")/.."

python3 -m pip install --quiet --upgrade pip pip-audit cyclonedx-bom

echo "== pip-audit (known CVEs in requirements.txt) =="
pip-audit -r requirements.txt --strict --desc

echo
echo "== CycloneDX SBOM -> sbom.cdx.json =="
cyclonedx-py requirements requirements.txt -o sbom.cdx.json 2>/dev/null \
  || cyclonedx-py -r -i requirements.txt -o sbom.cdx.json
echo "Wrote sbom.cdx.json"

# NOTE: a fully hash-locked manifest is a release-time artifact (needs network to
# resolve artifact hashes): regenerate with
#   pip-compile --generate-hashes --output-file=requirements.lock.txt requirements.txt
# and deploy with `pip install --require-hashes -r requirements.lock.txt`.
