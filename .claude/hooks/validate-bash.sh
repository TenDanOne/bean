#!/usr/bin/env bash
# Thin wrapper so settings.json can point at a stable .sh entrypoint; the
# actual logic is in validate-bash.mjs (Node, sin dependencias).
exec node "$(dirname "$0")/validate-bash.mjs"
