"""Pair the two installed VM apps through their actual UI; no contact messages or model calls."""
import json
from pathlib import Path
import re
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from phone import Phone


def main():
    phone=Phone(json.loads((ROOT/"downloads/device-usb.json").read_text("utf-8-sig")),ROOT.parent/"PocketPilot/.toolchain/sdk/platform-tools/adb.exe",5038)
    phone.verify()
    def read(package):
        try:
            if phone.foreground()!=package:return None
        except ValueError:return None  # A window transition may temporarily have no focus; only retry the read.
        root=phone.ui_xml()
        if phone.foreground()!=package:return None
        return root
    def click(package,label):
        until=time.monotonic()+15
        while True:
            root=read(package)
            if root is not None:
                node=next((n for n in root.iter("node") if n.get("package")==package and n.get("text")==label and n.get("clickable")=="true"),None)
                if node is not None:
                    bounds=re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]",node.get("bounds",""))
                    if bounds:
                        x1,y1,x2,y2=map(int,bounds.groups())
                        if x2>x1 and y2>y1:
                            phone.tap((x1+x2)//2,(y1+y2)//2,expected=package)
                            return
            if time.monotonic()>until:raise TimeoutError("Expected pairing control not visible; no guessed click")
            time.sleep(.5)
    phone.shell("am","start","-n","cn.littlewhale.app/.WebApp")
    click("cn.littlewhale.app","配对虚拟机消息助手")
    click("cn.littlewhale.bridge","保存配对")
    phone.shell("am","start","-n","cn.littlewhale.app/.WebApp")
    until=time.monotonic()+45
    while True:
        root=read("cn.littlewhale.app")
        connected=root is not None and any(n.get("text")=="消息助手已连接。" for n in root.iter("node"))
        if connected:break
        if time.monotonic()>until:raise TimeoutError("Pairing submitted; helper heartbeat not confirmed. Do not repeat blindly")
        time.sleep(1)
    listeners=phone.shell("settings","get","secure","enabled_notification_listeners").split(":")
    assert "cn.littlewhale.bridge/cn.littlewhale.bridge.Main$Listener" in listeners
    report=dict(checked_at=time.time(),target="paired phone VM",native_intent_handoff=True,helper_paired=True,
                notification_access=True,helper_heartbeat_connected=True,model_calls_requested=0,contact_messages_requested=0)
    (ROOT/"evidence/guest-pair-check.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print("PASS: VM app -> explicit helper pairing -> saved token -> connected notification heartbeat; no secrets displayed")


if __name__=="__main__":main()
