#!/bin/sh
# HELP: Transfer files to and from your game console from a phone or PC browser
# ICON: wifitransfer
# GRID: Wi-Fi Transfer
# © 2026 PocketKode · SPDX-License-Identifier: GPL-3.0-only

. /opt/muos/script/var/func.sh

APP_BIN="python3"
SETUP_APP "$APP_BIN" ""

SETUP_STAGE_OVERLAY

# -----------------------------------------------------------------------------

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR" || exit 1

mkdir -p "$APP_DIR/logs"

export PYSDL2_DLL_PATH="/usr/lib"
export SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS=1
export PYTHONDONTWRITEBYTECODE=1

# Log to memory while running: nothing may hold a file open on the SD card during a full backup
# Exit code 42: the app asks to be started again (to finish an update)
while :; do
	python3 -u main.py >/tmp/wifitransfer.log 2>&1
	[ $? -eq 42 ] || break
done
cp /tmp/wifitransfer.log "$APP_DIR/logs/app.log" 2>/dev/null

# If the app crashed during a transfer, undo its keep-awake setting
if [ -f /tmp/wifitransfer-caffeine ]; then
	rm -f /run/muos/caffeine /tmp/wifitransfer-caffeine
fi

# New files arrived; make sure they're written to the SD card before muOS rescans
sync
