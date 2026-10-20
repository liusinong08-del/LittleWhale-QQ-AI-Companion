#!/data/data/com.termux/files/usr/bin/bash
set -eu
cd "$HOME/LittleWhale"
read -r runtime_path < .runtime-path
case "$runtime_path" in "$HOME"/.littlewhale-runtime-*) ;; *) exit 1 ;; esac
export PYTHONHOME="$runtime_path"
export LD_LIBRARY_PATH="$runtime_path/lib"
export SSL_CERT_FILE="$runtime_path/etc/tls/cert.pem"
export PATH="$runtime_path/bin:$PATH"
umask 077
mkdir -p "$HOME/.littlewhale"
# ponytail: VMOS can leave service requests waiting forever; start the core after 8s and report an unconfirmed wake lock.
/system/bin/timeout -k 2 8 termux-wake-lock || printf 'Wake lock not confirmed; background lifetime needs verification.\n' >&2
trap '/system/bin/timeout -k 2 8 termux-wake-unlock || true' EXIT
trap 'exit 0' INT TERM
while true; do
    # Keep startup diagnostics bounded; stdout contains the private panel link and is not logged.
    "$runtime_path/bin/python3.14" -c 'from pathlib import Path; p=Path.home()/".littlewhale/runner-errors.log"; p.exists() and p.stat().st_size>1000000 and p.replace(p.with_suffix(".previous.log"))'
    set +e
    BROWSER=termux-open-url "$runtime_path/bin/python3.14" app.py --port 8767 "$@" 2>> "$HOME/.littlewhale/runner-errors.log"
    result=$?
    set -e
    case "$result" in 0|2) break ;; esac
    printf 'Unexpected core exit (%s); retrying in 10 seconds.\n' "$result"
    sleep 10
done
