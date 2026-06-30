#!/bin/bash
# Replaced by the `sysible_controller` CLI (installed to /usr/local/bin
# by install_sysible.sh) - see start_sysible.sh for why. Kept as a thin
# redirect so old habits/automation pointed at this path still work.
echo "stop_sysible.sh has been replaced by the 'sysible_controller' command."
echo "Running: sudo sysible_controller stop"
exec sysible_controller stop "$@"
