"""Pinned, private Termux update, embedded into the paired helper at build time."""
import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request


def main():
    import fcntl
    home=Path.home()
    if str(home)!="/data/data/com.termux/files/home":
        raise ValueError("Paired Termux required")
    data=home/".littlewhale"
    code=home/"LittleWhale"
    output=Path("/sdcard/LittleWhale-source/guest-maintenance.json")
    lock=(data/"update.lock").open("ab")
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    os.umask(0o077)
    report=dict(ok=False,running=True,pid=os.getpid(),started=time.time(),phase="verify")
    def publish():
        temporary=output.with_suffix(".tmp")
        temporary.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        temporary.replace(output)
    def api(path,body=None):
        url=(data/"panel-url").read_text("utf-8").strip()
        if not re.fullmatch(r"http://127\.0\.0\.1:8767/#[A-Za-z0-9_-]{43}",url):
            raise ValueError("Invalid private panel link")
        request=urllib.request.Request("http://127.0.0.1:8767"+path,
            None if body is None else json.dumps(body).encode(),
            {"Authorization":"Bearer "+url.split("#",1)[1],"Content-Type":"application/json"})
        with urllib.request.urlopen(request,timeout=8) as response:
            return json.loads(response.read(2_000_000))
    def pids():
        found=[]
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():continue
            try:
                args=entry.joinpath("cmdline").read_bytes().split(b"\0")
                # ponytail: VMOS reports cwd as /; require our private runtime, exact main/port args and one PID. Use a saved PID/start-time pair if launch arguments change.
                if entry.stat().st_uid==os.getuid() and (entry/"exe").resolve()==Path(sys.executable).resolve() and args[1:4]==[b"app.py",b"--port",b"8767"]:
                    found.append(int(entry.name))
            except (OSError,RuntimeError):pass
        return found
    def stop_core():
        found=pids()
        if len(found)>1:raise ValueError("Ambiguous core processes")
        if not found:return
        pid=found[0]
        if pid not in pids():raise ValueError("Core process changed")
        os.kill(pid,signal.SIGINT)
        until=time.monotonic()+75
        while pid in pids():
            if time.monotonic()>until:raise TimeoutError("Core is still stopping; do not restart")
            time.sleep(.25)
    def start_core():
        with (data/"runner-errors.log").open("ab") as errors:
            subprocess.Popen(["/data/data/com.termux/files/usr/bin/bash",str(code/"guest-start.sh"),"--no-browser"],
                cwd=code,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=errors,start_new_session=True)
        until=time.monotonic()+35
        while True:
            try:return api("/api/state")
            except (OSError,ValueError):
                if time.monotonic()>until:raise TimeoutError("Core startup not confirmed")
                time.sleep(.5)
    protected_columns={}
    def content_digest():
        result={}
        with closing(sqlite3.connect((data/"memory.sqlite3").as_uri()+"?mode=ro",uri=True)) as db:
            for table in ("people","messages","memory","diary"):
                # Keep every pre-update column, while allowing migrations to add new columns.
                if table not in protected_columns:
                    protected_columns[table]=[r[1] for r in db.execute("PRAGMA table_info("+table+")")]
                columns=",".join('"'+name.replace('"','""')+'"' for name in protected_columns[table])
                rows=db.execute("SELECT "+columns+" FROM "+table+" ORDER BY rowid").fetchall()
                result[table]=hashlib.sha256(json.dumps(rows,ensure_ascii=False).encode()).digest()
        return result
    previous=None
    stopped=False
    backup=None
    database_backup=None
    role=None
    publish()
    try:
        # Copy into private storage before checking hashes, so shared-storage changes cannot race execution.
        with tempfile.TemporaryDirectory(prefix=".littlewhale-update-",dir=home) as folder:
            stage=Path(folder)
            for name,expected in SOURCE_HASHES.items():
                blob=(Path("/sdcard/LittleWhale-source")/name).read_bytes()
                if hashlib.sha256(blob).hexdigest()!=expected:raise ValueError("Source hash mismatch")
                target=stage/name
                target.parent.mkdir(parents=True,exist_ok=True)
                target.write_bytes(blob)
            sys.path.insert(0,str(stage))
            from phone import Phone
            from whale import atomic,backup_database,LEGACY_ROLE,ROLE
            from app import acquire
            device=json.loads((data/"device.json").read_text("utf-8-sig"))
            report["phase"]="identity";publish()
            # Maintenance verifies the VM, not its foreground window; a background VM may have no focused app.
            Phone(device,Path(os.environ["PYTHONHOME"])/"bin/adb",device.get("adb_port",5039)).verify()
            report["phase"]="offline_checks";publish()
            with (data/"update-check.log").open("wb") as log:
                subprocess.run([sys.executable,"check.py"],cwd=stage,stdout=log,stderr=log,timeout=90,check=True)
            report["phase"]="inspect_core";publish()
            previous=api("/api/state")
            report["key_configured"]=previous["config"]["key_configured"]
            if len(pids())!=1:raise ValueError("Paired core process not unique")
            config=(data/"config.json").read_bytes()
            role=(data/"role.md").read_bytes()
            api("/api/pause",dict(paused=True))
            report["phase"]="drain";publish()
            # Deep-thinking requests can take 180s; allow them to unwind before touching data.
            until=time.monotonic()+210
            while True:
                with closing(sqlite3.connect((data/"memory.sqlite3").as_uri()+"?mode=ro",uri=True)) as db:
                    busy=db.execute("SELECT count(*) FROM events WHERE status='working'").fetchone()[0]
                if not busy:break
                if time.monotonic()>until:raise TimeoutError("Model call is still active")
                time.sleep(.5)
            before=content_digest()
            report["phase"]="stop";publish()
            stop_core();stopped=True
            backup=data/("code-backup-"+str(time.time_ns()))
            backup.mkdir()
            with acquire(data):
                backup_database(data/"memory.sqlite3",backup/"memory.sqlite3")
                database_backup=backup/"memory.sqlite3"
                for name in SOURCE_HASHES:
                    existing=code/name
                    if existing.exists():
                        saved=backup/name;saved.parent.mkdir(parents=True,exist_ok=True)
                        atomic(saved,existing.read_bytes())
                for name in SOURCE_HASHES:
                    target=code/name;target.parent.mkdir(parents=True,exist_ok=True)
                    atomic(target,(stage/name).read_bytes())
            report["phase"]="restart";publish()
            current=start_core()
            assert config==(data/"config.json").read_bytes(),"Configuration changed"
            assert before==content_digest(),"Personal data changed"
            new_role=(data/"role.md").read_text("utf-8")
            if role.decode()==LEGACY_ROLE:
                assert new_role==ROLE and (data/"role.before-personality.md").read_bytes()==role
            else:assert new_role==role.decode(),"Custom personality changed"
            api("/api/pause",dict(paused=previous["paused"]))
            report.update(ok=True,running=False,phase="done",finished=time.time(),
                role_migrated=role.decode()==LEGACY_ROLE,existing_role_preserved=new_role==role.decode(),custom_role_preserved=role.decode() not in (LEGACY_ROLE,ROLE),
                configuration_preserved=True,personal_data_preserved=True,
                key_configured=current["config"]["key_configured"],paused=previous["paused"],
                schema_before=previous.get("schema_version",0),schema_after=current["schema_version"],
                source_digest=hashlib.sha256(json.dumps(SOURCE_HASHES,sort_keys=True).encode()).hexdigest())
            publish()
    except Exception as error:
        report.update(running=False,error=type(error).__name__,failed_phase=report["phase"])
        if type(error) is ValueError:report["error_hint"]=str(error)[:200]
        try:
            if stopped:
                stop_core()
                if backup is not None:
                    with acquire(data):
                        for name in SOURCE_HASHES:
                            saved=backup/name
                            if saved.exists():
                                target=code/name;target.parent.mkdir(parents=True,exist_ok=True)
                                temporary=target.with_name(target.name+".rollback")
                                temporary.write_bytes(saved.read_bytes());temporary.replace(target)
                        if database_backup is not None:
                            backup_database(database_backup,data/"memory.sqlite3")
                            report["database_restored"]=True
                if role is not None:(data/"role.md").write_bytes(role)
                start_core()
            if previous is not None:api("/api/pause",dict(paused=previous["paused"]))
            report["rollback_confirmed"]=True
        except Exception:report["rollback_confirmed"]=False
        publish()


if __name__=="__main__":main()
