"""One emulator check of real NotificationListener -> core -> RemoteInput delivery.

Only installs on the explicitly named, dedicated emulator-5554. No account login,
external network, model key, or real QQ/WeChat messages. Run from project root.
"""
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import Handler, Server
from social import Social
from whale import Whale

ADB = ROOT.parent / "PocketPilot/.toolchain/sdk/platform-tools/adb.exe"


def adb(*args, check=True):
    if args and args[0] == "shell":
        args = ("shell", shlex.join(map(str, args[1:])))
    result = subprocess.run([str(ADB), "-P", "5038", "-s", "emulator-5554", *map(str, args)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    if check and result.returncode:
        raise RuntimeError(result.stderr or result.stdout)
    return result.stdout.strip()


def wait_until(predicate, seconds=15):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.25)
    raise AssertionError("Emulator check timed out")


def verify_emulator():
    device = dict(android_id=adb("shell", "settings", "get", "secure", "android_id"), sdk=int(adb("shell", "getprop", "ro.build.version.sdk")))
    # MuMu spoofs phone hardware and has no ro.kernel.qemu. Verify its measured identity.
    assert device == dict(android_id="6950311bd4cf06e9", sdk=32), "Paired dedicated emulator required"
    assert adb("shell", "getprop", "ro.product.cpu.abi") == "x86_64"
    return device


def tap_helper(x, y):
    # MuMu gives each app a separate display; default input hits the launcher instead.
    display, matches = None, []
    for line in adb("shell", "dumpsys", "activity", "activities").splitlines():
        match = re.match(r"Display #(\d+)", line)
        if match:
            display = int(match[1])
        if "topResumedActivity=" in line and "cn.littlewhale.bridge/.Main " in line:
            matches.append(display)
    assert len(matches) == 1 and matches[0] is not None, "Helper's current display is ambiguous"
    adb("shell", "input", "-d", matches[0], "tap", x, y)


def helper_click(label, checked=None):
    import xml.etree.ElementTree as ET
    adb("shell", "uiautomator", "dump", "/sdcard/whale-check.xml")
    tree = ET.fromstring(adb("shell", "cat", "/sdcard/whale-check.xml"))
    node = next(n for n in tree.iter("node") if n.get("text") == label and n.get("package") == "cn.littlewhale.bridge")
    if checked is not None and (node.get("checked") == "true") == checked:
        return
    x1, y1, x2, y2 = map(int, re.findall(r"\d+", node.get("bounds")))
    assert x2 > x1 and y2 > y1, "Helper control is off screen"
    tap_helper((x1+x2)//2, (y1+y2)//2)


def recovery_check():
    """Use the emulator's already installed Termux and Python; preserve its settings."""
    verify_emulator()
    import textwrap
    native_home = "/data/data/com.termux/files/home"
    native_python = "/data/data/com.termux/files/usr/bin/python3.14"
    def python(code):
        return adb("shell", "run-as", "com.termux", native_python, "-c", textwrap.dedent(code))
    def stop_owned():
        python('''
            from pathlib import Path
            import os, signal
            home=Path("/data/data/com.termux/files/home/LittleWhale").resolve()
            for process in Path("/proc").iterdir():
                if not process.name.isdigit(): continue
                try:
                    if process.joinpath("cwd").resolve()!=home: continue
                    args=process.joinpath("cmdline").read_bytes().split(b"\\0")
                    if b"/data/data/com.termux/files/home/LittleWhale/guest-start.sh" in args:
                        os.kill(int(process.name),signal.SIGTERM)
                    elif b"app.py" in args:
                        os.kill(int(process.name),signal.SIGINT)
                except (OSError,RuntimeError): pass
        ''')
    # Files are exclusively this project's; existing unrelated Termux files are not replaced.
    existing = adb("shell", "run-as", "com.termux", "ls", "files/home/LittleWhale", check=False)
    if existing:
        assert adb("shell", "run-as", "com.termux", "cat", "files/home/LittleWhale/.emulator-check") == "LittleWhale-Recovery-Check-v1"
        stop_owned()
    adb("shell", "run-as", "com.termux", "mkdir", "-p", "files/home/LittleWhale")
    python('from pathlib import Path; Path("/data/data/com.termux/files/home/LittleWhale/.emulator-check").write_text("LittleWhale-Recovery-Check-v1")')
    adb("shell","run-as","com.termux","mkdir","-p",native_home+"/LittleWhale/vendor/ai-companion")
    for name in ("app.py", "whale.py", "social.py", "phone.py", "index.html", "guest-start.sh", "guest-open.sh", "vendor/ai-companion/humanize.js", "vendor/ai-companion/LICENSE", "vendor/ai-companion/UPSTREAM.json"):
        remote="/data/local/tmp/littlewhale-"+Path(name).name
        adb("push", name, remote)
        adb("shell", "run-as", "com.termux", "cp", remote, native_home + "/LittleWhale/" + name)
    python('''
        from pathlib import Path
        import re
        home=Path("/data/data/com.termux/files/home")
        runtime=home/".littlewhale-runtime-emulator"
        if not runtime.exists(): runtime.symlink_to("/data/data/com.termux/files/usr", target_is_directory=True)
        (home/"LittleWhale/.runtime-path").write_text(str(runtime)+"\\n")
        path=home/".termux/termux.properties"
        backup=path.with_name("termux.properties.littlewhale-test-backup")
        assert path.exists() and not backup.exists(), "Inspect existing Termux settings backup before rerunning"
        old=path.read_bytes()
        backup.write_bytes(old)
        value=re.sub(r"(?m)^\\s*allow-external-apps\\s*=.*$", "", old.decode("utf-8"))
        path.write_text(value+"\\nallow-external-apps=true\\n")
    ''')
    def health():
        try:
            request=urllib.request.Request("http://127.0.0.1:18769/health",headers={"Host":"127.0.0.1:8767"})
            with urllib.request.urlopen(request, timeout=1) as response:
                return json.load(response)
        except OSError:
            return None
    def signal_core(number):
        python('''
            from pathlib import Path
            import os
            found=[]
            for process in Path("/proc").iterdir():
                if not process.name.isdigit(): continue
                try:
                    if process.joinpath("cwd").resolve()==Path("/data/data/com.termux/files/home/LittleWhale").resolve() and b"app.py" in process.joinpath("cmdline").read_bytes().split(b"\\0"):
                        found.append(int(process.name))
                except (OSError,RuntimeError): pass
            assert len(found)==1, "Core process identity ambiguous"
            os.kill(found[0], SIGNAL)
        '''.replace("SIGNAL", str(number)))
    try:
        adb("shell", "am", "broadcast", "-a", "com.termux.app.reload_style", "com.termux")
        adb("shell", "pm", "grant", "cn.littlewhale.bridge", "com.termux.permission.RUN_COMMAND")
        adb("forward", "tcp:18769", "tcp:8767")
        adb("shell", "am", "start", "-n", "cn.littlewhale.bridge/.Main")
        helper_click("虚拟机重启后启动小鲸鱼", checked=True)
        helper_click("启动或恢复小鲸鱼")
        wait_until(lambda: health() is not None, 25)
        print("PASS: Termux background core started", flush=True)
        started=health()["started"]
        signal_core(9)
        wait_until(lambda: health() is not None and health()["started"]>started, 30)
        print("PASS: killed Python child recovered", flush=True)
        restarted=health()["started"]
        signal_core(2)
        wait_until(lambda: health() is None)
        # Invoke the receiver logic inside the fixture APK. This is not a real system boot test.
        adb("shell", "am", "start", "-n", "cn.littlewhale.bridge/.Fixture", "--ez", "boot_check", "true")
        wait_until(lambda: health() is not None and health()["started"]>restarted, 25)
        record=dict(target="emulator-5554", real_termux_background_start=True,
                    killed_python_recovered=True, boot_receiver_logic=True,
                    actual_system_boot_test=False, real_phone_vm_verified=False,
                    external_model_calls=0)
        (ROOT/"evidence/android-recovery-check.json").write_text(json.dumps(record,indent=2),encoding="utf-8")
        print("PASS: real Termux background start, Python crash recovery and boot receiver logic (not an actual OS reboot)")
    finally:
        stop_owned()
        if health():
            wait_until(lambda: health() is None)
        python('''
            from pathlib import Path
            path=Path("/data/data/com.termux/files/home/.termux/termux.properties")
            backup=path.with_name("termux.properties.littlewhale-test-backup")
            path.write_bytes(backup.read_bytes())
            backup.unlink()
        ''')
        adb("shell", "am", "broadcast", "-a", "com.termux.app.reload_style", "com.termux")
        adb("shell", "pm", "revoke", "cn.littlewhale.bridge", "com.termux.permission.RUN_COMMAND", check=False)
        adb("shell", "am", "force-stop", "cn.littlewhale.bridge", check=False)
        adb("forward", "--remove", "tcp:18769", check=False)


def network_check():
    verify_emulator()
    adb("install", "-r", ROOT / "dist/LittleWhale-bridge-fixture.apk")
    report_path="/sdcard/Android/data/cn.littlewhale.bridge/files/network-check.json"
    adb("shell", "rm", "-f", report_path)
    adb("shell", "am", "start", "-n", "cn.littlewhale.bridge/.Main", "--ez", "network_check", "true")
    reports=[]
    def finished():
        raw=adb("shell", "cat", report_path, check=False)
        if not raw.startswith("{"): return False
        report=json.loads(raw)
        assert {item["domain"] for item in report["results"]}=={"api-docs.deepseek.com","im.qq.com","weixin.qq.com"}
        assert all(type(item["https_reachable"]) is bool for item in report["results"])
        reports.append(report)
        return True
    try:
        wait_until(finished,60)
        record=dict(target="emulator-5554",diagnostic_completed=True,report=reports[-1])
        (ROOT/"evidence/android-network-check.json").write_text(json.dumps(record,indent=2),encoding="utf-8")
        print("PASS: native Android HTTPS diagnostic completed and saved a complete report",flush=True)
    finally:
        adb("shell", "am", "force-stop", "cn.littlewhale.bridge", check=False)


def main():
    device = verify_emulator()
    path = ROOT / "downloads/emulator-device.json"
    scoped = ROOT / "downloads/emulator-scoped-id.txt"
    if scoped.exists():
        device["android_id"] = scoped.read_text("utf-8").strip()
    path.write_text(json.dumps(device), encoding="utf-8")
    subprocess.run([sys.executable, str(ROOT / "android/build.py"), "--device", str(path), "--fixture"], cwd=ROOT, check=True)
    adb("install", "-r", ROOT / "dist/LittleWhale-bridge-fixture.apk")
    adb("reverse", "tcp:8767", "tcp:8767")
    calls = []
    def model(config, messages):
        calls.append(messages)
        return dict(lines=["收到你的海风。", "我在自己的小手机里。"]), {}
    with tempfile.TemporaryDirectory() as temporary:
        whale = Whale(temporary, model)
        social = Social(whale, {"cn.littlewhale.bridge": "本地模拟器通知"})
        with Server(("127.0.0.1", 8767), Handler) as server:
            server.whale, server.social, server.token = whale, social, "offline-test-admin"
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                adb("shell", "am", "start", "-n", "cn.littlewhale.bridge/.Main", "--es", "token", social.token)
                # Read the form's actual bounds and app display; never tap the default launcher display.
                adb("shell", "uiautomator", "dump", "/sdcard/whale-check.xml")
                import xml.etree.ElementTree as ET
                tree = ET.fromstring(adb("shell", "cat", "/sdcard/whale-check.xml"))
                save = next((n for n in tree.iter("node") if n.get("text") == "保存配对"), None)
                if save is None:
                    diagnostic = "\n".join(n.get("text", "") for n in tree.iter("node"))
                    match = re.search(r"配对诊断：32 / ([0-9a-f]{16})", diagnostic)
                    if match and not scoped.exists():
                        scoped.write_text(match.group(1), encoding="utf-8")
                        raise ValueError("Android 8+ scopes ANDROID_ID to the app signing key; measured helper ID saved, rerun check")
                    raise AssertionError("Helper device identity mismatch")
                x1, y1, x2, y2 = map(int, re.findall(r"\d+", save.get("bounds")))
                tap_helper((x1+x2)//2, (y1+y2)//2)
                adb("shell", "cmd", "notification", "allow_listener", "cn.littlewhale.bridge/cn.littlewhale.bridge.Main$Listener")
                wait_until(lambda: whale.setting("bridge_status", {}).get("connected"), 20)
                # Delete only the disposable fixture's previous reply file, not application data.
                adb("shell", "run-as", "cn.littlewhale.bridge", "rm", "-f", "files/replies.json")
                posted = int(time.time() * 1000)
                def post(content, at):
                    adb("shell", "am", "start", "-n", "cn.littlewhale.bridge/.Fixture", "--es", "title", "Owner", "--es", "text", content, "--el", "time", at)
                post("discover", posted)
                wait_until(lambda: bool(social.snapshot()["threads"]))
                thread = social.snapshot()["threads"][0]
                assert not thread["enabled"] and thread["replyable"] and not calls
                social.configure(dict(thread=thread["id"], kind="private", owner=True, enabled=True, trigger=""))
                post("hello", posted + 1000)
                def replies():
                    raw = adb("shell", "run-as", "cn.littlewhale.bridge", "cat", "files/replies.json", check=False)
                    return json.loads(raw) if raw.startswith("[") else []
                expected = ["收到你的海风。", "我在自己的小手机里。"]
                wait_until(lambda: replies() == expected, 25)
                wait_until(lambda: all(d["status"] == "submitted" for d in social.snapshot()["deliveries"]))
                assert len(calls) == 1 and len(social.snapshot()["deliveries"]) == 2
                post("hello", posted + 1000)
                time.sleep(2)
                assert replies() == expected and len(calls) == 1
                whale.set_setting("paused", True)
                post("paused", posted + 2000)
                wait_until(lambda: whale.db.execute("SELECT 1 FROM social_inbox WHERE status='paused'").fetchone())
                assert replies() == expected and len(calls) == 1
                record = dict(check="NotificationListener -> loopback HTTP -> Python model stub -> durable outbox -> RemoteInput -> BroadcastReceiver",
                              target="emulator-5554", sdk=device["sdk"], reply_count=len(expected), model_calls=len(calls),
                              unbound_silent=True, duplicate_silent=True, pause_silent=True, real_qq_wechat_verified=False)
                (ROOT / "evidence/android-notification-check.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
                print("PASS: real Android notification, explicit binding, two Chinese RemoteInput replies, deduplication and pause")
            finally:
                if not whale.setting("bridge_status", {}).get("connected"):
                    print(adb("logcat", "-d", "-s", "LittleWhale:W"))
                    raw = adb("shell", "run-as", "cn.littlewhale.bridge", "cat", "shared_prefs/bridge.xml", check=False)
                    if raw.startswith("<?xml"):
                        stored = ET.fromstring(raw).find("string[@name='token']")
                        print("Pair token matches:", stored is not None and stored.text == social.token)
                adb("shell", "cmd", "notification", "disallow_listener", "cn.littlewhale.bridge/cn.littlewhale.bridge.Main$Listener", check=False)
                adb("shell", "am", "force-stop", "cn.littlewhale.bridge", check=False)
                adb("reverse", "--remove", "tcp:8767", check=False)
                server.shutdown()
                worker.join(3)
                whale.close()
    recovery_check()
    network_check()


if __name__ == "__main__":
    if sys.argv[1:] == ["--recovery-only"]:
        recovery_check()
    elif sys.argv[1:] == ["--network-only"]:
        network_check()
    else:
        main()
