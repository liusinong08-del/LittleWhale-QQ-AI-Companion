#!/data/data/com.termux/files/usr/bin/bash
# Install into a NEW, versioned private directory; never replace Termux/system files.
set -euo pipefail
[ "$(getprop ro.build.version.sdk)" = 25 ] || exit 1
case "$HOME" in /data/data/com.termux/files/home) ;; *) exit 1 ;; esac
expected=${1:?Expected SHA256 is required}
[[ "$expected" =~ ^[0-9a-f]{64}$ ]] || exit 1
exec > /sdcard/LittleWhale-source/guest-private-setup.log 2>&1
actual=$(sha256sum /sdcard/LittleWhale-source/runtime.zip)
[ "${actual%% *}" = "$expected" ] || { printf 'Runtime checksum mismatch\n'; exit 1; }
runtime_path="$HOME/.littlewhale-runtime-${expected:0:12}"
[ ! -e "$runtime_path" ] || { printf 'Runtime directory already exists; preserve it and inspect before retrying.\n'; exit 1; }
mkdir "$runtime_path"
unzip -q /sdcard/LittleWhale-source/runtime.zip -d "$runtime_path"
chmod -R 700 "$runtime_path"
export PYTHONHOME="$runtime_path"
export LD_LIBRARY_PATH="$runtime_path/lib"
export SSL_CERT_FILE="$runtime_path/etc/tls/cert.pem"
mkdir -p "$HOME/LittleWhale"
mkdir -p "$HOME/LittleWhale/vendor/ai-companion"
for file in whale.py app.py social.py index.html check.py phone.py guest-start.sh guest-open.sh README.md vendor/ai-companion/humanize.js vendor/ai-companion/LICENSE vendor/ai-companion/UPSTREAM.json; do
    cp "/sdcard/LittleWhale-source/$file" "$HOME/LittleWhale/$file"
done
cd "$HOME/LittleWhale"
"$runtime_path/bin/python3.14" -c 'import sys,sqlite3,ssl; print(sys.version); print(sqlite3.sqlite_version); print(ssl.OPENSSL_VERSION)'
"$runtime_path/bin/python3.14" check.py
"$runtime_path/bin/python3.14" -c 'import urllib.request; response=urllib.request.urlopen("https://api-docs.deepseek.com/",timeout=20); print("PUBLIC_HTTPS_VERIFIED",response.status); response.close()'
printf '%s\n' "$runtime_path" > "$HOME/LittleWhale/.runtime-path"
printf 'GUEST_PRIVATE_RUNTIME_VERIFIED\n'
