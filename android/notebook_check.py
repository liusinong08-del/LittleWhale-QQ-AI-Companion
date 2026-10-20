"""Runnable real-Android check; dedicated emulator only, no model or account messages."""
import base64
import json
from pathlib import Path
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from android.check import adb, verify_emulator

def result(output):
    found=re.search(r"data64=([A-Za-z0-9+/]+={0,2})",output)
    assert found,output[:200]
    return json.loads(base64.b64decode(found[1]))

def call(kind,method,arg=""):
    return result(adb("shell","content","call","--uri","content://cn.littlewhale."+kind+".entries","--method",method,*(["--arg",arg] if arg else [])))

def click(package,label):
    adb("shell","uiautomator","dump","/sdcard/littlewhale-notebook-check.xml")
    tree=ET.fromstring(adb("shell","cat","/sdcard/littlewhale-notebook-check.xml"))
    node=next(n for n in tree.iter("node") if n.get("text")==label and n.get("package")==package)
    x1,y1,x2,y2=map(int,re.findall(r"\d+",node.get("bounds")))
    display=None;matched=[]
    for line in adb("shell","dumpsys","activity","activities").splitlines():
        m=re.match(r"Display #(\d+)",line)
        if m:display=int(m[1])
        if "topResumedActivity=" in line and package+"/cn.littlewhale.notes.Notebook" in line:matched.append(display)
    assert len(matched)==1 and matched[0] is not None
    adb("shell","input","-d",matched[0],"tap",(x1+x2)//2,(y1+y2)//2)

def main():
    verify_emulator()
    target=ROOT/"downloads/emulator-notebook-device.json"
    target.write_text(json.dumps(dict(sdk=32,android_id=(ROOT/"downloads/emulator-scoped-id.txt").read_text().strip())),encoding="utf-8")
    reports=[]
    for kind in ("diary","memory"):
        subprocess.run([sys.executable,"android/build.py","--notebook",kind,"--fixture","--device",str(target)],cwd=ROOT,check=True)
        package="cn.littlewhale."+kind
        adb("install","-r",str(ROOT/"dist"/("LittleWhale-"+kind+"-fixture.apk")))
        entry=dict(id="check-"+str(time.time_ns()),title="跨天保存测试",body="小鲸鱼喜欢蓝色的大海。\n第二行也要留下。")
        assert call(kind,"put",json.dumps(entry,ensure_ascii=False))["body"]==entry["body"]
        adb("shell","am","force-stop",package)
        rows=[];after=""
        while True:
            page=call(kind,"export",after)
            if not page["entries"]:break
            rows.extend(page["entries"]);after=page["after"]
        assert any(r["id"]==entry["id"] and r["body"]==entry["body"] for r in rows)
        output=adb("shell","run-as","com.termux","/system/bin/content","call","--uri","content://"+package+".entries","--method","export",check=False)
        assert "data64=" not in output,"Unrelated app read private notes"
        # The other app has a different Android sandbox and cannot alter the first app's database.
        adb("shell","am","start","-n",package+"/cn.littlewhale.notes.Notebook")
        time.sleep(0.5)
        adb("shell","uiautomator","dump","/sdcard/littlewhale-notebook-check.xml")
        ui=adb("shell","cat","/sdcard/littlewhale-notebook-check.xml")
        assert "跨天保存测试" in ui and "小鲸鱼喜欢蓝色的大海" in ui
        entry["body"]+="\n后台记录已自动更新。"
        call(kind,"put",json.dumps(entry,ensure_ascii=False))
        adb("shell","uiautomator","dump","/sdcard/littlewhale-notebook-check.xml")
        assert "后台记录已自动更新" in adb("shell","cat","/sdcard/littlewhale-notebook-check.xml")
        click(package,"修改")
        click(package,"保存")
        preserved=call(kind,"put",json.dumps(dict(entry,body="AI should not overwrite manual changes")))
        assert preserved["source"]=="manual" and preserved["body"]==entry["body"]
        click(package,"删除")
        click(package,"删除")
        preserved=call(kind,"put",json.dumps(entry))
        assert preserved["deleted"]==1,"AI resurrected a user-deleted record"
        bad=dict(entry,id="invalid;id")
        output=adb("shell","content","call","--uri","content://"+package+".entries","--method","put","--arg",json.dumps(bad,ensure_ascii=False),check=False)
        assert "data64=" not in output
        reports.append(dict(kind=kind,installed=True,unicode_saved=True,process_restart_preserved=True,ui_rendered=True,live_updates=True,manual_edit_preserved=True,deletion_preserved=True,foreign_app_rejected=True,invalid_id_rejected=True))
    (ROOT/"evidence/notebook-check.json").write_text(json.dumps(dict(target="emulator-5554",results=reports,external_model_calls=0),ensure_ascii=False,indent=2),encoding="utf-8")
    print("PASS: two Android notebooks, Unicode persistence, real UI, app isolation and input validation")

if __name__=="__main__":main()
