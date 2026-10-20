"""Read the paired VM's update report and match it to the current pinned source."""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from phone import Phone


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config",type=Path,default=ROOT/"downloads/device-usb.json")
    args=parser.parse_args()
    phone=Phone(json.loads(args.config.read_text("utf-8-sig")),ROOT.parent/"PocketPilot/.toolchain/sdk/platform-tools/adb.exe",5038)
    phone.verify()
    report=json.loads(phone.shell("cat","/sdcard/LittleWhale-source/guest-maintenance.json"))
    assert report["ok"] and not report["running"] and report["phase"]=="done",report
    assert report["configuration_preserved"] and report["personal_data_preserved"]
    module=ast.parse((ROOT/"android/build.py").read_text("utf-8"))
    files=next(ast.literal_eval(node.value) for node in module.body if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=="CORE_FILES" for t in node.targets))
    hashes={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in files}
    assert report["source_digest"]==hashlib.sha256(json.dumps(hashes,sort_keys=True).encode()).hexdigest(),"VM report is for another source version"
    (ROOT/"evidence/guest-core-update.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print("PASS: paired VM updated to current source, preserved configuration and personal data; no secrets read")


if __name__=="__main__":main()
