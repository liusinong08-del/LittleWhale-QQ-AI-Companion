"""Build the VM notification helper using the existing SDK, no downloaded dependencies."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SDK = Path(os.environ.get("ANDROID_SDK_ROOT") or os.environ.get("ANDROID_HOME") or str(ROOT.parent / "PocketPilot/.toolchain/sdk"))
TOOLS = SDK / "build-tools/35.0.0"
JAVA = Path(re.search(r"java.home\s*=\s*(.+)", subprocess.run(
    ["java", "-XshowSettings:properties", "-version"], capture_output=True, text=True).stderr).group(1).strip()) / "bin"
CORE_FILES=("whale.py","app.py","phone.py","social.py","speech.py","index.html","check.py","guest-start.sh","guest-open.sh",
            "vendor/ai-companion/humanize.js","vendor/ai-companion/LICENSE","vendor/ai-companion/UPSTREAM.json")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", type=Path, default=ROOT / "downloads/device.json")
    parser.add_argument("--fixture", action="store_true")
    parser.add_argument("--host", action="store_true", help="Build the physical-phone cloud client; never enables VM control")
    parser.add_argument("--notebook", choices=("diary", "memory"))
    parser.add_argument("--app", action="store_true")
    args = parser.parse_args()
    if args.app and (args.notebook or args.fixture):
        parser.error("--app builds only the paired production chat app")
    target = json.loads(args.device.read_text("utf-8-sig"))
    if not re.fullmatch(r"[0-9a-f]{16}", target["android_id"]) or type(target["sdk"]) is not int or not 24 <= target["sdk"] <= (36 if args.host else 35):
        raise ValueError("Invalid paired VM")
    build = ROOT / "downloads" / ("android-fixture-build" if args.fixture else "android-build")
    # Fresh build directories avoid deletion and stale classes from previous versions.
    import tempfile
    build.mkdir(exist_ok=True)
    build = Path(tempfile.mkdtemp(prefix="build-", dir=build))
    for name in ("classes", "dex"):
        (build / name).mkdir()
    android = SDK / "platforms/android-35/android.jar"
    def run(*values):
        subprocess.run([os.path.relpath(v, ROOT) if isinstance(v, Path) and v.drive == ROOT.drive else str(v) for v in values], cwd=ROOT, check=True)
    generated = build / "Target.java"
    hashes={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in CORE_FILES}
    update="SOURCE_HASHES="+repr(hashes)+"\n"+(ROOT/"android/update_core.py").read_text("utf-8")
    update64="" if args.fixture else base64.b64encode(update.encode()).decode()
    check=ROOT/"android/p0_check.py"
    check64="" if args.fixture or not check.exists() else base64.b64encode(check.read_text("utf-8").encode()).decode()
    # JVM constants are limited to 65535 bytes; assemble the maintenance payload at runtime.
    check_expression='new StringBuilder()'+''.join('.append("'+check64[i:i+24000]+'")' for i in range(0,len(check64),24000))+'.toString()'
    generated.write_text('package cn.littlewhale.bridge; final class Target { static final boolean HOST='+str(args.host).lower()+'; static final String ID="' + target["android_id"] +
                         '"; static final int SDK=' + str(target["sdk"]) + '; static final String FIXTURE="' +
                         ("cn.littlewhale.bridge" if args.fixture else "") + '"; static final String UPDATE="'+update64+'"; static final String P0CHECK='+check_expression+'; }', encoding="utf-8")
    if args.notebook:
        generated.write_text('package cn.littlewhale.notes; final class Target { static final boolean HOST='+str(args.host).lower()+'; static final String ID="'+target["android_id"]+'"; static final int SDK='+str(target["sdk"])+'; static final String KIND="'+args.notebook+'"; }',encoding="utf-8")
    elif args.app:
        generated.write_text('package cn.littlewhale.app; final class Target { static final boolean HOST='+str(args.host).lower()+'; static final String ID="'+target["android_id"]+'"; static final int SDK='+str(target["sdk"])+'; }',encoding="utf-8")
    manifest = build / "AndroidManifest.xml"
    contents = (ROOT / "android/AndroidManifest.xml").read_text("utf-8")
    if args.notebook:
        contents=(ROOT/"android/NotebookManifest.xml").read_text("utf-8").replace("cn.littlewhale.diary", "cn.littlewhale."+args.notebook).replace("小鲸鱼日记", "小鲸鱼日记" if args.notebook=="diary" else "小鲸鱼记忆")
    elif args.app:
        contents=(ROOT/"android/WebAppManifest.xml").read_text("utf-8")
    elif args.fixture:
        contents = contents.replace("</application>", '<activity android:name=".Fixture" android:exported="true" />'
                                    '<receiver android:name=".Fixture$Receiver" android:exported="false" /></application>')
    icon = ROOT/"android/icons"/(("app" if args.app else args.notebook or "bridge")+".png")
    contents=contents.replace("<application ", '<application android:icon="@mipmap/ic_launcher" android:roundIcon="@mipmap/ic_launcher" ')
    resources=build/"res/mipmap-xxxhdpi"
    resources.mkdir(parents=True)
    (resources/"ic_launcher.png").write_bytes(icon.read_bytes())
    run(TOOLS/"aapt2.exe","compile","--dir",build/"res","-o",build/"resources.zip")
    manifest.write_text(contents, encoding="utf-8")
    run(TOOLS / "aapt2.exe", "link", "-o", build / "base.apk", "-I", android, "--manifest", manifest, build/"resources.zip")
    sources = [ROOT / "android/Main.java", generated]
    if args.notebook:
        sources=[ROOT/"android/Notebook.java",generated]
    elif args.app:
        sources=[ROOT/"android/WebApp.java",generated]
    elif args.fixture:
        sources.append(ROOT / "android/Fixture.java")
    run(JAVA / "javac.exe", "-encoding", "UTF-8", "--release", "8", "-classpath", android, "-d", build / "classes", *sources)
    jar = build / "classes.jar"
    with zipfile.ZipFile(jar, "w") as archive:
        for path in (build / "classes").rglob("*.class"):
            archive.write(path, path.relative_to(build / "classes").as_posix())
    run(JAVA / "java.exe", "-cp", TOOLS / "lib/d8.jar", "com.android.tools.r8.D8", "--min-api", "24", "--lib", android, "--output", build / "dex", jar)
    with zipfile.ZipFile(build / "base.apk", "a") as archive:
        for path in (build / "dex").glob("*.dex"):
            archive.write(path, path.name)
    run(TOOLS / "zipalign.exe", "-f", "4", build / "base.apk", build / "aligned.apk")
    key = ROOT / "downloads/bridge-debug.keystore"
    if not key.exists():
        run(JAVA / "keytool.exe", "-genkeypair", "-keystore", key, "-storepass", "android", "-keypass", "android",
            "-alias", "androiddebugkey", "-dname", "CN=LittleWhale Local Development", "-keyalg", "RSA", "-validity", "10000")
    output = ROOT / "dist" / ("LittleWhale-bridge-fixture.apk" if args.fixture else "LittleWhale-bridge.apk")
    if args.notebook:
        output=ROOT/"dist"/("LittleWhale-"+args.notebook+("-fixture" if args.fixture else "")+".apk")
    elif args.app:
        output=ROOT/"dist/LittleWhale.apk"
    if args.host:output=output.with_stem(output.stem+"-host")
    output.parent.mkdir(exist_ok=True)
    run(JAVA / "java.exe", "-jar", TOOLS / "lib/apksigner.jar", "sign", "--ks", key, "--ks-pass", "pass:android", "--out", output, build / "aligned.apk")
    run(JAVA / "java.exe", "-jar", TOOLS / "lib/apksigner.jar", "verify", output)
    print(json.dumps(dict(apk=str(output), sha256=hashlib.sha256(output.read_bytes()).hexdigest(), target_sdk=target["sdk"], fixture=args.fixture)))


if __name__ == "__main__":
    main()
