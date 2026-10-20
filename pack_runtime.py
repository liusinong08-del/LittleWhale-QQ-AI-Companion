"""Build a relocatable private runtime from pinned bootstrap and APT-verified packages."""
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import posixpath
import tarfile
import urllib.parse
import zipfile

ROOT = Path(__file__).resolve().parent
BOOTSTRAP = ROOT.parent / "PocketPilot/app/build/embedded/assets/termux-arm64-v8a.zip"
BOOTSTRAP_SHA256 = "7b170ff5ef0befe501d01febc61382f3c6e34456521853cff9ba0b277aa52fe9"
PREFIX = "data/data/com.termux/files/usr/"


def name(value):
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value or not path.parts:
        raise ValueError("Unsafe archive path")
    return path.as_posix()


def relative_link(location, target):
    if target.startswith("/" + PREFIX):
        return name(target[len(PREFIX) + 1:])
    if target.startswith("/"):
        raise ValueError("Runtime link outside private directory")
    return name(posixpath.normpath(posixpath.join(posixpath.dirname(location), target)))


def payload(data):
    if not data.startswith(b"!<arch>\n"):
        raise ValueError("Invalid Debian archive")
    cursor = 8
    while cursor < len(data):
        header = data[cursor:cursor + 60]
        if len(header) != 60 or header[-2:] != b"`\n":
            raise ValueError("Invalid Debian member")
        size = int(header[48:58])
        member = header[:16].decode().strip().rstrip("/")
        content = data[cursor + 60:cursor + 60 + size]
        if len(content) != size:
            raise ValueError("Truncated Debian member")
        if member.startswith("data.tar"):
            return content
        cursor += 60 + size + size % 2
    raise ValueError("Debian payload missing")


def main():
    output = ROOT / "downloads/runtime"
    output.mkdir(parents=True, exist_ok=True)
    raw = BOOTSTRAP.read_bytes()
    if hashlib.sha256(raw).hexdigest() != BOOTSTRAP_SHA256:
        raise ValueError("Bootstrap checksum mismatch")
    files, links, provenance = {}, {}, {}
    with zipfile.ZipFile(io.BytesIO(raw)) as source:
        for entry in source.infolist():
            key = name(entry.filename)
            if not entry.is_dir() and key.startswith(("lib/", "etc/tls/", "share/LICENSES/")):
                files[key] = source.read(entry)
        for line in source.read("SYMLINKS.txt").decode().splitlines():
            target, location = line.split("←", 1)
            if location.startswith("etc/tls/") or location.startswith("lib/") and ".so" in PurePosixPath(location).name:
                links[name(location)] = relative_link(location, target)

    # APT on the VM has already verified signed repository metadata. Verify the
    # exported public archives again against APT's signature-verified index.
    # Exporting the same index also avoids mixing packages from different updates.
    metadata = (ROOT / "downloads/LittleWhale-packages/Packages").read_text("utf-8")
    expected = {}
    for paragraph in metadata.split("\n\n"):
        fields = dict(line.split(": ", 1) for line in paragraph.splitlines() if ": " in line and not line.startswith(" "))
        if "Filename" in fields and "SHA256" in fields:
            expected[urllib.parse.unquote(fields["Filename"].rsplit("/", 1)[-1])] = fields["SHA256"]
    for archive in sorted((ROOT / "downloads/LittleWhale-packages").glob("*.deb")):
        data = archive.read_bytes()
        checksum = hashlib.sha256(data).hexdigest()
        if expected.get(urllib.parse.unquote(archive.name)) != checksum:
            raise ValueError("Package checksum mismatch: " + archive.name)
        provenance[archive.name] = checksum
        with tarfile.open(fileobj=io.BytesIO(payload(data)), mode="r:*") as source:
            for entry in source:
                if entry.isdir() and entry.name in (".", "./"):
                    continue
                key = name(entry.name)
                if not key.startswith(PREFIX):
                    continue
                key = name(key[len(PREFIX):])
                if entry.isfile():
                    if entry.size > 40_000_000:
                        raise ValueError("Oversized runtime member")
                    files[key] = source.extractfile(entry).read()
                    links.pop(key, None)
                elif entry.issym():
                    links[key] = relative_link(key, entry.linkname)
                    files.pop(key, None)
                elif not entry.isdir():
                    raise ValueError("Unsupported runtime member")
    if not provenance:
        raise ValueError("No verified Python packages found")

    def resolve(key, seen=None):
        seen = set() if seen is None else seen
        if key in seen or len(seen) > 20:
            raise ValueError("Cyclic runtime link")
        if key in files:
            return files[key]
        if key in links:
            return resolve(links[key], seen | {key})
        raise ValueError("Unresolved runtime link: " + key)

    for key, target in list(links.items()):
        children = {key + "/" + path[len(target) + 1:]: data for path, data in files.items() if path.startswith(target + "/")}
        if children:
            files.update(children)
            del links[key]
    for key in links:
        files[key] = resolve(key)
    if "bin/python3.14" not in files or "lib/python3.14/os.py" not in files:
        raise ValueError("Python interpreter or standard library missing")
    if sum(map(len, files.values())) > 350_000_000 or len(files) > 18000:
        raise ValueError("Runtime size limit exceeded")
    with zipfile.ZipFile(output / "runtime.zip", "w", zipfile.ZIP_DEFLATED) as target:
        for key, data in sorted(files.items()):
            info = zipfile.ZipInfo(key, (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            target.writestr(info, data)
    (output / "provenance.json").write_text(json.dumps(dict(bootstrap_sha256=BOOTSTRAP_SHA256,
        packages=provenance, archive_sha256=hashlib.sha256((output / "runtime.zip").read_bytes()).hexdigest()), indent=2))
    # Materialized links make the runtime portable to both APK extraction and ADB.
    for key, data in files.items():
        path = output / "tree" / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    print("Verified runtime:", len(files), "files;", (output / "runtime.zip").stat().st_size, "compressed bytes")


if __name__ == "__main__":
    import sys
    if "--self-check" in sys.argv:
        for bad in ("../escape", "/absolute", "folder/../../escape", "windows\\escape"):
            try:
                name(bad)
            except ValueError:
                continue
            raise AssertionError("Unsafe path accepted")
        assert relative_link("lib/liba.so", "liba.so.1") == "lib/liba.so.1"
        assert relative_link("lib/link", "/" + PREFIX + "lib/real") == "lib/real"
        print("Runtime path checks passed")
    else:
        main()
