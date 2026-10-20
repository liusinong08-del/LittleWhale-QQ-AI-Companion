"""Local CPU ASR and TTS through pinned sherpa CLIs; no audio or text uploads."""
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import wave

LIMIT=6_000_000


def wav_info(raw):
    if not isinstance(raw,bytes) or len(raw)>LIMIT:raise ValueError("语音文件过大")
    try:
        with wave.open(io.BytesIO(raw)) as source:
            duration=source.getnframes()/source.getframerate()
            if source.getnchannels()!=1 or source.getsampwidth()!=2 or source.getcomptype()!='NONE' or not 8000<=source.getframerate()<=48000 or not 0<duration<=60:
                raise ValueError("语音须为60秒内的单声道PCM录音")
            if len(source.readframes(source.getnframes()))!=source.getnframes()*2:raise ValueError("语音文件不完整")
            return dict(seconds=round(duration,3),sample_rate=source.getframerate())
    except (wave.Error,EOFError,ZeroDivisionError):raise ValueError("语音不是可识别的WAV文件") from None


def roots(home,keys=('native','asr')):
    try:config=json.loads((home/'speech.json').read_text('utf-8'))
    except (OSError,ValueError):raise ValueError("语音运行组件尚未安装") from None
    found={}
    for key in keys:
        value=config.get(key) if isinstance(config,dict) else None
        if not isinstance(value,str):raise ValueError("本地语音配置无效")
        folder=Path(value).resolve()
        if folder.parent!=home.parent.resolve() or not re.fullmatch(r'\.littlewhale-speech-'+key+r'-[0-9a-f]{12}',folder.name):raise ValueError("本地语音目录不安全")
        found[key]=folder
    return found


def run(native,command,args):
    binary=native/'bin'/(command+('.exe' if os.name=='nt' else ''))
    env=dict(os.environ,LD_LIBRARY_PATH=str(native/'lib'),OMP_NUM_THREADS='2')
    env.pop('LD_PRELOAD',None)
    try:result=subprocess.run([str(binary),'--num-threads=2',*map(str,args)],env=env,capture_output=True,timeout=60)
    except (OSError,subprocess.TimeoutExpired):raise ValueError("本地语音处理失败或超时，未上传音频") from None
    # Native output may contain the utterance; never include it in errors or logs.
    if result.returncode:raise ValueError("本地语音处理失败，未上传音频")
    return (result.stdout+b'\n'+result.stderr).decode('utf-8','replace')


def synthesize(home,text,*,voice='whale'):
    home=Path(home).resolve()
    if not isinstance(text,str) or not text.strip() or len(text)>480 or '\x00' in text:raise ValueError("语音文字为空或过长")
    if voice!='whale':raise ValueError('声线无效')
    config=roots(home,('native','tts'));model=config['tts']
    reference=(model/'reference.txt').read_text('utf-8').strip()
    if not reference or len(reference)>200:raise ValueError('参考语音文字无效')
    cache=home/'voice';cache.mkdir(mode=0o700,exist_ok=True)
    files=list(cache.glob('*.wav'))
    # ponytail: retain uncertain audio for reconciliation; the bounded cache stops growth until reviewed.
    if len(files)>=100 or sum(p.stat().st_size for p in files)>=100_000_000:raise ValueError("待核对语音缓存已满，请先处理未发送的语音")
    with tempfile.TemporaryDirectory(prefix='.voice-',dir=home) as folder:
        target=Path(folder)/'output.wav'
        run(config['native'],'sherpa-onnx-offline-tts',[
            '--zipvoice-encoder='+str(model/'encoder.int8.onnx'),'--zipvoice-decoder='+str(model/'decoder.int8.onnx'),
            '--zipvoice-data-dir='+str(model/'espeak-ng-data'),'--zipvoice-lexicon='+str(model/'lexicon.txt'),
            '--zipvoice-tokens='+str(model/'tokens.txt'),'--zipvoice-vocoder='+str(model/'vocos_24khz.onnx'),
            '--reference-audio='+str(model/'reference.wav'),'--reference-text='+reference,'--num-steps=4',
            '--speed=1.0','--output-filename='+str(target),
            # Native option parsing must not interpret model-controlled text as a switch.
            ' '+text.strip()])
        if not target.is_file() or target.stat().st_size>LIMIT:raise ValueError('本地语音输出无效或过大')
        raw=target.read_bytes();wav_info(raw);digest=hashlib.sha256(raw).hexdigest()
        target.chmod(0o600);target.replace(cache/(digest+'.wav'))
    return digest


def audio(home,digest):
    if not isinstance(digest,str) or not re.fullmatch('[0-9a-f]{64}',digest):raise ValueError("语音标识无效")
    path=Path(home)/'voice'/(digest+'.wav')
    if not path.is_file() or path.is_symlink() or path.stat().st_size>LIMIT:raise ValueError("本地语音文件不存在或无效")
    raw=path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=digest:raise ValueError("本地语音文件校验失败")
    wav_info(raw)
    return raw


def transcribe(home,raw):
    home=Path(home).resolve();wav_info(raw);config=roots(home);model=config['asr']
    with tempfile.TemporaryDirectory(prefix='.hearing-',dir=home) as folder:
        path=Path(folder)/'input.wav';path.write_bytes(raw);path.chmod(0o600)
        output=run(config['native'],'sherpa-onnx-offline',[
            '--paraformer='+str(model/'model.int8.onnx'),'--tokens='+str(model/'tokens.txt'),str(path)])
    for line in output.splitlines():
        try:row=json.loads(line)
        except ValueError:continue
        if isinstance(row,dict) and isinstance(row.get('text'),str) and row['text'].strip() and len(row['text'])<=4000:return row['text'].strip()
    raise ValueError("这条语音没有识别到清晰文字，原始音频未上传")
