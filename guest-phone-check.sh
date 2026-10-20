#!/data/data/com.termux/files/usr/bin/bash
# Read-only check of the paired virtual phone; no message sending or UI input.
set -eu
cd "$HOME/LittleWhale"
read -r runtime_path < .runtime-path
case "$runtime_path" in "$HOME"/.littlewhale-runtime-*) ;; *) exit 1 ;; esac
export PYTHONHOME="$runtime_path"
export LD_LIBRARY_PATH="$runtime_path/lib"
exec > /sdcard/LittleWhale-source/guest-phone-check.log 2>&1
mkdir -p "$HOME/.littlewhale"
cp /sdcard/LittleWhale-source/device.json "$HOME/.littlewhale/device.json"
"$runtime_path/bin/python3.14" phone.py --adb "$runtime_path/bin/adb" --adb-port 5039
