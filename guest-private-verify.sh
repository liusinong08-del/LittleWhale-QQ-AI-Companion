#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail
expected=${1:?Runtime checksum required}
[[ "$expected" =~ ^[0-9a-f]{64}$ ]] || exit 1
runtime_path="$HOME/.littlewhale-runtime-${expected:0:12}"
[ -x "$runtime_path/bin/python3.14" ] || exit 1
exec > /sdcard/LittleWhale-source/guest-private-verification.log 2>&1
export PYTHONHOME="$runtime_path"
export LD_LIBRARY_PATH="$runtime_path/lib"
export SSL_CERT_FILE="$runtime_path/etc/tls/cert.pem"
cd "$HOME/LittleWhale"
cp /sdcard/LittleWhale-source/app.py app.py
mkdir -p vendor/ai-companion
for file in humanize.js LICENSE UPSTREAM.json; do
    cp "/sdcard/LittleWhale-source/vendor/ai-companion/$file" "vendor/ai-companion/$file"
done
"$runtime_path/bin/python3.14" -u check.py
printf '%s\n' "$runtime_path" > .runtime-path
"$runtime_path/bin/python3.14" -u -c 'import urllib.request; response=urllib.request.urlopen("https://api-docs.deepseek.com/",timeout=20); print("PUBLIC_HTTPS_VERIFIED",response.status); response.close()'
printf 'GUEST_PRIVATE_RUNTIME_VERIFIED\n'
