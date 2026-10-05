#!/usr/bin/env bash
# Start one gateway instance. Configuration comes from the GATEWAY_* environment
# variables documented in README.md.
set -euo pipefail
cd "$(dirname "$0")"
exec python3 -m gateway.app
