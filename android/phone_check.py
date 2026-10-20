"""One real-Android model/action/UI check on the dedicated emulator; no external model calls."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from android.check import adb, verify_emulator
from phone import Phone
from whale import Whale


def main():
    measured=verify_emulator()
    target=ROOT/"downloads/emulator-notebook-device.json"
    subprocess.run([sys.executable,"android/build.py","--fixture","--device",str(target)],cwd=ROOT,check=True)
    adb("install","-r",str(ROOT/"dist/LittleWhale-bridge-fixture.apk"))
    config=dict(serial="127.0.0.1:5555",sdk=measured["sdk"],android_id=measured["android_id"],
                fingerprint=adb("shell","getprop","ro.build.fingerprint"),app_scope="whole_vm")
    phone=Phone(config,ROOT.parent/"PocketPilot/.toolchain/sdk/platform-tools/adb.exe",5038)
    # Test transport only: production Phone refuses USB serials. Identity is still verified on every action.
    phone.serial="emulator-5554"
    phone.connect=phone.status
    calls=[]
    def model(cfg,messages):
        step=len(calls);calls.append(step)
        previous=json.loads(messages[-1]["content"].split("] ",1)[1]) if step else None
        if step==0:request=dict(name="open",package="cn.littlewhale.memory")
        elif step in (1,3,5):request=dict(name="observe")
        elif step==2:
            field=next(n for n in previous["nodes"] if n["editable"] and n["description"]=="搜索记录")
            request=dict(name="tap",screen=previous["screen"],node=field["node"])
        elif step==4:
            field=next(n for n in previous["nodes"] if n["editable"] and n["focused"])
            request=dict(name="type",screen=previous["screen"],node=field["node"],text="海")
        else:
            assert previous["package"]=="cn.littlewhale.memory"
            return dict(lines=["已在记忆应用搜索‘海’。"]),{"total_tokens":1}
        return dict(lines=[],action=request),{"total_tokens":1}
    with tempfile.TemporaryDirectory() as folder:
        whale=Whale(Path(folder)/"whale",model)
        phone.permitted=lambda:not whale.setting("paused",False)
        whale.paired_phone=lambda **kwargs:phone
        try:
            entry=dict(id="phone-check-"+str(time.time_ns()),title="中文输入测试",body="想找一片海。")
            phone.notebook("memory",entry)
            reply=whale.chat("在记忆应用里搜索海","real-ui-check")
            view=phone.observe(private=True)
            field=next(n for n in view["nodes"] if n["editable"] and n["description"]=="搜索记录")
            assert field["value"]=="海"
            assert reply["lines"]==["已在记忆应用搜索‘海’。"] and len(calls)==7
            rows=[dict(r) for r in whale.db.execute("SELECT step,name,status,result FROM phone_actions ORDER BY step")]
            assert len(rows)==6 and all(r["status"]=="done" for r in rows)
            assert json.loads(rows[4]["result"])["typed_verified"] is True
            assert "想找一片海" in json.dumps(json.loads(rows[5]["result"]),ensure_ascii=False)
            before=len(calls)
            assert whale.chat("在记忆应用里搜索海","real-ui-check")==reply and len(calls)==before
            denied=adb("shell","run-as","com.termux","/system/bin/content","call","--uri","content://cn.littlewhale.bridge.control","--method","clipboard","--arg","5rW3",check=False)
            assert "clipboard_set=true" not in denied
            report=dict(target="emulator-5554",sdk=measured["sdk"],loop="model stub -> real Phone -> Android -> verified Chinese text -> model result",model_calls=len(calls),
                        external_model_calls=0,action_count=len(rows),real_open=True,real_observation=True,real_click=True,chinese_input_verified=True,
                        app_display_routed=phone.focus()[1],duplicate_did_not_replay=True,unrelated_app_clipboard_rejected=True,
                        real_phone_vm_verified=False,real_deepseek_verified=False)
            (ROOT/"evidence/android-phone-check.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
            print("PASS: model/action/result loop on real Android, app launch, display-aware click, verified Chinese input and no duplicate replay")
        finally:
            whale.close()


if __name__=="__main__":main()
