"""Prepare pinned local speech components for the existing VM; never installs on a device."""
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import tarfile
import zipfile

from pack_napcat_runtime import fetch
from pack_runtime import ROOT, payload


def main():
    downloads=ROOT/'downloads'
    inputs=[dict(file='sherpa-onnx-v1.13.8-android-aarch64-termux-shared.tar.bz2',
                 url='https://github.com/k2-fsa/sherpa-onnx/releases/download/v1.13.8/sherpa-onnx-v1.13.8-android-aarch64-termux-shared.tar.bz2',
                 sha256='ad7bac42adc6295293fe6d36475936afb227cdc757cf293c02a706f6aca4e0a2'),
            dict(file='sherpa-onnx-paraformer-zh-small-2024-03-09.tar.bz2',
                 url='https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-paraformer-zh-small-2024-03-09.tar.bz2',
                 sha256='da92b3db5218c5be53aad53e57d1b6e63e7fc98a0e054fbdd6dbe18e9c6b1450'),
            dict(file='sherpa-onnx-zipvoice-distill-int8-zh-en-emilia.tar.bz2',
                 url='https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/sherpa-onnx-zipvoice-distill-int8-zh-en-emilia.tar.bz2',
                 sha256='77219c8b40f4ee8d73a7f902305ff6c1128ef9b54461c41b4ca6ed890b6c2803')]
    selected={'native':{'bin/sherpa-onnx-offline','bin/sherpa-onnx-offline-tts','bin/sherpa-onnx-version','lib/libonnxruntime.so','lib/libsherpa-onnx-c-api.so','lib/libsherpa-onnx-cxx-api.so'},
              'asr':{'model.int8.onnx','tokens.txt','test_wavs/0.wav','README.md'},
              'tts':{'encoder.int8.onnx','decoder.int8.onnx','lexicon.txt','tokens.txt'}}
    files={'native':{},'asr':{},'tts':{}}
    for kind,item in zip(('native','asr','tts'),inputs):
        with tarfile.open(fileobj=io.BytesIO(fetch(item,downloads))) as archive:
            for member in archive:
                name=PurePosixPath(*PurePosixPath(member.name).parts[1:]).as_posix()
                if member.isfile() and (name in selected[kind] or kind=='tts' and name.startswith('espeak-ng-data/')):
                    if '..' in PurePosixPath(name).parts or PurePosixPath(name).is_absolute():raise ValueError('Unsafe speech archive path')
                    files[kind][name]=(archive.extractfile(member).read(),member.mode)
        assert selected[kind]<=set(files[kind])
    vocoder=dict(file='vocos_24khz.onnx',url='https://github.com/k2-fsa/sherpa-onnx/releases/download/vocoder-models/vocos_24khz.onnx',
                 sha256='bcb3b970e384161c4d634f0bb9e999ff1c471b34c9bc0b1049a5014065ed3cc0')
    files['tts'][vocoder['file']]=(fetch(vocoder,downloads),0o600)
    reference=(downloads/'whale-voice-I-qwen-fp32.wav').read_bytes()
    assert hashlib.sha256(reference).hexdigest()=='338bdf37726efe7a096e41b9dc6f8ec00aa1aa4230c4434cce0497767edb3562'
    files['tts']['reference.wav']=(reference,0o600)
    files['tts']['reference.txt']=('又叫我大肥鱼？我这是储备能量，好不好？'.encode(),0o600)
    files['tts']['REFERENCE-SOURCE.txt']=(b'Synthetic Qwen3-TTS-12Hz-0.6B-CustomVoice Vivian example, not a private recording.\n'
        b'https://huggingface.co/Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice/tree/85e237c12c027371202489a0ec509ded67b5e4b5\n',0o600)
    # Reuse the APT-verified libc++ already required by the private Python runtime.
    package=downloads/'LittleWhale-packages/libc++_30_aarch64.deb'
    expected=json.loads((ROOT/'evidence/runtime-provenance.json').read_text('utf-8'))['packages'][package.name]
    raw=package.read_bytes();assert hashlib.sha256(raw).hexdigest()==expected
    with tarfile.open(fileobj=io.BytesIO(payload(raw))) as archive:
        files['native']['lib/libc++_shared.so']=(archive.extractfile('./data/data/com.termux/files/usr/lib/libc++_shared.so').read(),0o755)
    outputs={}
    for kind,contents in files.items():
        note=('For personal local testing. No recordings are sent to a recognition service.\n'
              'sherpa-onnx 1.13.8: https://github.com/k2-fsa/sherpa-onnx/tree/v1.13.8\n'
              'ONNX Runtime: https://github.com/microsoft/onnxruntime\n'
              'libc++: https://github.com/termux/termux-packages/tree/master/packages/libc%2B%2B\n'
              'ASR source: https://www.modelscope.cn/models/crazyant/speech_paraformer_asr_nat-zh-cn-16k-common-vocab8358-onnx/summary\n')
        contents['SOURCES.txt']=(note.encode(),0o600)
        target=downloads/('speech-'+kind+'.zip')
        with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as archive:
            for name,(data,mode) in sorted(contents.items()):
                info=zipfile.ZipInfo(name,(1980,1,1,0,0,0));info.create_system=0
                info.external_attr=mode<<16;info.compress_type=zipfile.ZIP_DEFLATED;archive.writestr(info,data)
        assert sum(len(data) for data,_ in contents.values())<250_000_000
        outputs[kind]=dict(file=target.name,sha256=hashlib.sha256(target.read_bytes()).hexdigest(),bytes=target.stat().st_size,
                           unpacked_bytes=sum(len(data) for data,_ in contents.values()),files={n:hashlib.sha256(data).hexdigest() for n,(data,_) in contents.items()})
        print(json.dumps(dict(kind=kind,**{k:v for k,v in outputs[kind].items() if k!='files'})))
    (ROOT/'evidence/speech-runtime-provenance.json').write_text(json.dumps(dict(inputs=inputs+[vocoder],libcxx_package_sha256=expected,
        asr_upstream_digest_published=False,outputs=outputs,installed=False),indent=2)+'\n','utf-8')


if __name__=='__main__':main()
