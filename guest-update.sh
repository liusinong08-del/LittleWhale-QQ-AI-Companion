#!/data/data/com.termux/files/usr/bin/bash
# Run only in the paired VM's Termux, with the core stopped. User data stays in ~/.littlewhale.
set -euo pipefail
[ "$(getprop ro.build.version.sdk)" = 25 ] || exit 1
case "$HOME" in /data/data/com.termux/files/home) ;; *) exit 1 ;; esac
cd "$HOME/LittleWhale"
read -r runtime_path < .runtime-path
case "$runtime_path" in "$HOME"/.littlewhale-runtime-*) ;; *) exit 1 ;; esac
export PYTHONHOME="$runtime_path"
export LD_LIBRARY_PATH="$runtime_path/lib"
export SSL_CERT_FILE="$runtime_path/etc/tls/cert.pem"
"$runtime_path/bin/python3.14" -c 'from pathlib import Path; from app import acquire; lock=acquire(Path.home()/".littlewhale"); lock.close()'
mkdir -p vendor/ai-companion
for file in whale.py app.py social.py speech.py index.html check.py phone.py guest-start.sh guest-open.sh README.md vendor/ai-companion/humanize.js vendor/ai-companion/LICENSE vendor/ai-companion/UPSTREAM.json; do
    cp "/sdcard/LittleWhale-source/$file" "$file"
done
"$runtime_path/bin/python3.14" check.py
"$runtime_path/bin/python3.14" - <<'PY'
from pathlib import Path
import re
from whale import atomic

folder = Path.home() / ".termux"
folder.mkdir(exist_ok=True)
path = folder / "termux.properties"
old = path.read_bytes() if path.exists() else b""
backup = folder / "termux.properties.before-littlewhale"
if not backup.exists():
    atomic(backup, old)
value = re.sub(r"(?m)^\s*allow-external-apps\s*=.*$", "allow-external-apps=true", old.decode("utf-8"))
if not re.search(r"(?m)^allow-external-apps=true$", value):
    value += "\nallow-external-apps=true\n"
atomic(path, value.encode())
print("Helper recovery setting prepared; grant RUN_COMMAND only to the paired LittleWhale helper.")
PY
# ponytail: VMOS may never complete an Android broadcast; bound the wait and keep the saved configuration.
/system/bin/timeout -k 2 8 termux-reload-settings || printf 'Settings saved; Android did not confirm reloading.\n'
printf 'LittleWhale code updated; memory, diary, role and model settings preserved.\n'
