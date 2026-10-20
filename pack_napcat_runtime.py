"""Reproduce the pinned native NapCat candidate archives; does not deploy or log in."""
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import posixpath
import tarfile
import urllib.request
import zipfile

from pack_runtime import ROOT, name, payload, relative_link


def fetch(item, directory):
    path=directory/item["file"]
    if not path.exists():
        with urllib.request.urlopen(item["url"],timeout=45) as response:
            data=response.read()
        if hashlib.sha256(data).hexdigest()!=item["sha256"]:raise ValueError("Download checksum mismatch")
        temporary=path.with_suffix(path.suffix+".partial")
        temporary.write_bytes(data);temporary.replace(path)
    raw=path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=item["sha256"]:raise ValueError("Cached checksum mismatch: "+path.name)
    return raw


def build(items,directory,kind):
    files,links,selected={},{},{}
    for package in items:
        with tarfile.open(fileobj=io.BytesIO(payload(fetch(package,directory))),mode="r:*") as archive:
            for item in archive:
                key=posixpath.normpath(item.name)
                if kind=="glibc":
                    prefix="data/data/com.termux/files/usr/glibc/"
                    if not key.startswith(prefix):continue
                    key=name(key[len(prefix):])
                    if not key.startswith(("bin/","lib/","etc/")):continue
                    if package["package"]=="gcc-libs-glibc" and not PurePosixPath(key).name.startswith(("libstdc++","libgcc_s","libatomic","libgomp")):continue
                    selected[key]=key
                elif kind=="bionic":
                    prefix="data/data/com.termux/files/usr/"
                    if not key.startswith(prefix):continue
                    key=name(key[len(prefix):])
                    if not (key.startswith("lib/") and ".so" in PurePosixPath(key).name or key=="bin/qemu-aarch64"):continue
                    selected[key]=key
                else:
                    if key.startswith(("usr/lib/aarch64-linux-gnu/","lib/aarch64-linux-gnu/")) and ".so" in PurePosixPath(key).name and "/" not in key.split("aarch64-linux-gnu/",1)[1]:selected[key]="lib/"+PurePosixPath(key).name
                    if key in ("usr/bin/Xvfb","usr/bin/xkbcomp"):selected[key]="bin/"+PurePosixPath(key).name
                    if key.startswith(("usr/share/X11/xkb/","usr/share/fonts/","usr/share/glib-2.0/schemas/")):selected[key]=key[len("usr/"):]
                if item.isfile():files[key]=(archive.extractfile(item).read(),item.mode)
                elif item.issym():
                    target=item.linkname
                    links[key]=(relative_link(key,target) if kind=="bionic" else name(posixpath.normpath(target.lstrip("/") if target.startswith("/") else posixpath.join(posixpath.dirname(key),target))))
                else:selected.pop(key,None)

    def resolve(key,seen=()):
        if key in seen:raise ValueError("Cyclic runtime link")
        if key in files:return files[key]
        if key in links:return resolve(links[key],seen+(key,))
        if key.startswith("usr/") and key[4:] in files:return files[key[4:]]
        if not key.startswith("usr/") and "usr/"+key in files:return files["usr/"+key]
        raise ValueError("Unresolved runtime link: "+key)

    return {target:resolve(key) for key,target in selected.items()}


def main():
    metadata=json.loads((ROOT/"evidence/napcat-native-provenance.json").read_text("utf-8"))
    output=ROOT/"downloads/napcat-pinned"
    output.mkdir(parents=True,exist_ok=True)
    (output/"debian").mkdir(exist_ok=True)
    (output/"qemu").mkdir(exist_ok=True)
    for group,kind,directory,filename in (("glibc","glibc",output,"glibc-native.zip"),("debian","debian",output/"debian","qq-dependencies.zip"),("qemu","bionic",output/"qemu","qemu-native.zip")):
        files=build(metadata[group],directory,kind)
        target=output/filename
        with zipfile.ZipFile(target,"w",zipfile.ZIP_DEFLATED) as archive:
            for key,(data,mode) in sorted(files.items()):
                info=zipfile.ZipInfo(key,(1980,1,1,0,0,0))
                info.create_system=0  # Match the original Windows build on every development platform.
                info.external_attr=mode<<16
                info.compress_type=zipfile.ZIP_DEFLATED
                archive.writestr(info,data)
        if hashlib.sha256(target.read_bytes()).hexdigest()!=metadata["outputs"][filename]:raise ValueError("Reproduced archive differs: "+filename)
        print(filename,": checksum verified;",len(files),"files")
    for item in metadata["upstream"]:fetch(item,output)
    node=metadata["node"]
    with tarfile.open(fileobj=io.BytesIO(fetch(node,output)),mode="r:xz") as archive:
        data=archive.extractfile("node-"+node["version"]+"-linux-arm64/bin/node").read()
    target=output/"node-native.zip"
    with zipfile.ZipFile(target,"w",zipfile.ZIP_DEFLATED) as archive:
        info=zipfile.ZipInfo("node",(1980,1,1,0,0,0));info.create_system=0
        info.external_attr=0o755<<16;info.compress_type=zipfile.ZIP_DEFLATED
        archive.writestr(info,data)
    if hashlib.sha256(target.read_bytes()).hexdigest()!=node["output_sha256"]:raise ValueError("Reproduced Node archive differs")
    fetch(metadata["node_loader"],output)
    print("Pinned QQ and NapCat archives verified. Device compatibility still requires real VM checks.")


if __name__=="__main__":main()
