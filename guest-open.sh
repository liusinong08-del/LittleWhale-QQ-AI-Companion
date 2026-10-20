#!/data/data/com.termux/files/usr/bin/bash
set -eu
export PATH="/system/bin:/system/xbin:$PATH"
url=
IFS= read -r url < "$HOME/.littlewhale/panel-url" || [ -n "$url" ]
case "$url" in http://127.0.0.1:8767/\#*) ;; *) exit 1 ;; esac
# Use Android directly: Termux's opener broadcast can hang inside VMOS.
LD_LIBRARY_PATH= /system/bin/timeout -k 2 15 /system/bin/am start --user 0 -a android.intent.action.VIEW -d "$url" >/dev/null 2>&1
