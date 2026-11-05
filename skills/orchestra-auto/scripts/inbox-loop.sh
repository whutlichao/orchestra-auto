#!/bin/bash
# Retired entry point: the Python stub exits before touching Orca or state.
set -euo pipefail
exec python3 "$(dirname "$0")/inbox-loop.py" "$@"
