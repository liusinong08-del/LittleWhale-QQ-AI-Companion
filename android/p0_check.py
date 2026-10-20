"""P0 environment probe and bounded, fixed setup actions for the paired Termux VM, embedded into the helper at build time."""
import base64
import json
import hashlib
import io
import os
import re
import socket
import ssl
import subprocess
import sys
import time
import tarfile
from pathlib import Path, PurePosixPath

ACTIONS = ("net", "inspect", "inspect2", "inspect3", "prootprobe", "nativeprobe", "nativeqq", "nativenode", "qemuprobe", "qemustart", "qemustop", "qemurestart", "nativeinspect", "nativelog", "napcatstatus", "napcatconfig", "napcatopen", "napcatqr", "napcatqrfresh", "napcatconnect", "autologin", "qqinspect", "modelprobe", "webprobe", "roleupdate", "speechsetup", "speechprobe", "unlimited", "aptfix", "dpkgfix", "unpack24", "proot", "debian")
SPEECH_RUNTIMES={'native':'87524ec977bd99b05cab7bd83a94ddc9bfcc8dc232ea783a5b345af1d5e1a29b',
                 'asr':'fab35c84f082015d729df50a04bea840df843383b44616438872b74d7a5d5c30',
                 'tts':'72bdcf11f54086f0b0ca7d649259e26fed22f04448cc94fb8469b21b87b700a0'}
DEBIAN_IMAGE="debian:bookworm-slim@sha256:a1b86db52ce3daef089e45aabe36dfec4091f82464c25c1fdcf03de197cbe82a"
DEBIAN_NAME="littlewhale-debian"
GLIBC_ZIP_SHA256="8161c8bed37c1034a2d68ff49cc00b30dfd0a1a6977e946992fb1612dc900f9a"
QQ_DEB_SHA256="181b895cb573548e396e54bb0548cca92ffdc1b1a4b018fa4551bb062385fdca"
QQ_DEPS_SHA256="4ec6e52ce26d1f1f587a4beef1abf39550e9fc446fbb54b6ca68cc2b57f4e149"
NAPCAT_ZIP_SHA256="735f2e633247df3ffd6e8ee8b84d7039c315b2655f5807e5f0029f93d2c30016"
NODE_ZIP_SHA256="f383deac163605114655e598025db180732b2911c95e7a98bdfb484fb5299eb1"
QEMU_ZIP_SHA256="090932b80bcdfe35e04b971de17b68bd1596f75c7aab684e82b0b16e28ff8807"
NODE_ADDON_SHA256="db2d643bc380f78b728a7e6e4660fd4cf68e600655c0ef8cfbaac6144ee235f1"
ADDON_CHECK_JS="const os=require('node:os');const m={exports:{}};process.dlopen(m,process.argv[1],os.constants.dlopen.RTLD_NOW|os.constants.dlopen.RTLD_GLOBAL);console.log('HOST_ADDON_OK');const w={exports:{}};process.dlopen(w,process.argv[2]);if(typeof w.exports.NodeIKernelLoginService?.get!=='function')throw Error('Missing QQ login service');console.log('WRAPPER_ADDON_OK');process.exit(0);"


def verified_file(path, expected):
    digest=hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda:source.read(1024*1024),b""):digest.update(block)
    if digest.hexdigest()!=expected:raise ValueError("Archive hash mismatch: "+path.name)


def unpack_native_zip(source,target,expected):
    """Install only a verified, materialized runtime into a new private directory."""
    import zipfile
    import tempfile
    verified_file(source,expected)
    if target.exists():
        if (target/".littlewhale-source-sha256").read_text()!=expected:raise ValueError("Existing native runtime is not ours")
        return
    staging=Path(tempfile.mkdtemp(prefix=".littlewhale-glibc-",dir=target.parent))
    with zipfile.ZipFile(source) as archive:
        if sum(item.file_size for item in archive.infolist())>250_000_000:raise ValueError("Oversized native runtime")
        for item in archive.infolist():
            path=PurePosixPath(item.orig_filename)
            if path.is_absolute() or ".." in path.parts or "\\" in item.orig_filename or ":" in item.orig_filename:raise ValueError("Unsafe runtime path")
            if item.is_dir():continue
            destination=staging/path
            destination.parent.mkdir(parents=True,exist_ok=True)
            destination.write_bytes(archive.read(item))
            destination.chmod(0o755 if item.external_attr>>16&0o111 else 0o600)
    (staging/".littlewhale-source-sha256").write_text(expected)
    staging.rename(target)


def setup_speech(home,shared,*,tts=False):
    from whale import atomic
    if type(tts) is not bool:raise ValueError('Invalid speech component selection')
    data=home/'.littlewhale';data.mkdir(mode=0o700,exist_ok=True)
    path=data/'speech.json'
    config=json.loads(path.read_text('utf-8')) if path.exists() else {}
    if not isinstance(config,dict):raise ValueError('Invalid private speech configuration')
    components=[kind for kind in SPEECH_RUNTIMES if kind!='tts' or tts]
    for kind in components:
        digest=SPEECH_RUNTIMES[kind]
        folder=home/('.littlewhale-speech-'+kind+'-'+digest[:12])
        unpack_native_zip(shared/('speech-'+kind+'.zip'),folder,digest)
        config[kind]=str(folder)
    atomic(path,json.dumps(config).encode())
    return dict(prepared=True,tts_setting_changed=False,components=components)


def patch_deb(source, target, expected):
    """Remove only inaccessible ancestor directory metadata; keep file/link payloads intact."""
    raw=source.read_bytes()
    if not re.fullmatch(r"[0-9a-f]{64}",expected) or hashlib.sha256(raw).hexdigest()!=expected:
        raise ValueError("Package does not match the APT index: "+source.name)
    if not raw.startswith(b"!<arch>\n"):raise ValueError("Invalid Debian archive")
    prefix="data/data/com.termux/files/usr"
    ancestors={"", ".", "data", "data/data", "data/data/com.termux", "data/data/com.termux/files",prefix}
    position=8
    while position<len(raw):
        header=raw[position:position+60]
        if len(header)!=60 or header[58:]!=b"`\n":raise ValueError("Invalid archive header")
        name=header[:16].decode("ascii").strip().rstrip("/")
        size=int(header[48:58])
        body=position+60
        if size<0 or body+size>len(raw):raise ValueError("Truncated archive")
        if name.startswith("data.tar"):
            mode={"data.tar.xz":"w:xz","data.tar.gz":"w:gz"}.get(name)
            if mode is None:raise ValueError("Unsupported data compression")
            buffer=io.BytesIO()
            dropped=0
            with tarfile.open(fileobj=io.BytesIO(raw[body:body+size]),mode="r:*") as original:
                options={"preset":1} if name.endswith(".xz") else {"compresslevel":1}
                with tarfile.open(fileobj=buffer,mode=mode,format=tarfile.GNU_FORMAT,**options) as repacked:
                    for item in original:
                        path=PurePosixPath(item.name)
                        if path.is_absolute() or ".." in path.parts:raise ValueError("Unsafe package path")
                        normalized=path.as_posix()
                        if item.isdir() and normalized in ancestors:
                            dropped+=1
                            continue
                        if not normalized.startswith(prefix+"/"):raise ValueError("Package writes outside Termux prefix")
                        # Do not dereference symlinks/hardlinks: some point forward or outside this tar member.
                        repacked.addfile(item,original.extractfile(item) if item.isfile() else None)
            packed=buffer.getvalue()
            new_header=header[:48]+str(len(packed)).ljust(10).encode("ascii")+header[58:]
            target.write_bytes(raw[:position]+new_header+packed+(b"\n" if len(packed)%2 else b"")+raw[body+size+size%2:])
            return dropped
        position=body+size+size%2
    raise ValueError("Missing Debian data member")


