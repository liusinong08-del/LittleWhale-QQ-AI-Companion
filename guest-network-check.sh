#!/data/data/com.termux/files/usr/bin/bash
# Public endpoints only: no account, cookie, model key or paid model request.
set -eu
cd "$HOME/LittleWhale"
read -r runtime_path < .runtime-path
case "$runtime_path" in "$HOME"/.littlewhale-runtime-*) ;; *) exit 1 ;; esac
export PYTHONHOME="$runtime_path"
export LD_LIBRARY_PATH="$runtime_path/lib"
export SSL_CERT_FILE="$runtime_path/etc/tls/cert.pem"
"$runtime_path/bin/python3.14" -u - <<'PY'
import datetime
import json
from pathlib import Path
import socket
import time
import urllib.error
import urllib.request

results=[]
for domain, path in (("api-docs.deepseek.com", "/"), ("im.qq.com", "/"), ("weixin.qq.com", "/"),
                     ("turing.captcha.qcloud.com", "/TCaptcha.js"),
                     ("turing.captcha.qcloud.com", "/TJCaptcha.js")):
    item={"domain":domain,"path":path,"https_reachable":False}
    start=time.monotonic()
    try:
        item["dns_answers"]=len(socket.getaddrinfo(domain,443,type=socket.SOCK_STREAM))
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open("https://"+domain+path,timeout=8) as response:
            item.update(https_reachable=True,status=response.status)
            response.read(1)
    except urllib.error.HTTPError as error:
        # A verified HTTPS response with HTTP 4xx still proves connectivity, not account validity.
        item.update(https_reachable=True,status=error.code)
    except Exception as error:
        cause=error.reason if isinstance(error,urllib.error.URLError) else error
        item["error"]=type(cause).__name__
    item["seconds"]=round(time.monotonic()-start,2)
    results.append(item)
    print(json.dumps(item,ensure_ascii=False),flush=True)
report={"checked_at":datetime.datetime.now(datetime.timezone.utc).isoformat(),"results":results}
Path("/sdcard/LittleWhale-source/guest-network-check.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
print("NETWORK_CHECK_FINISHED",flush=True)
PY
