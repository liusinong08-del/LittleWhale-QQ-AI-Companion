"""Inspect the real VM's model check; --request explicitly makes ONE configured API call."""
import argparse
import json
from pathlib import Path
import re
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from phone import Phone


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request",action="store_true")
    args=parser.parse_args()
    phone=Phone(json.loads((ROOT/"downloads/device-usb.json").read_text("utf-8-sig")),ROOT.parent/"PocketPilot/.toolchain/sdk/platform-tools/adb.exe",5038)
    phone.verify()
    def read():
        assert phone.foreground()=="cn.littlewhale.app","App focus changed; no further UI input"
        root=phone.ui_xml()
        assert phone.foreground()=="cn.littlewhale.app","App focus changed while reading"
        # Only non-secret status/model labels are selected. No input values, role cards or URLs leave this function.
        selected={n.get("resource-id"):n.attrib for n in root.iter("node") if n.get("package")=="cn.littlewhale.app" and n.get("resource-id") in ("test-model","model-check","key-state","model")}
        selected["calls"]=next((int(m[1]) for n in root.iter("node") if (m:=re.search(r"在这儿 · 今日 (\d+) 次",n.get("text","")))),None)
        return selected
    state=read()
    before=state["calls"]
    if args.request:
        for attempt in range(8):
            node=state.get("test-model",{})
            bounds=re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]",node.get("bounds",""))
            if bounds:
                x1,y1,x2,y2=map(int,bounds.groups())
                if x2>x1 and y2>y1 and node.get("enabled")=="true":
                    phone.tap((x1+x2)//2,(y1+y2)//2,expected="cn.littlewhale.app")
                    break
            width,height=phone.size()
            phone.swipe(int(width*.8),int(height*.85),int(width*.8),int(height*.3),500,expected="cn.littlewhale.app")
            state=read()
        else:raise AssertionError("Model-check button not visible; no API request submitted")
        deadline=time.monotonic()+85
        while True:
            state=read()
            result=state.get("model-check",{}).get("text","")
            if state.get("test-model",{}).get("enabled")=="true" and "正在检查" not in result:break
            if time.monotonic()>deadline:raise TimeoutError("Outcome not confirmed; do not automatically click again")
            time.sleep(1)
    result=state.get("model-check",{}).get("text","")
    report=dict(checked_at=time.time(),target="paired phone VM",requested_once=args.request,
                success=result.startswith("模型已连通："),result=result,model=state.get("model",{}).get("text"),
                calls_before=before,calls_after=state["calls"],personal_context_sent=False)
    (ROOT/"evidence/guest-model-check.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(report,ensure_ascii=False))
    assert report["success"],"Real model connection not verified; see the recorded sanitized result"


if __name__=="__main__":main()