def main():
    import fcntl
    home = Path.home()
    if str(home) != "/data/data/com.termux/files/home":
        raise ValueError("Paired Termux required")
    prefix = Path("/data/data/com.termux/files/usr")
    maintenance=home/".littlewhale-maintenance"
    maintenance.mkdir(mode=0o700,exist_ok=True)
    lock=(maintenance/"p0.lock").open("ab")
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    shared = Path("/sdcard/LittleWhale-source")
    output = shared / "guest-p0-check.json"
    marker = shared / "p0-action.json"
    report = dict(ok=False, running=True, pid=os.getpid(), started=time.time(), phase="system")

    def publish():
        temporary = output.with_suffix(".tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(output)

    def run(argv, timeout=1800):
        try:
            env=dict(os.environ,PATH=str(prefix/"bin")+":/system/bin:/system/xbin",
                     LD_LIBRARY_PATH=str(prefix/"lib"),DEBIAN_FRONTEND="noninteractive",TMPDIR=str(prefix/"tmp"))
            # Child Termux Python must use its installed libraries, not the core's private PYTHONHOME.
            for key in ("PYTHONHOME","PYTHONPATH","VIRTUAL_ENV"):env.pop(key,None)
            result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                env=env,start_new_session=True)
            return dict(code=result.returncode, out=result.stdout.strip()[-6000:], err=result.stderr.strip()[-2000:])
        except subprocess.TimeoutExpired as error:
            def tail(value,limit):return (value.decode("utf-8",errors="replace") if isinstance(value,bytes) else value or "")[-limit:]
            return dict(error="TimeoutExpired",out=tail(error.stdout,6000),err=tail(error.stderr,2000))
        except Exception as error:
            return dict(error=type(error).__name__)

    action = None
    payload = {}
    forced = os.environ.get("LITTLEWHALE_P0_ACTION", "")
    if forced:
        action = forced
        report["action"] = action
    elif marker.exists():
        try:
            payload = json.loads(marker.read_text("utf-8"))
            action = payload.get("action")
        except Exception:
            action = "invalid"
        report["action"] = action
    publish()
    try:
        if forced and forced not in ("qemustop", "qemustart", "qemurestart"):raise ValueError("Invalid managed P0 action")
        if action is not None and action not in ACTIONS:raise ValueError("Unknown P0 action")
        if action not in ("napcatstatus","napcatconfig","napcatopen","napcatqr","napcatconnect","autologin","qqinspect","modelprobe","webprobe","roleupdate","speechsetup","speechprobe","nativeinspect","nativelog","qemustop","qemurestart"):
            report["when"] = run([str(prefix / "bin/bash"), "-c", "date +'%Y-%m-%d %H:%M %z'"], timeout=60)
            report["arch"] = run(["/system/bin/uname", "-m"], timeout=60)
            report["android_release"] = run(["/system/bin/getprop", "ro.build.version.release"], timeout=60)
            report["apt"] = run([str(prefix / "bin/apt"), "--version"], timeout=60)
            report["proot"] = run([str(prefix / "bin/bash"), "-c", "command -v proot && proot --version"], timeout=60)
            report["proot_distro"] = run([str(prefix / "bin/bash"), "-c", "command -v proot-distro && proot-distro --version"], timeout=60)
            report["installed"] = run([str(prefix / "bin/bash"), "-c", "dpkg-query -W -f='${binary:Package} ${Version}\\n' proot proot-distro curl tar 2>/dev/null || true"], timeout=60)
            report["storage"] = run(["/system/bin/df", "-k", "/data", "/sdcard"], timeout=60)
            report["sizes"] = run([str(prefix / "bin/bash"), "-c", "du -sk ~/.littlewhale ~/LittleWhale 2>/dev/null || true"], timeout=300)
            report["termux_storage_link"] = bool((home / "storage").exists() or (home / "storage").is_symlink())
            report["rootfs"]={}
            for layout in ("containers","installed-rootfs"):
                rootfs=prefix/"var/lib/proot-distro"/layout
                report["rootfs"][layout]=sorted(entry.name for entry in rootfs.iterdir()) if rootfs.exists() else []
            runtime = Path(os.environ.get("PYTHONHOME", ""))
            import sqlite3
            report["runtime"] = dict(path=str(runtime), python=sys.version.split()[0], sqlite=sqlite3.sqlite_version,
                cert=(runtime / "etc/tls/cert.pem").exists(), adb=(runtime / "bin/adb").exists())
            probe = socket.socket()
            probe.settimeout(3)
            try:
                probe.connect(("127.0.0.1", 8767))
                report["core_loopback"] = True
            except OSError:
                report["core_loopback"] = False
            finally:
                probe.close()
            report["staged_sources"] = sorted(entry.name for entry in shared.iterdir())[:60] if shared.exists() else []
        if action == "net":
            report["phase"] = "net"
            publish()
            report["https"] = {}
            for host in ("packages-cf.termux.dev", "github.com", "deb.debian.org"):
                entry = {}
                try:
                    entry["dns"] = sorted({info[4][0] for info in socket.getaddrinfo(host, 443)})
                except OSError as error:
                    entry["dns_error"] = type(error).__name__
                try:
                    raw = socket.create_connection((host, 443), timeout=10)
                    with ssl.create_default_context().wrap_socket(raw, server_hostname=host) as tls:
                        entry["tls"] = tls.version()
                except Exception as error:
                    entry["tls_error"] = type(error).__name__
                report["https"][host] = entry
            report["phase"] = "net_done"
            publish()
        if action == "inspect":
            report["phase"] = "inspect"
            publish()
            report["dpkg_tmp"] = run([str(prefix / "bin/bash"), "-c",
                "ls -la /data/data/ 2>&1 | grep -i dpkg || echo none"], timeout=60)
            report["tmp_leftovers"] = run([str(prefix / "bin/bash"), "-c",
                "ls -la " + str(prefix / "lib") + " | grep dpkg-tmp || echo no-tmp"], timeout=60)
            deb = None
            for folder in (prefix / "var/cache/apt/archives", Path("/data/data/com.termux/cache/apt/archives")):
                found = sorted(folder.glob("ncurses_*.deb")) if folder.exists() else []
                if found:
                    deb = found[0]
                    break
            if deb is not None:
                report["deb"] = dict(path=str(deb), size=deb.stat().st_size, sha256=hashlib.sha256(deb.read_bytes()).hexdigest())
                report["deb_odd"] = run([str(prefix / "bin/bash"), "-c",
                    "dpkg-deb -c " + str(deb) + " | awk '{print $6}' | grep -vE '^./data/data/com.termux/files/usr/' | head -20"], timeout=120)
                report["deb_head"] = run([str(prefix / "bin/bash"), "-c",
                    "dpkg-deb -c " + str(deb) + " | head -12"], timeout=120)
            report["db_list"] = run([str(prefix / "bin/bash"), "-c",
                "dpkg -L ncurses 2>&1 | head -15; echo ---STATUS; dpkg-query -W -f='${Status}\\n' ncurses"], timeout=60)
            report["apt_dirs"] = run([str(prefix / "bin/bash"), "-c",
                "apt-config dump 2>/dev/null | grep -iE 'Dir::(Root|State|Cache)'"], timeout=60)
            report["phase"] = "inspect_done"
            publish()
        if action == "dpkgfix":
            report["phase"] = "restore"
            publish()
            deb = None
            for folder in (prefix / "var/cache/apt/archives", Path("/data/data/com.termux/cache/apt/archives")):
                found = sorted(folder.glob("ncurses_*.deb")) if folder.exists() else []
                if found:
                    deb = found[0]
                    break
            fixdir = prefix / "tmp/ncurses-fix"
            if fixdir.exists():
                run([str(prefix / "bin/bash"), "-c", "rm -rf " + str(fixdir)], timeout=60)
            report["extract"] = run([str(prefix / "bin/dpkg-deb"), "-x", str(deb), str(fixdir)], timeout=300)
            report["restore"] = run([str(prefix / "bin/bash"), "-c",
                "cp -a " + str(fixdir) + "/data/data/com.termux/files/usr/. " + str(prefix) + "/ && echo COPIED"], timeout=600)
            report["tmp_found"] = run([str(prefix / "bin/bash"), "-c",
                "rm -f " + str(prefix / "lib") + "/*.dpkg-tmp; find " + str(prefix) + " -name '*.dpkg-tmp' 2>/dev/null | head -30"], timeout=120)
            report["tmp_removed"] = run([str(prefix / "bin/bash"), "-c",
                "find " + str(prefix) + " -name '*.dpkg-tmp' -delete 2>/dev/null; find " + str(prefix) + " -name '*.dpkg-tmp' 2>/dev/null | wc -l"], timeout=120)
            report["cleanup"] = run([str(prefix / "bin/bash"), "-c", "rm -rf " + str(fixdir)], timeout=120)
            report["phase"] = "status"
            publish()
            status_path = prefix / "var/lib/dpkg/status"
            blob = status_path.read_bytes()
            if not (shared / "dpkg-status-before-fix.txt").exists():
                (shared / "dpkg-status-before-fix.txt").write_bytes(blob)
            fixed, count = re.subn(rb"(Package: ncurses\n(?:.*\n)*?Status: )deinstall reinstreq unpacked", rb"\1install ok installed", blob)
            lines = fixed.split(b"\n")
            out = []
            inblock = False
            dropped = 0
            for line in lines:
                if line == b"Package: ncurses":
                    inblock = True
                elif line == b"":
                    inblock = False
                if inblock and line.startswith(b"Config-Version:"):
                    dropped += 1
                    continue
                out.append(line)
            fixed = b"\n".join(out)
            report["status_patched"] = count
            report["config_version_dropped"] = dropped
            if count == 1 or dropped:
                temporary = status_path.with_suffix(".fixed")
                temporary.write_bytes(fixed)
                temporary.replace(status_path)
            report["phase"] = "verify"
            publish()
            report["audit_after"] = run([str(prefix / "bin/bash"), "-c",
                "dpkg --audit; echo ---STATUS; dpkg-query -W -f='${binary:Package} ${Status} ${Version}\\n' ncurses; echo ---CONFIGURE"], timeout=300)
            report["configure"] = run([str(prefix / "bin/dpkg"), "--configure", "-a"], timeout=900)
            report["audit_final"] = run([str(prefix / "bin/bash"), "-c",
                "dpkg --audit; echo ---; dpkg-query -W -f='${Status}\\n' ncurses; echo ---; ls -l " + str(prefix / "lib/libncursesw.so*") + " 2>&1"], timeout=300)
            report["phase"] = "dpkgfix_done"
            publish()
        if action == "inspect2":
            report["phase"] = "stat"
            publish()
            paths = {}
            for name in ("/data", "/data/data", "/data/data.dpkg-tmp", "/data/.dpkg-tmp",
                         "/data/data/com.termux.dpkg-tmp", "/data/data/com.termux"):
                try:
                    st = os.lstat(name)
                    paths[name] = dict(mode=oct(st.st_mode), uid=st.st_uid, gid=st.st_gid, size=st.st_size)
                except OSError as error:
                    paths[name] = type(error).__name__
            report["paths"] = paths
            report["states"] = run([str(prefix / "bin/bash"), "-c",
                "dpkg-query -W -f='${binary:Package} ${Status} ${Version}\\n' "
                "proot proot-distro python python-pip libtalloc libandroid-shmem libandroid-posix-semaphore "
                "libcrypt libexpat libsqlite libffi gdbm glib libxml2 libllvm clang lld llvm make pkg-config "
                "ndk-sysroot libcompiler-rt ncurses-ui-libs python-ensurepip-wheels ncurses 2>&1"], timeout=120)
            report["tools"] = run([str(prefix / "bin/bash"), "-c",
                "command -v proot-distro; command -v python3.14; command -v clang; command -v ar; command -v xz; "
                "proot-distro --version 2>&1 | head -2; python3.14 --version 2>&1"], timeout=120)
            report["audit"] = run([str(prefix / "bin/dpkg"), "--audit"], timeout=120)
            report["phase"] = "inspect2_done"
            publish()
        if action == "inspect3":
            report["phase"]="inspect3";publish()
            report["links"]={}
            for name in ("bin/python3.14","bin/proot-distro","lib/libpython3.14.so","lib/libc++.so","lib/libexpat.so","lib/libexpat.so.1",
                         "lib/libcrypt.so","lib/libcrypt.so.2","lib/clang","lib/clang/21","lib/clang/21/include"):
                path=prefix/name
                try:
                    info=path.lstat()
                    item=dict(mode=oct(info.st_mode),file=path.is_file(),directory=path.is_dir(),resolved=str(path.resolve()))
                    if path.is_symlink():item["link"]=os.readlink(path)
                    report["links"][name]=item
                except OSError as error:report["links"][name]=type(error).__name__
            report["prefix_python"]=run([str(prefix/"bin/python3.14"),"--version"],timeout=60)
            report["private_libs_python"]=run(["/system/bin/env","LD_LIBRARY_PATH="+str(runtime/"lib"),str(prefix/"bin/python3.14"),"--version"],timeout=60)
            report["private_runtime_pd"]=run(["/system/bin/env","LD_LIBRARY_PATH="+str(runtime/"lib"),"PYTHONHOME="+str(runtime),
                "PYTHONPATH="+str(prefix/"lib/python3.14/site-packages"),sys.executable,str(prefix/"bin/proot-distro"),"--version"],timeout=60)
            report["prefix_pd"]=run([str(prefix/"bin/proot-distro"),"--version"],timeout=60)
            report["phase"]="inspect3_done";publish()
        if action == "aptfix":
            report["phase"] = "audit"
            publish()
            report["audit"] = run([str(prefix / "bin/bash"), "-c",
                "dpkg --audit; echo ---STATUS; dpkg-query -W -f='${binary:Package} ${Status} ${Version}\\n' ncurses ncurses-utils ncurses-ui-libs 2>&1; "
                "echo ---POLICY; apt-cache policy ncurses; echo ---DEPS; apt-cache depends proot proot-distro; "
                "echo ---LIBS; ls -l " + str(prefix / "lib") + "/libncursesw* 2>&1"], timeout=300)
            report["phase"] = "reinstall"
            publish()
            report["reinstall"] = run([str(prefix / "bin/apt-get"), "install", "--reinstall", "-y", "ncurses"], timeout=900)
            if report["reinstall"].get("code") != 0:
                report["phase"] = "forcerm"
                publish()
                report["force_remove"] = run([str(prefix / "bin/dpkg"), "--remove", "--force-remove-reinstreq", "ncurses"], timeout=600)
                report["force_install"] = run([str(prefix / "bin/apt-get"), "install", "-y", "ncurses"], timeout=900)
            report["phase"] = "configure"
            publish()
            report["configure"] = run([str(prefix / "bin/dpkg"), "--configure", "-a"], timeout=900)
            report["audit_after"] = run([str(prefix / "bin/bash"), "-c",
                "dpkg --audit; echo ---STATUS; dpkg-query -W -f='${binary:Package} ${Status} ${Version}\\n' ncurses 2>&1"], timeout=300)
            report["phase"] = "aptfix_done"
            publish()
        if action == "unpack24":
            import shutil
            report["phase"] = "backup"
            publish()
            status_path = prefix / "var/lib/dpkg/status"
            saved=maintenance/("dpkg-before-"+str(time.time_ns()))
            shutil.copytree(prefix/"var/lib/dpkg",saved,symlinks=True)
            report["dpkg_backup"]=str(saved)
            report["status_sha256"]=hashlib.sha256(status_path.read_bytes()).hexdigest()

            names = ("libtalloc", "libcrypt", "libexpat", "libandroid-posix-semaphore", "libandroid-shmem",
                     "gdbm", "ncurses-ui-libs", "libffi", "libsqlite", "pkg-config", "make", "libxml2",
                     "ndk-sysroot", "proot", "proot-distro", "python-ensurepip-wheels", "python", "python-pip",
                     "libcompiler-rt", "clang", "lld", "glib", "libllvm", "llvm")
            folders = (prefix / "var/cache/apt/archives", Path("/data/data/com.termux/cache/apt/archives"))
            fixed = prefix / "tmp/fixed"
            fixed.mkdir(parents=True, exist_ok=True)
            report["phase"] = "patch"
            publish()
            patched = {}
            missing = []
            for name in names:
                report["package"]=name
                publish()
                source = None
                for folder in folders:
                    hits = sorted(folder.glob(name + "_*.deb")) if folder.exists() else []
                    if hits:
                        source = hits[0]
                        break
                if source is None:
                    missing.append(name)
                    continue
                target = fixed / source.name
                try:
                    version=run([str(prefix/"bin/dpkg-deb"),"-f",str(source),"Version"],timeout=60)
                    if version.get("code")!=0:raise ValueError("Cannot read package version")
                    metadata=run([str(prefix/"bin/apt-cache"),"show",name+"="+version["out"]],timeout=60)
                    checksums=re.findall(r"(?m)^SHA256: ([0-9a-f]{64})$",metadata.get("out",""))
                    checksum=hashlib.sha256(source.read_bytes()).hexdigest()
                    if metadata.get("code")!=0 or checksum not in checksums:raise ValueError("Package absent from verified APT cache")
                    dropped = patch_deb(source, target,checksum)
                    patched[name] = dict(dropped=dropped, source=str(source),
                                         source_sha256=checksum,sha256=hashlib.sha256(target.read_bytes()).hexdigest())
                except Exception as error:
                    patched[name] = type(error).__name__ + ": " + str(error)[:120]
            report["missing_debs"] = missing
            report["patched"] = patched
            publish()
            if missing or any(not isinstance(patched.get(name),dict) for name in names):
                raise ValueError("Package validation/transform incomplete; no packages unpacked")
            report["phase"] = "unpack"
            publish()
            unpacked = {}
            report["unpacked"]=unpacked
            for name in names:
                report["package"]=name
                entry = patched.get(name)
                if not isinstance(entry, dict):
                    continue
                result = run([str(prefix / "bin/dpkg"), "--unpack", str(fixed / Path(entry["source"]).name)], timeout=1800)
                if result.get("code") == 0:
                    unpacked[name] = 0
                else:
                    unpacked[name] = dict(code=result.get("code", result.get("error")), err=result.get("err", "")[-400:])
                publish()
            report["unpacked"] = unpacked
            report["phase"] = "configure"
            publish()
            passes = []
            for _ in range(3):
                result = run([str(prefix / "bin/dpkg"), "--configure", "-a"], timeout=1800)
                passes.append(dict(code=result.get("code"), out=result.get("out", "")[-1200:], err=result.get("err", "")[-600:]))
                publish()
                if result.get("code") == 0:
                    break
            report["configure_passes"] = passes
            report["phase"] = "verify"
            publish()
            report["states"] = run([str(prefix / "bin/bash"), "-c",
                "dpkg-query -W -f='${binary:Package} ${Status} ${Version}\\n' " + " ".join(names) + " 2>&1"], timeout=120)
            report["audit"] = run([str(prefix / "bin/dpkg"), "--audit"], timeout=120)
            report["tools"] = run([str(prefix / "bin/bash"), "-c",
                "command -v proot-distro; command -v clang; proot-distro --version 2>&1 | head -1; proot --version | head -1"], timeout=120)
            report["phase"] = "unpack24_done"
            publish()
            if any(value!=0 for value in unpacked.values()) or not passes or passes[-1]["code"]!=0 or report["audit"].get("code")!=0 or report["audit"].get("out"):
                raise ValueError("Package repair incomplete; inspect unpack/configure/audit results")
        if action == "proot":
            report["phase"] = "apt"
            publish()
            report["apt_update"] = run([str(prefix / "bin/apt-get"), "update"], timeout=900)
            report["apt_install"] = run([str(prefix / "bin/apt-get"), "install", "-y", "proot", "proot-distro"], timeout=900)
            report["versions"] = run([str(prefix / "bin/bash"), "-c", "dpkg-query -W -f='${binary:Package} ${Version}\\n' proot proot-distro"], timeout=60)
            report["proot"] = run([str(prefix / "bin/bash"), "-c", "proot --version"], timeout=60)
            report["phase"] = "apt_done"
            publish()
            if report["apt_update"].get("code")!=0 or report["apt_install"].get("code")!=0 or report["proot"].get("code")!=0:
                raise ValueError("PRoot installation/check failed")
        if action == "debian":
            report["phase"] = "rootfs"
            report["image"]=DEBIAN_IMAGE
            report["container_name"]=DEBIAN_NAME
            publish()
            pd=str(prefix/"bin/proot-distro")
            existing=prefix/"var/lib/proot-distro/containers"/DEBIAN_NAME
            report["debian_install"]=(dict(code=0,out="Existing project container retained",err="") if (existing/"manifest.json").is_file()
                else run([pd,"install",DEBIAN_IMAGE,"--name",DEBIAN_NAME,"--architecture","aarch64"],timeout=3600))
            if report["debian_install"].get("code")!=0:raise ValueError("Pinned Debian install failed; existing data retained")
            report["debian_list"] = run([pd,"list"], timeout=120)
            report["rootfs_size"] = run([str(prefix/"bin/du"),"-sk",str(existing)], timeout=600)
            report["phase"] = "container"
            publish()
            inside = ('cat /etc/os-release; test "$(uname -m)" = aarch64; '
                '. /etc/os-release; test "$VERSION_ID" = 12; getent hosts deb.debian.org; '
                'apt-get update -qq; apt-get install -y --no-install-recommends ca-certificates curl; '
                'curl --fail --max-time 30 --output /dev/null https://deb.debian.org/debian/README; echo PROOT_MINIMAL_OK')
            report["debian_login"] = run([pd,"login","--isolated",DEBIAN_NAME,"--","/bin/sh","-eu","-c",inside], timeout=1800)
            report["phase"] = "container_done"
            publish()
            if report["debian_login"].get("code")!=0 or "PROOT_MINIMAL_OK" not in report["debian_login"].get("out",""):
                raise ValueError("Debian command/DNS/HTTPS checks failed")
        if action == "prootprobe":
            root=prefix/"var/lib/proot-distro/containers"/DEBIAN_NAME/"rootfs"
            report["rootfs_files"]={}
            for name in ("bin/true","bin/sh","usr/bin/bash","lib/ld-linux-aarch64.so.1","lib/aarch64-linux-gnu/ld-linux-aarch64.so.1"):
                path=root/name
                report["rootfs_files"][name]=dict(file=path.is_file(),link=os.readlink(path) if path.is_symlink() else None)
            pd=str(prefix/"bin/proot-distro")
            report["variants"]={}
            for name,args in (
                ("no_seccomp_isolated",[pd,"login","--isolated",DEBIAN_NAME,"--","/bin/true"]),
                ("no_seccomp_minimal",[pd,"login","--minimal",DEBIAN_NAME,"--","/bin/true"]),
                ("no_seccomp_direct",[str(prefix/"bin/proot"),"-r",str(root),"-w","/","/bin/true"])):
                report["phase"]=name;publish()
                report["variants"][name]=run(["/system/bin/env","PROOT_NO_SECCOMP=1",*args],timeout=30)
                publish()
            # Distinguish broken PRoot translation from the kernel's ability to execute glibc at all.
            report["phase"]="glibc_direct";publish()
            report["glibc_direct"]=run(["/system/bin/env","LD_PRELOAD=","LD_LIBRARY_PATH=",
                str(root/"lib/aarch64-linux-gnu/ld-linux-aarch64.so.1"),"--library-path",
                str(root/"lib/aarch64-linux-gnu")+":"+str(root/"usr/lib/aarch64-linux-gnu"),str(root/"usr/bin/true")],timeout=30)
            if not any(item.get("code")==0 for item in report["variants"].values()):
                raise ValueError("No PRoot execution mode passed; do not deploy Linux QQ yet")
        if action == "nativeprobe":
            import shlex
            root=prefix/"var/lib/proot-distro/containers"/DEBIAN_NAME/"rootfs"
            glibc=prefix/"glibc"
            report["phase"]="native_glibc_unpack";publish()
            unpack_native_zip(shared/"glibc-native.zip",glibc,GLIBC_ZIP_SHA256)
            loader=str(glibc/"lib/ld-linux-aarch64.so.1")
            libraries=str(glibc/"lib")+":"+str(root/"usr/lib/aarch64-linux-gnu")
            # Keep glibc paths out of Android children's bionic linker environment.
            launch=["/system/bin/env","LD_PRELOAD=","LD_LIBRARY_PATH=",loader,"--library-path",libraries]
            report["native"]={}
            for label,args in (("true",[str(root/"usr/bin/true")]),("patcher",[str(glibc/"bin/patchelf"),"--version"]),
                ("shell_child",[str(root/"usr/bin/bash"),"--noprofile","--norc","-c",shlex.join([loader,"--library-path",libraries,str(root/"usr/bin/true")])+" && echo NATIVE_CHILD_OK"])):
                report["phase"]="native_"+label;publish()
                report["native"][label]=run(launch+args,timeout=30)
                publish()
            if any(item.get("code")!=0 for item in report["native"].values()):raise ValueError("Native glibc/child process check failed")
            report["phase"]="native_qq_unpack";publish()
            qq=home/".littlewhale-napcat/opt/QQ"
            source=shared/"linuxqq_3.2.34-53644_arm64.deb"
            verified_file(source,QQ_DEB_SHA256)
            if not qq.exists():
                qq.parent.mkdir(parents=True,exist_ok=True)
                with source.open("rb") as archive:
                    if archive.read(8)!=b"!<arch>\n":raise ValueError("Invalid QQ archive")
                    while header:=archive.read(60):
                        if len(header)!=60 or header[58:]!=b"`\n":raise ValueError("Invalid QQ archive member")
                        size=int(header[48:58]);start=archive.tell()
                        if header[:16].decode().strip().startswith("data.tar"):
                            with tarfile.open(fileobj=archive,mode="r|*") as contents:
                                for item in contents:
                                    path=PurePosixPath(item.name)
                                    if path.is_absolute() or ".." in path.parts:raise ValueError("Unsafe QQ payload path")
                                    if path.as_posix().startswith("opt/QQ/") and item.isfile():
                                        item.name=path.as_posix()[len("opt/QQ/"):]
                                        contents.extract(item,path=qq,filter="data")
                            break
                        archive.seek(start+size+size%2)
                    else:raise ValueError("QQ data missing")
            if json.loads((qq/"resources/app/package.json").read_text())["version"]!="3.2.34-53644":raise ValueError("QQ version mismatch")
            report["phase"]="native_qq_libraries";publish()
            report["qq_libraries"]=run(launch+["--list",str(qq/"qq")],timeout=30)
            if report["qq_libraries"].get("code")!=0:raise ValueError("Native QQ needs additional libraries; no login attempted")
        if action == "nativeqq":
            import shutil
            base=home/".littlewhale-napcat"
            qq=base/"opt/QQ"
            glibc=prefix/"glibc"
            deps=base/"deps"
            report["phase"]="native_qq_dependencies";publish()
            unpack_native_zip(shared/"qq-dependencies.zip",deps,QQ_DEPS_SHA256)
            libraries=":".join(map(str,(glibc/"lib",deps/"lib",qq)))
            loader=str(glibc/"lib/ld-linux-aarch64.so.1")
            launch=["/system/bin/env","LD_PRELOAD=","LD_LIBRARY_PATH=",loader,"--library-path",libraries]
            report["qq_libraries"]=run(launch+["--list",str(qq/"qq")],timeout=30)
            if report["qq_libraries"].get("code")!=0:raise ValueError("QQ shared-library check failed")
            report["phase"]="native_qq_patch";publish()
            report["patches"]={}
            for name in ("qq","chrome_crashpad_handler"):
                binary=qq/name
                if not binary.is_file():continue
                backup=binary.with_name(name+".littlewhale-original")
                if not backup.exists():shutil.copy2(binary,backup)
                report["patches"][name]=run(launch+[str(glibc/"bin/patchelf"),"--set-interpreter",loader,"--force-rpath","--set-rpath",libraries,str(binary)],timeout=180)
                if report["patches"][name].get("code")!=0:raise ValueError("QQ ELF relocation failed")
            report["phase"]="native_qq_node";publish()
            node=["/system/bin/env","LD_PRELOAD=","LD_LIBRARY_PATH=","ELECTRON_RUN_AS_NODE=1",str(qq/"qq")]
            report["node"]=run(node+["-e","console.log('NODE_OK',process.platform,process.arch,process.versions.node);const cp=require('node:child_process');const r=cp.spawnSync(process.execPath,['-e',\"console.log('NODE_CHILD_OK')\"],{encoding:'utf8'});console.log(r.stdout);if(r.status!==0){console.error(r.error||r.stderr);process.exit(1)}"],timeout=30)
            report["phase"]="native_xvfb_help";publish()
            report["xvfb_help"]=run(launch+[str(deps/"bin/Xvfb"),"-help"],timeout=30)
            if report["node"].get("code")!=0 or "NODE_CHILD_OK" not in report["node"].get("out",""):
                raise ValueError("QQ Node/child execution check failed")
        if action == "nativenode":
            base=home/".littlewhale-napcat"
            qq=base/"opt/QQ"
            node=base/"node"
            glibc=prefix/"glibc"
            unpack_native_zip(shared/"node-native.zip",node,NODE_ZIP_SHA256)
            root=prefix/"var/lib/proot-distro/containers"/DEBIAN_NAME/"rootfs"
            libraries=":".join(map(str,(root/"lib/aarch64-linux-gnu",root/"usr/lib/aarch64-linux-gnu",base/"deps/lib",qq)))
            loader=str(root/"lib/aarch64-linux-gnu/ld-linux-aarch64.so.1")
            launch=["/system/bin/env","LD_PRELOAD=","LD_LIBRARY_PATH=",str(glibc/"lib/ld-linux-aarch64.so.1"),"--library-path",str(glibc/"lib")]
            report["phase"]="native_node_patch";publish()
            report["node_patch"]=run(launch+[str(glibc/"bin/patchelf"),"--set-interpreter",loader,"--force-rpath","--set-rpath",libraries,str(node/"node")],timeout=180)
            if report["node_patch"].get("code")!=0:raise ValueError("Native Node relocation failed")
            report["phase"]="native_node_check";publish()
            command=["/system/bin/env","LD_PRELOAD=","LD_LIBRARY_PATH=",str(node/"node")]
            report["node_check"]=run(command+["--jitless","-e","console.log('NODE_OK',process.versions.node,process.arch);const c=require('node:child_process').spawnSync(process.execPath,['--jitless','-e',\"console.log('NODE_CHILD_OK')\"],{encoding:'utf8'});console.log(c.stdout);if(c.status!==0){console.error(c.error||c.stderr);process.exit(1)}"],timeout=30)
            if report["node_check"].get("code")!=0:raise ValueError("Native Node check failed")
            report["phase"]="native_node_qq_addon";publish()
            report["node_addon"]=run(command+["-e",ADDON_CHECK_JS,str(qq/"resources/app/major.node"),str(qq/"resources/app/wrapper.node")],timeout=30)
            if report["node_addon"].get("code")!=0:raise ValueError("QQ addon does not yet load in standalone Node")
        if action == "qemuprobe":
            import zipfile
            base=home/".littlewhale-napcat"
            qemu=base/"qemu"
            root=prefix/"var/lib/proot-distro/containers"/DEBIAN_NAME/"rootfs"
            report["phase"]="qemu_unpack";publish()
            unpack_native_zip(shared/"qemu-native.zip",qemu,QEMU_ZIP_SHA256)
            report["qemu_linkers"]={}
            command=None
            for label,candidate in (("inherited",[str(qemu/"bin/qemu-aarch64")]),
                ("private",["/system/bin/env","LD_LIBRARY_PATH="+str(qemu/"lib"),str(qemu/"bin/qemu-aarch64")]),
                ("combined",["/system/bin/env","LD_LIBRARY_PATH="+str(qemu/"lib")+":"+str(prefix/"lib"),str(qemu/"bin/qemu-aarch64")])):
                report["qemu_linkers"][label]=run(candidate+["--version"],timeout=30)
                if report["qemu_linkers"][label].get("code")==0:command=candidate;break
            report["qemu_version"]=report["qemu_linkers"][label]
            if report["qemu_version"].get("code")!=0:raise ValueError("QEMU runtime did not start")
            libraries=":".join(map(str,(root/"lib/aarch64-linux-gnu",root/"usr/lib/aarch64-linux-gnu",base/"deps/lib",base/"opt/QQ")))
            command.extend(["-L",str(root),"-U","LD_PRELOAD","-E","LD_LIBRARY_PATH="+libraries])
            verified_file(shared/"node-native.zip",NODE_ZIP_SHA256)
            original=base/"node/node.unmodified"
            with zipfile.ZipFile(shared/"node-native.zip") as archive:original.write_bytes(archive.read("node"))
            original.chmod(0o700)
            report["qemu_checks"]={}
            for label,args in (("true",[str(root/"usr/bin/true")]),("node_version",[str(original),"--version"]),
                ("node_eval",[str(original),"-e","console.log('QEMU_NODE_OK',process.platform,process.arch);const m=new WebAssembly.Module(Uint8Array.from([0,97,115,109,1,0,0,0,1,5,1,96,0,1,127,3,2,1,0,7,10,1,6,97,110,115,119,101,114,0,0,10,6,1,4,0,65,42,11]));if(new WebAssembly.Instance(m).exports.answer()!==42)throw Error('Wasm failed');console.log('QEMU_WASM_OK');setTimeout(()=>console.log('QEMU_TIMER_OK'),10)"])):
                report["phase"]="qemu_"+label;publish()
                report["qemu_checks"][label]=run(command+args,timeout=45)
                publish()
                if report["qemu_checks"][label].get("code")!=0:raise ValueError("QEMU check failed: "+label)
            report["phase"]="qemu_qq_addon";publish()
            qq=base/"opt/QQ"
            # libbugly expects symbols normally exported by Electron's GnuTLS dependency.
            command.extend(["-E","LD_PRELOAD="+str(base/"deps/lib/libgnutls.so.30")])
            verified_file(shared/"node_addon.node",NODE_ADDON_SHA256)
            addon=base/"node_addon.node";addon.write_bytes((shared/"node_addon.node").read_bytes());addon.chmod(0o600)
            report["qemu_addon"]=run(command+[str(original),"-e",ADDON_CHECK_JS,str(addon),str(qq/"resources/app/wrapper.node")],timeout=60)
            if report["qemu_addon"].get("code")!=0 or "WRAPPER_ADDON_OK" not in report["qemu_addon"].get("out",""):raise ValueError("QQ native addon check failed under QEMU")
            (base/"qq-addon-validated.json").write_text(json.dumps(dict(node=NODE_ZIP_SHA256,addon=NODE_ADDON_SHA256,qq=QQ_DEB_SHA256,wasm=True)))
        if action in ("qemustop","qemurestart"):
            import signal
            base=home/".littlewhale-napcat"
            pidfile=base/"native-qq.pid"
            if pidfile.exists():
                pid=int(pidfile.read_text())
                process=Path("/proc")/str(pid)
                if process.exists():
                    if process.joinpath("exe").resolve()!=base/"qemu/bin/qemu-aarch64" or str(base/"load.cjs").encode() not in process.joinpath("cmdline").read_bytes().split(b"\0"):
                        raise ValueError("Saved PID is not our NapCat QEMU process")
                    os.kill(pid,signal.SIGTERM)
                    for attempt in range(20):
                        if not process.exists():break
                        time.sleep(.5)
                    if process.exists():raise ValueError("NapCat did not stop yet; no forced kill performed")
            report["napcat_stopped"]=True
        if action in ("qemustart","qemurestart"):
            import secrets
            import urllib.request
            base=home/".littlewhale-napcat"
            qq=base/"opt/QQ"
            if json.loads((base/"qq-addon-validated.json").read_text())!=dict(node=NODE_ZIP_SHA256,addon=NODE_ADDON_SHA256,qq=QQ_DEB_SHA256,wasm=True):raise ValueError("QQ addon and Wasm must pass the real VM check first")
            napcat=qq/"resources/app/app_launcher/napcat"
            report["phase"]="native_napcat_unpack";publish()
            unpack_native_zip(shared/"NapCat.Shell.v4.18.38.zip",napcat,NAPCAT_ZIP_SHA256)
            config=napcat/"config/webui.json"
            if not config.exists():
                config.write_text(json.dumps(dict(host="127.0.0.1",port=6099,token=secrets.token_urlsafe(32),autoLoginAccount="",disableWebUI=False)))
                config.chmod(0o600)
            if json.loads(config.read_text()).get("host")!="127.0.0.1":raise ValueError("WebUI must be loopback only")
            bootstrap=base/"load.cjs"
            bootstrap.write_text("const os=require('node:os');process.dlopen({exports:{}},"+json.dumps(str(base/"node_addon.node"))+",os.constants.dlopen.RTLD_NOW|os.constants.dlopen.RTLD_GLOBAL);import(require('node:url').pathToFileURL("+json.dumps(str(napcat/"napcat.mjs"))+").href);\n")
            pidfile=base/"native-qq.pid"
            if pidfile.exists():
                pid=int(pidfile.read_text())
                try:os.kill(pid,0)
                except ProcessLookupError:pass
                else:raise ValueError("Previous native QQ process is still alive; do not start twice")
            for folder in (base/"home",base/"config",base/"runtime",base/"tmp"):
                folder.mkdir(mode=0o700,parents=True,exist_ok=True)
            env=dict(os.environ,LD_LIBRARY_PATH=str(base/"qemu/lib"),HOME=str(base/"home"),XDG_CONFIG_HOME=str(base/"config"),XDG_RUNTIME_DIR=str(base/"runtime"),TMPDIR=str(base/"tmp"))
            for key in ("PYTHONHOME","PYTHONPATH","VIRTUAL_ENV","ELECTRON_RUN_AS_NODE"):env.pop(key,None)
            # ponytail: one Node process avoids untranslated child exec; recovery needs a separately verified watchdog.
            env.update(NAPCAT_FORCE_NODE_PROCESS="1",NAPCAT_DISABLE_MULTI_PROCESS="1",NAPCAT_DISABLE_PIPE="1",NAPCAT_DISABLE_BYPASS="1",
                NAPCAT_WRAPPER_PATH=str(qq/"resources/app/wrapper.node"),NAPCAT_QQ_PACKAGE_INFO_PATH=str(qq/"resources/app/package.json"))
            digest=base/"quick-password-md5"
            if digest.exists():
                value=digest.read_text("utf-8").strip()
                if not re.fullmatch(r"[0-9a-f]{32}",value):raise ValueError("Invalid quick password digest")
                account=str(json.loads(config.read_text("utf-8")).get("autoLoginAccount",""))
                if not re.fullmatch(r"[1-9][0-9]{4,11}",account):raise ValueError("Auto-login account missing")
                env.update(NAPCAT_QUICK_ACCOUNT=account,NAPCAT_QUICK_PASSWORD_MD5=value)
                report["password_fallback"]=True
            root=prefix/"var/lib/proot-distro/containers"/DEBIAN_NAME/"rootfs"
            libraries=":".join(map(str,(root/"lib/aarch64-linux-gnu",root/"usr/lib/aarch64-linux-gnu",base/"deps/lib",qq,qq/"resources/app")))
            command=[str(base/"qemu/bin/qemu-aarch64"),"-L",str(root),"-U","LD_PRELOAD","-E","LD_PRELOAD="+str(base/"deps/lib/libgnutls.so.30"),"-E","LD_LIBRARY_PATH="+libraries,
                str(base/"node/node.unmodified"),str(bootstrap)]
            log=base/("native-qq-"+str(time.time_ns())+".log")
            pidfile.write_text(str(os.getpid()));pidfile.chmod(0o600)
            # Replace the tracked Termux task; an orphaned daemon lost its maintenance parent in VMOS.
            report.update(running=False,phase="launch_submitted",napcat=dict(pid=os.getpid(),log_private=True,startup_verified=False))
            publish()
            marker.unlink(missing_ok=True)
            with log.open("wb") as output:
                log.chmod(0o600)
                os.dup2(output.fileno(),1);os.dup2(output.fileno(),2)
            with open(os.devnull,"rb") as source:os.dup2(source.fileno(),0)
            os.chdir(qq)
            os.execve(command[0],command,env)
        if action == "nativeinspect":
            base=home/".littlewhale-napcat"
            logs=sorted(base.glob("native-qq-*.log"))
            report["native_logs"]=[]
            for path in logs[-3:]:
                text=path.read_text(errors="replace")[-20000:]
                report["native_logs"].append(dict(file=path.name,bytes=path.stat().st_size,
                    module_seen="[NapCat]" in text,errors=[line[:700] for line in text.splitlines()
                        if ("FATAL:" in line or "ERROR:" in line or "Error:" in line or "error while loading" in line or "Missing X server" in line)
                        and not re.search(r"https?://|token|qrcode",line,re.I)][-15:]))
            if (base/"native-qq.pid").exists():
                pid=int((base/"native-qq.pid").read_text());report["native_qq_pid"]=pid
                try:os.kill(pid,0);report["native_qq_alive"]=True
                except ProcessLookupError:report["native_qq_alive"]=False
            report["memory"]=Path("/proc/meminfo").read_text().splitlines()[:5]
        if action == "nativelog":
            base=home/".littlewhale-napcat"
            logs=sorted(base.glob("native-qq-*.log"),key=lambda path:path.stat().st_mtime)
            if not logs:raise ValueError("No NapCat log yet")
            text=logs[-1].read_text(errors="replace")[-60000:]
            text=re.sub(r"https?://[^\s\"']+","[url]",text)
            text=re.sub(r"(?i)(token|secret|password|qrcode)[^,\s]*[=:]\s*[^\s,]+",r"\1=[redacted]",text)
            lines=[line[:400] for line in text.splitlines()
                if re.search(r"(?i)login|登录|quick|auto|扫码|scan|error|fail|offline|online|disconnect|reconnect",line)]
            report["log_file"]=logs[-1].name
            report["log_tail"]=lines[-80:]
        if action == "napcatconfig":
            base=home/".littlewhale-napcat"
            config=base/"opt/QQ/resources/app/app_launcher/napcat/config/webui.json"
            value=json.loads(config.read_text("utf-8"))
            report["auto_login_account"]=value.get("autoLoginAccount","")
            report["config_keys"]=sorted(value)
        if action == "autologin":
            base=home/".littlewhale-napcat"
            config=base/"opt/QQ/resources/app/app_launcher/napcat/config/webui.json"
            account=str(payload.get("account",""))
            if not re.fullmatch(r"[1-9][0-9]{4,11}",account):raise ValueError("Invalid QQ account")
            value=json.loads(config.read_text("utf-8"))
            value["autoLoginAccount"]=account
            temporary=config.with_suffix(".tmp")
            temporary.write_text(json.dumps(value,ensure_ascii=False),encoding="utf-8")
            temporary.chmod(0o600)
            temporary.replace(config)
            report["auto_login_account"]=account
        if action in ("napcatstatus","napcatopen","napcatqr","napcatqrfresh","napcatconnect"):
            import urllib.request
            import urllib.parse
            base=home/".littlewhale-napcat"
            config=json.loads((base/"opt/QQ/resources/app/app_launcher/napcat/config/webui.json").read_text())
            if config.get("host")!="127.0.0.1" or config.get("port")!=6099:raise ValueError("Unexpected NapCat endpoint")
            endpoint="http://127.0.0.1:6099"
            def api(path,body,credential=None):
                headers={"Content-Type":"application/json"}
                if credential:headers["Authorization"]="Bearer "+credential
                request=urllib.request.Request(endpoint+"/api/"+path,json.dumps(body).encode(),headers)
                with urllib.request.urlopen(request,timeout=8) as response:result=json.loads(response.read(200000))
                if result.get("code")!=0:raise ValueError("NapCat authenticated status check failed")
                return result.get("data")
            auth=api("auth/login",dict(hash=hashlib.sha256((config["token"]+".napcat").encode()).hexdigest()))
            status=api("QQLogin/CheckLoginStatus",{},auth["Credential"])
            phase=status.get("loginPhase")
            report["napcat_status"]=dict(authenticated=True,auto_login_account=config.get("autoLoginAccount",""),logged_in=status.get("isLogin") is True,core_ready=status.get("coreReady") is True,
                qr_available=bool(status.get("qrcodeurl")),login_error_present=bool(status.get("loginError")),
                phase=phase if phase in ("idle","qrcode","qrcode_scanned","initializing","ready","offline","reconnecting","waiting_qrcode","qrcode_expired") else "other")
            if action=="napcatconnect":
                if not status.get("isLogin") or not status.get("coreReady"):raise ValueError("QQ not ready; no configuration changed")
                data=home/".littlewhale"
                token=(data/"qq-token").read_text().strip()
                if not re.fullmatch(r"[A-Za-z0-9_-]{43}",token):raise ValueError("Invalid private QQ token")
                previous=api("OB11Config/GetConfig",{},auth["Credential"])
                network=previous.get("network",{})
                for key,items in network.items():
                    if not isinstance(items,list):raise ValueError("Unexpected OneBot network format")
                    if any(item.get("enable") and item.get("name") not in ("littlewhale-http","littlewhale-events") for item in items):
                        raise ValueError("Existing active OneBot adapter needs review; preserved")
                backup=base/("onebot-before-connect-"+str(time.time_ns())+".json")
                backup.write_text(json.dumps(previous));os.chmod(backup,0o600)
                network=dict(network)
                network["httpServers"]=[dict(name="littlewhale-http",enable=True,host="127.0.0.1",port=3000,
                    messagePostFormat="array",token=token,debug=False,enableCors=False,enableWebsocket=False,reportSelfMessage=False)]
                network["httpClients"]=[dict(name="littlewhale-events",enable=True,url="http://127.0.0.1:8767/bridge/qq/event",
                    messagePostFormat="array",token=token,debug=False,reportSelfMessage=False)]
                config=dict(previous,network=network)
                api("OB11Config/SetConfig",dict(config=json.dumps(config)),auth["Credential"])
                report["phase"]="onebot_pair";publish()
                link=(data/"panel-url").read_text().strip()
                if not re.fullmatch(r"http://127\.0\.0\.1:8767/#[A-Za-z0-9_-]{43}",link):raise ValueError("Invalid private panel link")
                def core(path,body):
                    request=urllib.request.Request("http://127.0.0.1:8767"+path,json.dumps(body).encode(),
                        {"Authorization":"Bearer "+link.split("#",1)[1],"Content-Type":"application/json"})
                    with urllib.request.urlopen(request,timeout=18) as response:return json.loads(response.read(20000))
                paired=core("/api/qq-pair",{})
                if not paired.get("configured"):raise ValueError("QQ pairing was not confirmed")
                owner=json.loads(marker.read_text("utf-8")).get("owner")
                if owner is not None:
                    if not isinstance(owner,str) or not re.fullmatch(r"[1-9][0-9]{4,19}",owner):raise ValueError("Invalid owner QQ identifier")
                    if not core("/api/qq-owner",dict(user_id=owner)).get("saved"):raise ValueError("Owner authorization not confirmed")
                    report["owner_authorized"]=True
                request=urllib.request.Request("http://127.0.0.1:3000/get_status",b"{}",
                    {"Authorization":"Bearer "+token,"Content-Type":"application/json"})
                with urllib.request.urlopen(request,timeout=12) as response:online=json.loads(response.read(20000))
                if online.get("status")!="ok" or online.get("retcode")!=0 or online.get("data",{}).get("online") is not True:
                    raise ValueError("OneBot online status not confirmed")
                persisted=api("OB11Config/GetConfig",{},auth["Credential"])
                # Upstream drops unsupported optional fields; verify both adapters' effective security fields.
                for key,fields in (("httpServers",("name","enable","host","port","messagePostFormat","token","debug")),
                                   ("httpClients",("name","enable","url","messagePostFormat","token","debug","reportSelfMessage"))):
                    actual=persisted.get("network",{}).get(key,[])
                    if len(actual)!=1 or any(actual[0].get(k)!=network[key][0][k] for k in fields):
                        raise ValueError("OneBot configuration readback differs: "+key)
                report["onebot"]=dict(configured=True,authenticated=True,online=True,server_loopback=True,
                    events_loopback=True,core_paired=True,owner_authorized=report.get("owner_authorized",False),backup_private=True)
            if action=="napcatqr":
                if status.get("isLogin"):raise ValueError("QQ already logged in; no new QR requested")
                refreshed=api("QQLogin/RefreshQRcode",{},auth["Credential"])
                value=refreshed.get("qrcodeurl") if isinstance(refreshed,dict) else None
                if not value:
                    value=api("QQLogin/GetQQLoginQrcode",{},auth["Credential"]).get("qrcode")
                if not isinstance(value,str) or not value:raise ValueError("Fresh QR image was not produced yet")
                payload=value.split(",",1)[1] if value.startswith("data:") and "," in value else value[9:] if value.startswith("base64://") else value
                try:data=base64.b64decode(payload,validate=True)
                except (ValueError,TypeError):raise ValueError("Invalid QR PNG") from None
                if not data.startswith(b"\x89PNG\r\n\x1a\n") or not 100<len(data)<500000:raise ValueError("Invalid QR PNG")
                # User explicitly requested this short-lived login image; remove transport copy after USB pull.
                target=shared/"qq-login-qr.png";target.write_bytes(data);os.chmod(target,0o600)
                report["qr_image"]=dict(file=target.name,bytes=len(data),sha256=hashlib.sha256(data).hexdigest(),fresh=True)
            if action=="napcatqrfresh":
                if status.get("isLogin"):
                    report["qq_login"]=dict(logged_in=True)
                else:
                    refreshed=api("QQLogin/RefreshQRcode",{},auth["Credential"])
                    value=refreshed.get("qrcodeurl") if isinstance(refreshed,dict) else None
                    if not isinstance(value,str) or not value.startswith("https://") or len(value)>2000:
                        raise ValueError("Fresh QR URL was not produced")
                    target=shared/"qq-login-url";target.write_text(value,encoding="utf-8");os.chmod(target,0o600)
                    report["qr_url"]=dict(file=target.name,bytes=len(value),fresh=True)
            if action=="napcatopen":
                url=endpoint+"/webui?"+urllib.parse.urlencode(dict(token=config["token"]))
                # Never copy the local panel secret into command output or shared reports.
                launch_env=dict(os.environ,LD_LIBRARY_PATH="/system/lib64:/vendor/lib64")
                for key in ("PYTHONHOME","PYTHONPATH","VIRTUAL_ENV"):launch_env.pop(key,None)
                result=subprocess.run(["/system/bin/am","start","--user","0","-a","android.intent.action.VIEW","-d",url],capture_output=True,env=launch_env,timeout=10)
                report["panel_open_requested"]=result.returncode==0
                report["panel_errors"]=[label for label in ("SecurityException","ClassNotFoundException","ActivityNotFoundException","CANNOT LINK EXECUTABLE") if label.encode() in result.stdout+result.stderr]
                if result.returncode:raise ValueError("VM browser did not accept the panel request")
        if action in ('speechsetup','speechprobe'):
            sys.path.insert(0,str(home/'LittleWhale'))
            if action=='speechsetup':report['speech']=setup_speech(home,shared,tts=payload.get('tts',False))
            else:
                import speech
                data=home/'.littlewhale'
                fixture=speech.roots(data)['asr']/'test_wavs/0.wav'
                verified_file(fixture,'668bf8df51a10027b84d5d8816a1ce11ae93545538dc05cfe2aa6811d399c250')
                recognized=speech.transcribe(data,fixture.read_bytes())
                if '感兴趣' not in recognized:raise ValueError('Local ASR fixture mismatch')
                report['speech']=dict(local_asr=True,fixture_only=True,recognized_characters=len(recognized),raw_audio_uploaded=False)
                if payload.get('tts') is True:
                    from whale import atomic
                    before=time.monotonic();digest=speech.synthesize(data,'主人，我这是储备能量。')
                    raw=speech.audio(data,digest)
                    atomic(shared/'speech-preview.wav',raw)
                    report['speech']['tts']=dict(**speech.wav_info(raw),sha256=digest,wall_seconds=round(time.monotonic()-before,3),
                        synthetic_only=True,local_only=True,qq_sent=False)
        if action=="roleupdate":
            import urllib.request
            data=home/".littlewhale"
            candidate=shared/"role-candidate.md"
            expected=payload.get("sha256","")
            if not re.fullmatch(r"[0-9a-f]{64}",expected):raise ValueError("Invalid role checksum")
            verified_file(candidate,expected)
            role=candidate.read_text("utf-8")
            if not role.strip() or len(role)>12000:raise ValueError("Invalid role length")
            link=(data/"panel-url").read_text("utf-8").strip()
            if not re.fullmatch(r"http://127\.0\.0\.1:8767/#[A-Za-z0-9_-]{43}",link):raise ValueError("Invalid private panel link")
            backup=data/("role-backup-"+str(time.time_ns())+".md")
            backup.write_bytes((data/"role.md").read_bytes());backup.chmod(0o600)
            request=urllib.request.Request("http://127.0.0.1:8767/api/role",json.dumps(dict(role=role)).encode(),headers={"Authorization":"Bearer "+link.split("#",1)[1],"Content-Type":"application/json"})
            with urllib.request.urlopen(request,timeout=8) as response:result=json.loads(response.read(20000))
            if result.get("saved") is not True or (data/"role.md").read_text("utf-8")!=role.strip():raise ValueError("Role save not confirmed")
            candidate.unlink()
            report["role_update"]=dict(saved=True,backup=True,characters=len(role),sha256=expected)
        if action=="webprobe" and payload.get("chat") is True:
            import urllib.request
            import sqlite3
            from contextlib import closing
            report["phase"]="web_chat";publish()
            data=home/".littlewhale"
            link=(data/"panel-url").read_text("utf-8").strip()
            if not re.fullmatch(r"http://127\.0\.0\.1:8767/#[A-Za-z0-9_-]{43}",link):raise ValueError("Invalid private panel link")
            event="web-probe-"+str(os.getpid())+"-"+str(time.time_ns())
            report["web_probe"]=dict(event=event,browser_used=False);publish()
            body=dict(event_id=event,message="这是一次联网功能验收，不要记入长期记忆或日记：不用浏览器，联网找一个开心猫咪表情包，收藏并发给我。请选择真正的表情图，不要网站标志或广告。")
            request=urllib.request.Request("http://127.0.0.1:8767/api/chat",json.dumps(body).encode(),headers={"Authorization":"Bearer "+link.split("#",1)[1],"Content-Type":"application/json"})
            # ponytail: exercise the actual core without inventing a QQ inbound event or sending a test to a contact.
            with urllib.request.urlopen(request,timeout=1800) as response:reply=json.loads(response.read(200000))
            with closing(sqlite3.connect((data/"memory.sqlite3").as_uri()+"?mode=ro",uri=True)) as db:
                steps=[dict(name=name,status=status) for name,status in db.execute("SELECT name,status FROM phone_actions WHERE event=? ORDER BY step",(event,))]
            report["web_probe"].update(photo=bool(reply.get("photo")),reply_lines=len(reply.get("lines",[])),steps=steps,browser_used=any(s["name"] not in ("web_search","web_read","save_image") for s in steps))
        elif action=="webprobe":
            sys.path.insert(0,str(home/"LittleWhale"))
            from whale import configuration,web_request
            from social import fetch_image
            config=configuration(json.loads((home/".littlewhale/config.json").read_text("utf-8")))
            report["phase"]="web_search";publish()
            found=web_request(config,dict(name="web_search",query="猫咪 开心 表情包 gif"))
            import urllib.parse
            report["web_probe"]=dict(results=len(found["results"]),browser_used=False,read_requests=0,read_errors=[],sources=[dict(title=r["title"],scheme=urllib.parse.urlsplit(r["url"]).scheme,host=urllib.parse.urlsplit(r["url"]).hostname) for r in found["results"]])
            candidates=[url for row in found["results"] for url in row["images"]]
            for row in found["results"][:3]:
                if candidates:break
                if not row["url"].startswith(("https://","http://")):continue
                report["phase"]="web_read";publish()
                try:
                    page=web_request(config,dict(name="web_read",url=row["url"]))
                    report["web_probe"]["read_requests"]+=1
                    candidates.extend(page["images"])
                except ValueError as error:
                    report["web_probe"]["read_errors"].append(str(error)[:100]);continue
            report["web_probe"]["image_candidates"]=len(candidates)
            report["phase"]="web_image";publish()
            for url in candidates[:3]:
                try:
                    mime,raw=fetch_image(url)
                    report["web_probe"]["image_download"]=dict(ok=True,mime=mime,bytes=len(raw))
                    break
                except Exception as error:report["web_probe"]["image_download"]=dict(ok=False,error=type(error).__name__,hint=str(error)[:80] if type(error) is ValueError and str(error).startswith("图片") else "")
        if action in ("qqinspect","modelprobe"):
            import sqlite3
            import urllib.request
            from contextlib import closing
            data=home/".littlewhale"
            link=(data/"panel-url").read_text().strip()
            if not re.fullmatch(r"http://127\.0\.0\.1:8767/#[A-Za-z0-9_-]{43}",link):raise ValueError("Invalid private panel link")
            config=json.loads((data/"config.json").read_text("utf-8"))
            report["model_configuration"]={key:config.get(key) for key in ("model","reasoning_effort")}
            role=(data/"role.md").read_text("utf-8")
            report["role_status"]=dict(characters=len(role),sha256=hashlib.sha256(role.encode()).hexdigest(),whale_girl=role.startswith("你是一只蓝白配色"),nickname="这是他给你起的昵称" in role)
            if action=="modelprobe":
                options=json.loads(marker.read_text("utf-8"))
                vision=options.get("vision") is True
                path="/api/qq-diagnose" if options.get("context")=="qq" and not vision else "/api/test-model"
                body={}
                if vision:
                    fixture=shared/"vision-test.png"
                    verified_file(fixture,"99330b95ffc17e2fd42adf6c5f486e39801019bfab2065f2b68c5248757df18e")
                    body["image"]=base64.b64encode(fixture.read_bytes()).decode()
                request=urllib.request.Request("http://127.0.0.1:8767"+path,json.dumps(body).encode(),headers={"Authorization":"Bearer "+link.split("#",1)[1],"Content-Type":"application/json"})
                try:
                    with urllib.request.urlopen(request,timeout=190) as response:probe=json.loads(response.read(20000))
                    report["model_probe"]=dict(ok=probe.get("ok") is True)
                    if vision:report["model_probe"].update(vision=True,reply=probe.get("reply"),fixture_only=True)
                except urllib.error.HTTPError as error:
                    result=json.loads(error.read(20000))
                    report["model_probe"]=dict(ok=False,http=error.code,error=result.get("error","Model probe failed"))
            request=urllib.request.Request("http://127.0.0.1:8767/api/social",headers={"Authorization":"Bearer "+link.split("#",1)[1]})
            with urllib.request.urlopen(request,timeout=8) as response:social=json.loads(response.read(200000))
            report["qq_channel"]=social.get("qq")
            report["helper"]=social.get("helper")
            report["worker_error"]=False
            with closing(sqlite3.connect((data/"memory.sqlite3").as_uri()+"?mode=ro",uri=True)) as db:
                report["qq_threads"]=dict(db.execute("SELECT enabled,count(*) FROM social_threads WHERE package='onebot.qq' GROUP BY enabled"))
                report["qq_inbox"]=dict(db.execute("SELECT i.status,count(*) FROM social_inbox i JOIN social_qq q ON q.thread=i.thread GROUP BY i.status"))
                report["qq_outbox"]=dict(db.execute("SELECT o.status,count(*) FROM social_outbox o JOIN social_qq q ON q.thread=o.thread GROUP BY o.status"))
                report["qq_latest_at"]=db.execute("SELECT max(i.at) FROM social_inbox i JOIN social_qq q ON q.thread=i.thread").fetchone()[0]
                row=db.execute("SELECT value FROM settings WHERE key='social_worker_error'").fetchone()
                report["worker_error"]=json.loads(row[0]) if row else False
                report["model_events"]=dict(db.execute("SELECT status,count(*) FROM events WHERE thread LIKE 'qq:%' GROUP BY status"))
                report["recent_requests"]=[]
                for at,usage in db.execute("SELECT at,usage FROM calls ORDER BY id DESC LIMIT 8"):
                    usage=json.loads(usage) if usage else {}
                    report["recent_requests"].append(dict(at=at,**{key:usage[key] for key in ("request_seconds","reasoning_effort","prompt_tokens","completion_tokens") if key in usage}))
                report["working_events"]=[dict(age_seconds=max(0,round(time.time()-at)),steps=steps) for at,steps in db.execute("SELECT e.at,(SELECT count(*) FROM phone_actions a WHERE a.event=e.id) FROM events e WHERE e.status='working'")]
                report["recent_actions"]=[dict(name=name,status=status,at=at) for name,status,at in db.execute("SELECT name,status,at FROM phone_actions ORDER BY at DESC LIMIT 16")]
                row=db.execute("SELECT value FROM settings WHERE key='last_model_error'").fetchone()
                report["last_model_error"]=json.loads(row[0]) if row else None
                for key in ('hearing_status','voice_status'):
                    row=db.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
                    report[key]=json.loads(row[0]) if row else None
                # Media diagnostics contain types/counts only, never image URLs, captions or private messages.
                import urllib.parse
                report["qq_media_samples"]=[]
                for (raw,) in db.execute("SELECT p.parts FROM social_parts p JOIN social_inbox i ON i.id=p.event ORDER BY i.at DESC,i.rowid DESC LIMIT 12"):
                    sample=[]
                    for part in json.loads(raw):
                        item=dict(type=part.get("type"))
                        if part.get("type")=="image":
                            parsed=urllib.parse.urlsplit(part.get("url") or "")
                            item.update(has_url=bool(part.get("url")),scheme=parsed.scheme,host=parsed.hostname,sticker=part.get("sticker"),caption_present=bool(part.get("summary")))
                        if part.get("type")=="face":item["id"]=part.get("id")
                        sample.append(item)
                    report["qq_media_samples"].append(sample)
                if json.loads(marker.read_text("utf-8")).get("image_probe") is True:
                    sys.path.insert(0,str(home/"LittleWhale"))
                    from social import fetch_image
                    report["image_download_probe"]=dict(found=False)
                    for (raw,) in db.execute("SELECT p.parts FROM social_parts p JOIN social_inbox i ON i.id=p.event ORDER BY i.at DESC,i.rowid DESC LIMIT 30"):
                        image=next((p for p in json.loads(raw) if p.get("type")=="image"),None)
                        if image:
                            try:
                                mime,blob=fetch_image(image.get("url"))
                                report["image_download_probe"]=dict(found=True,ok=True,mime=mime,bytes=len(blob))
                            except Exception as error:
                                report["image_download_probe"]=dict(found=True,ok=False,error=type(error).__name__,hint=str(error)[:80] if type(error) is ValueError and str(error).startswith("图片") else "")
                            break
                report["album_count"]=db.execute("SELECT count(*) FROM album").fetchone()[0]
                report["sticker_count"]=db.execute("SELECT count(*) FROM album WHERE reason LIKE '[表情包]%'").fetchone()[0]
            token=(data/"qq-token").read_text().strip()
            request=urllib.request.Request("http://127.0.0.1:3000/get_status",b"{}",{"Content-Type":"application/json","Authorization":"Bearer "+token})
            with urllib.request.urlopen(request,timeout=12) as response:status=json.loads(response.read(20000))
            report["onebot_online"]=status.get("status")=="ok" and status.get("data",{}).get("online") is True
            base=home/".littlewhale-napcat"
            logs=sorted(base.glob("native-qq-*.log"),key=lambda path:path.stat().st_mtime)
            if logs:
                with logs[-1].open("rb") as stream:
                    stream.seek(max(0,logs[-1].stat().st_size-250000));tail=stream.read().decode("utf-8",errors="replace")
                report["napcat_log_flags"]={name:bool(re.search(pattern,tail,re.I)) for name,pattern in dict(
                    http_client="HTTP.Client|HTTP 客户端",signature_error="signature|签名",http_400="400|Bad Request",http_401="401|Unauthorized",
                    http_error="HTTP.*(?:Error|失败)|HttpGetText|ECONNREFUSED|ECONNRESET",message_received="接收|收到|recv|receive",recent_error="\\[error\\]").items()}
        if action == "unlimited":
            import urllib.request
            report["phase"]="unlimited";publish()
            data=home/".littlewhale"
            link=(data/"panel-url").read_text("utf-8").strip()
            if not re.fullmatch(r"http://127\.0\.0\.1:8767/#[A-Za-z0-9_-]{43}",link):raise ValueError("Invalid private panel link")
            before=json.loads((data/"config.json").read_text("utf-8"))
            request=urllib.request.Request("http://127.0.0.1:8767/api/config",json.dumps(dict(daily_calls=0)).encode(),
                {"Authorization":"Bearer "+link.split("#",1)[1],"Content-Type":"application/json"})
            with urllib.request.urlopen(request,timeout=8) as response:current=json.loads(response.read(20000))
            after=json.loads((data/"config.json").read_text("utf-8"))
            if current["daily_calls"]!=0 or after["daily_calls"]!=0:raise ValueError("Unlimited setting not persisted")
            if {k:v for k,v in before.items() if k!="daily_calls"}!={k:v for k,v in after.items() if k!="daily_calls"}:
                raise ValueError("Unrelated model settings changed")
            report["unlimited"]=dict(daily_calls=0,persisted=True,other_configuration_preserved=True,key_configured=current["key_configured"])
        report.update(ok=True, running=False, phase="done", finished=time.time())
        if action in ACTIONS:
            marker.unlink(missing_ok=True)
    except Exception as error:
        report.update(running=False, error=type(error).__name__, failed_phase=report["phase"])
        if type(error) is ValueError:
            report["error_hint"] = str(error)[:200]
    publish()


if __name__ == "__main__":
    main()
