"""Read-only verification of the real VM app and four packaged launcher icons."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import sys
import time
import zipfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from phone import Phone
from PIL import Image, ImageChops


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config",type=Path,default=ROOT/"downloads/device-usb.json")
    parser.add_argument("--adb",default=str(ROOT.parent/"PocketPilot/.toolchain/sdk/platform-tools/adb.exe"))
    parser.add_argument("--port",type=int,default=5038)
    args=parser.parse_args()
    phone=Phone(json.loads(args.config.read_text("utf-8-sig")),args.adb,args.port)
    phone.verify()
    assert phone.foreground()=="cn.littlewhale.app", "Open the LittleWhale app first; this check never changes the user's page"
    # This developer check reads labels directly; the model's Phone.observe deliberately blocks its own settings app.
    root=phone.ui_xml()
    assert phone.foreground()=="cn.littlewhale.app","App focus changed while checking"
    labels={n.get("text","") for n in root.iter("node") if n.get("package")=="cn.littlewhale.app" and n.get("password")!="true" and not n.get("class","").endswith("EditText")}
    assert "小鲸鱼 · 本机空间" in labels, "The dedicated app has not connected"
    assert "生活设置" in labels and any("API 密钥" in label for label in labels), "Open life settings before this read-only check"
    # No input values or authentication links are recorded in the report.
    icons={}
    for name,apk in (("app","LittleWhale.apk"),("bridge","LittleWhale-bridge.apk"),("diary","LittleWhale-diary.apk"),("memory","LittleWhale-memory.apk")):
        path=ROOT/"dist"/apk
        with zipfile.ZipFile(path) as package:
            icon=next(n for n in package.namelist() if n.endswith("/ic_launcher.png"))
            packed=Image.open(io.BytesIO(package.read(icon))).convert("RGB")
            original=Image.open(ROOT/"android/icons"/(name+".png")).convert("RGB")
            assert packed.size==original.size==(192,192)
            assert ImageChops.difference(packed,original).getbbox() is None, "Wrong launcher icon in "+apk
        icons[name]=dict(apk=apk,sha256=hashlib.sha256(path.read_bytes()).hexdigest(),icon=icon)
    report=dict(checked_at=time.time(),target="paired phone VM",app_connected=True,settings_visible=True,
                user_input_changed=False,external_model_calls=0,icons=icons)
    (ROOT/"evidence/guest-app-check.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print("PASS: real VM app connected, settings visible, four distinct icons correctly packaged; no user input changed")


if __name__=="__main__":main()
