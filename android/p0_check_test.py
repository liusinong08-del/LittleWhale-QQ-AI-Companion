"""Offline check of Debian package rewriting; never runs dpkg or touches a device."""
import hashlib
import io
from pathlib import Path
import tarfile
import tempfile

from p0_check import patch_deb, unpack_native_zip


def member(name,body):
    header=(name+"/").ljust(16)+"0".ljust(12)+"0".ljust(6)+"0".ljust(6)+"100644".ljust(8)+str(len(body)).ljust(10)+"`\n"
    return header.encode()+body+(b"\n" if len(body)%2 else b"")


with tempfile.TemporaryDirectory() as folder:
    folder=Path(folder)
    for compression in ("xz","gz"):
        payload=io.BytesIO()
        with tarfile.open(fileobj=payload,mode="w:"+compression) as archive:
            for name in ("./","./data","./data/data","./data/data/com.termux","./data/data/com.termux/files","./data/data/com.termux/files/usr"):
                item=tarfile.TarInfo(name);item.type=tarfile.DIRTYPE;archive.addfile(item)
            prefix="./data/data/com.termux/files/usr/"
            for kind,name,link in ((tarfile.SYMTYPE,"lib/libdemo.so","libdemo.so.1"),(tarfile.LNKTYPE,"bin/alias",prefix+"bin/demo")):
                item=tarfile.TarInfo(prefix+name);item.type=kind;item.linkname=link;archive.addfile(item)
            item=tarfile.TarInfo(prefix+"bin/demo");item.size=5;item.mode=0o755;archive.addfile(item,io.BytesIO(b"hello"))
        source=folder/(compression+".deb");target=folder/(compression+"-fixed.deb")
        control=member("control.tar.gz",b"unchanged-control")
        source.write_bytes(b"!<arch>\n"+member("debian-binary",b"2.0\n")+control+member("data.tar."+compression,payload.getvalue()))
        assert patch_deb(source,target,hashlib.sha256(source.read_bytes()).hexdigest())==6
        raw=target.read_bytes();assert control in raw
        offset=raw.index(("data.tar."+compression+"/").encode())
        size=int(raw[offset+48:offset+58])
        with tarfile.open(fileobj=io.BytesIO(raw[offset+60:offset+60+size]),mode="r:"+compression) as archive:
            items=archive.getmembers()
            assert len(items)==3 and items[0].issym() and items[0].linkname=="libdemo.so.1" and items[1].islnk()
            assert archive.extractfile(items[2]).read()==b"hello" and items[2].mode==0o755
        try:patch_deb(source,target,"0"*64)
        except ValueError:pass
        else:raise AssertionError("Unverified package accepted")
print("PASS: xz/gzip format, ancestor removal, symlink/hardlink preservation, unchanged file/control data and source hash rejection")

with tempfile.TemporaryDirectory() as folder:
    import zipfile
    folder=Path(folder)
    for filename in ("bin/demo","../escape","/absolute","windows\\escape"):
        archive=folder/"runtime.zip"
        with zipfile.ZipFile(archive,"w") as output:
            item=zipfile.ZipInfo();item.filename=filename
            output.writestr(item,b"content")
        checksum=hashlib.sha256(archive.read_bytes()).hexdigest()
        target=folder/"runtime"
        if filename=="bin/demo":
            unpack_native_zip(archive,target,checksum)
            unpack_native_zip(archive,target,checksum)
            assert (target/filename).read_bytes()==b"content"
        else:
            try:unpack_native_zip(archive,folder/"invalid",checksum)
            except ValueError:pass
            else:raise AssertionError("Unsafe native archive accepted")
    try:unpack_native_zip(archive,folder/"bad-hash","0"*64)
    except ValueError:pass
    else:raise AssertionError("Unverified native archive accepted")
print("PASS: native runtime hash, idempotent install, path traversal rejection")

with tempfile.TemporaryDirectory() as folder:
    import json
    import sys
    from unittest.mock import patch
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    from p0_check import setup_speech
    folder=Path(folder);home=folder/'home';home.mkdir();shared=folder/'shared';shared.mkdir()
    (home/'.littlewhale').mkdir();config=home/'.littlewhale/speech.json'
    config.write_text(json.dumps(dict(keep='existing voice preferences')),'utf-8')
    hashes={}
    for kind in ('native','asr','tts'):
        archive=shared/('speech-'+kind+'.zip')
        with zipfile.ZipFile(archive,'w') as z:z.writestr('fixture.txt',kind)
        hashes[kind]=hashlib.sha256(archive.read_bytes()).hexdigest()
    with patch('p0_check.SPEECH_RUNTIMES',hashes):
        assert setup_speech(home,shared)['components']==['native','asr']
        assert setup_speech(home,shared)['tts_setting_changed'] is False
        assert 'tts' not in json.loads(config.read_text('utf-8'))
        assert not (home/('.littlewhale-speech-tts-'+hashes['tts'][:12])).exists()
        assert setup_speech(home,shared,tts=True)['components']==list(hashes)
        before=config.read_bytes()
        for value in ('true',1,None):
            try:setup_speech(home,shared,tts=value)
            except ValueError:pass
            else:raise AssertionError('Invalid component selection accepted')
        assert config.read_bytes()==before
        assert setup_speech(home,shared)['components']==['native','asr']
    result=json.loads(config.read_text('utf-8'));assert result['keep']=='existing voice preferences'
    assert all(Path(result[kind]).parent==home for kind in hashes)
print('PASS: fixed speech setup defaults to ASR only, explicitly selects TTS, verifies archives, remains idempotent, preserves private preferences and never enables TTS')
