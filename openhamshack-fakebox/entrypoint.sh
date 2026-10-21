#!/bin/bash
# Start the fakebox. exec, so SIGTERM from Dock360 reaches Python directly.
set -euo pipefail
cd /opt/fakebox
exec python3 -m app.main
