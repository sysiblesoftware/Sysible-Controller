#!/bin/bash
# Replaced by the `sysible_controller` CLI (installed to /usr/local/bin
# by install_sysible.sh) - the backend now runs as a real systemd
# service (sysible-backend) instead of a foreground process tied to
# this script's lifetime. Kept as a thin redirect so old habits/
# automation pointed at this path still work.
echo "start_sysible.sh has been replaced by the 'sysible_controller' command."
echo "Running: sudo sysible_controller start"
exec sysible_controller start "$@"
