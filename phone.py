"""ADB control of the specifically paired VM, with no host-phone fallback."""
import argparse
import base64
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import uuid
import xml.etree.ElementTree as ET

PACKAGE = r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+"
PRIVATE_APPS={"com.termux","cn.littlewhale.app","cn.littlewhale.bridge","com.android.settings","com.eg.android.AlipayGphone","com.unionpay"}
SENSITIVE = re.compile(r"验证码|支付密码|确认支付|确认付款|立即支付|银行卡|转账|购买|订阅|恢复出厂|清除数据|注销账号|API\s*密钥|verification.code|password|\bOTP\b",re.I)


class UncertainAction(ValueError):
    """A command may already have changed the VM; never automatically replay it."""


def action(value):
    if not isinstance(value,dict) or not isinstance(value.get("name"),str):
        raise ValueError("手机动作格式无效")
    fields={"observe":set(),"apps":set(),"open":{"package"},"tap":{"screen","node"},
            "type":{"screen","node","text"},"back":{"screen"},"home":set(),
            "swipe":{"screen","x1","y1","x2","y2","duration"},
            "download":{"url"},"install":{"sha256"},"read_notebook":{"kind","after"},
            "web_search":{"query"},"web_read":{"url"},"save_image":{"url","reason","sticker"},
            "qq_favorite":{"image"},"qq_post":{"content","photos"}}
    name=value["name"]
    if name not in fields or set(value)!={"name",*fields[name]}:
        raise ValueError("手机动作或参数不支持")
    if "screen" in value and (not isinstance(value["screen"],str) or not re.fullmatch(r"[0-9a-f]{64}",value["screen"])):
        raise ValueError("请先观察当前页面")
    if "node" in value and (type(value["node"]) is not int or not 0<=value["node"]<10000):
        raise ValueError("页面控件无效")
    if name=="open" and (not isinstance(value["package"],str) or not re.fullmatch(PACKAGE,value["package"])):
        raise ValueError("应用标识无效")
    if name=="type" and (not isinstance(value["text"],str) or not value["text"] or len(value["text"])>2000 or any(ord(c)<32 and c not in "\n\t" for c in value["text"])):
        raise ValueError("输入文字无效或过长")
    if name=="swipe" and (any(type(value[k]) is not int or value[k]<0 for k in ("x1","y1","x2","y2")) or type(value["duration"]) is not int or not 100<=value["duration"]<=1500):
        raise ValueError("滑动参数无效")
    if name=="download":
        download_url(value["url"])
    if name=="save_image":download_url(value["url"])
    if name=="web_read":
        url=value["url"]
        if not isinstance(url,str):raise ValueError("网页地址无效")
        download_url("https://"+url[7:] if url.startswith("http://") else url)
        host=urllib.parse.urlsplit(url).hostname
        if host=="localhost" or "." not in host or host.endswith((".local",".internal")):raise ValueError("网页地址须为公开域名")
        try:ipaddress.ip_address(host)
        except ValueError:pass
        else:raise ValueError("网页地址须为公开域名")
    if name=="web_search" and (not isinstance(value["query"],str) or not 1<=len(value["query"])<=128 or not value["query"].strip() or any(ord(c)<32 for c in value["query"])):
        raise ValueError("搜索词须为1至128字")
    if name=="save_image" and (not isinstance(value["reason"],str) or not 1<=len(value["reason"].strip())<=240 or type(value["sticker"]) is not bool):
        raise ValueError("请填写图片收藏理由及是否表情包")
    if name=="install" and (not isinstance(value["sha256"],str) or not re.fullmatch(r"[0-9a-f]{64}",value["sha256"])):
        raise ValueError("安装包标识无效")
    if name=="read_notebook" and (value["kind"] not in ("diary","memory") or not isinstance(value["after"],str) or not re.fullmatch(r"[A-Za-z0-9:_-]{0,120}",value["after"])):
        raise ValueError("记录应用或游标无效")
    if name=='qq_favorite' and (not isinstance(value['image'],str) or not re.fullmatch(r'(?:[0-9a-f]{64}|received)',value['image'])):
        raise ValueError('请选择相册图片或本轮第一张图片')
    if name=='qq_post':
        if not isinstance(value['content'],str) or not 1<=len(value['content'].strip())<=2000 or any(ord(c)<32 and c not in '\n\t' for c in value['content']):
            raise ValueError('空间正文须为1至2000字')
        photos=value['photos']
        if not isinstance(photos,list) or len(photos)>9 or any(not isinstance(p,str) or not re.fullmatch(r'(?:[0-9a-f]{64}|received)',p) for p in photos) or len(set(photos))!=len(photos):
            raise ValueError('空间配图须为至多9张不同的相册图片或本轮第一张图片')
    return dict(value)


def download_url(value):
    if not isinstance(value, str) or len(value) > 4000:
        raise ValueError("下载地址无效")
    url = urllib.parse.urlsplit(value)
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.fragment or any(ord(c) < 33 for c in value):
        raise ValueError("下载地址须为不含账号和片段的HTTPS链接")
    return value


class DownloadRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        download_url(new_url)
        return super().redirect_request(request, response, code, message, headers, new_url)


class Phone:
    def __init__(self, config, adb="adb", port=5037, permitted=lambda: True):
        self.config = config
        self.serial = config["serial"]
        if not re.fullmatch(r"127\.0\.0\.1:\d{4,5}", self.serial):
            raise ValueError("只允许连接本机虚拟机端口，拒绝USB主手机或其他地址")
        if not 1024 <= int(self.serial.rsplit(":", 1)[1]) <= 65535:
            raise ValueError("虚拟机端口无效")
        if not re.fullmatch(r"[0-9a-f]{16}", config["android_id"]):
            raise ValueError("请先核对虚拟机身份")
        if not isinstance(config["sdk"], int) or not 24 <= config["sdk"] <= 35:
            raise ValueError("虚拟机版本未核对")
        self.prefix = [str(adb), "-P", str(port)]
        self.screen_key=os.urandom(32)
        self.observed=None
        self.permitted = permitted
        scope = config.get("app_scope", "allowlist")
        if scope not in ("allowlist", "whole_vm"):
            raise ValueError("虚拟机应用控制范围无效")
        self.whole_vm = scope == "whole_vm"
        packages = config.get("allow_packages", [])
        if not isinstance(packages, list) or any(not isinstance(p,str) or not re.fullmatch(PACKAGE,p) for p in packages):
            raise ValueError("应用白名单无效")
        self.allow = set(packages)
        if not self.allow and not self.whole_vm:
            raise ValueError("应用白名单无效")

    def command(self, *args, binary=False, timeout=12):
        try:
            result = subprocess.run([*self.prefix, "-s", self.serial, *args], capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise UncertainAction("虚拟机命令超时，执行结果需要核对；本次不自动重试") from None
        if result.returncode:
            raise UncertainAction("虚拟机命令失败；请核对虚拟机与ADB连接，本次不重试")
        if len(result.stdout) > 8_000_000:
            raise ValueError("虚拟机输出超限")
        return result.stdout if binary else result.stdout.decode("utf-8", "replace").strip()

    def shell(self, *args):
        return self.command("shell", shlex.join(str(a) for a in args))

    def connect(self):
        result = subprocess.run([*self.prefix, "connect", self.serial], capture_output=True, timeout=12)
        if result.returncode:
            raise ValueError("无法连接虚拟机")
        return self.status()

    def verify(self):
        if not self.permitted():
            raise ValueError("手机操作已暂停")
        if self.shell("settings", "get", "secure", "android_id") != self.config["android_id"]:
            raise ValueError("连接的不是配对虚拟机，拒绝操作")
        if self.shell("getprop", "ro.build.version.sdk") != str(self.config["sdk"]):
            raise ValueError("虚拟机系统版本已改变，请重新核对")
        if self.shell("getprop", "ro.build.fingerprint") != self.config["fingerprint"]:
            raise ValueError("虚拟机系统身份已改变，请重新核对")

    def size(self):
        display=self.focus()[1] if self.config["sdk"]>=29 else 0
        output=self.shell("dumpsys","input")
        for line in output.splitlines():
            if re.search(r"\bdisplayId="+str(display)+r"\b",line):
                frame=re.search(r"logicalFrame=\[(\d+),\s*(\d+),\s*(\d+),\s*(\d+)\]",line)
                if frame:
                    left,top,right,bottom=map(int,frame.groups())
                    if right>left and bottom>top:return right-left,bottom-top
        # Old single-display Android also reports SurfaceOrientation when the viewport line is absent.
        rotation=re.search(r"SurfaceOrientation:\s*([0-3])\b",output) if self.config["sdk"]<29 else None
        if rotation:
            matches=re.findall(r"(?:Override|Physical) size: (\d+)x(\d+)",self.shell("wm","size"))
            if matches:
                width,height=map(int,matches[-1])
                return (height,width) if int(rotation[1])%2 else (width,height)
        raise ValueError("无法核对当前显示方向与屏幕尺寸，请重新观察")

    def focus(self):
        # MuMu puts each app on a different display; its window dump can be empty.
        output = self.shell("dumpsys", "activity", "activities") if self.config["sdk"]>=29 else self.shell("dumpsys", "window", "windows")
        match = re.search(r"mCurrentFocus=.*?\s([\w.]+)/[^\s}]+", output)
        if not match:
            raise ValueError("无法核对前台应用，拒绝猜测")
        display=re.search(r"\bdisplayId=(\d+)",output[match.end():]) if self.config["sdk"]>=29 else None
        if self.config["sdk"]>=29 and not display:
            raise ValueError("无法核对应用所在屏幕")
        return match[1],int(display[1]) if display else 0

    def foreground(self):
        return self.focus()[0]

    def input(self,*args,expected=None):
        if not self.permitted():
            raise ValueError("手机操作已暂停")
        package,display_id=self.focus()
        if expected is not None and package!=expected:
            raise ValueError("应用已经切换，本次未输入或点击")
        display=("-d",display_id) if self.config["sdk"]>=29 else ()
        return self.shell("input",*display,*args)

    def status(self):
        self.verify()
        return dict(serial=self.serial, sdk=self.config["sdk"], size=self.size(), foreground=self.foreground())

    def allowed(self):
        self.verify()
        package = self.foreground()
        if not self.whole_vm and package not in self.allow:
            raise ValueError("当前应用不在小鲸鱼的授权名单中")
        return package

    def point(self, x, y):
        width, height = self.size()
        if type(x) is not int or type(y) is not int or not 0 <= x < width or not 0 <= y < height:
            raise ValueError("坐标超出虚拟机屏幕")

    def open(self, package):
        self.verify()
        if not isinstance(package,str) or not re.fullmatch(PACKAGE,package):
            raise ValueError("应用标识无效")
        if not self.whole_vm and package not in self.allow:
            raise ValueError("目标应用不在授权名单中")
        if not self.permitted():
            raise ValueError("手机操作已暂停")
        self.shell("monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1")
        return {"submitted": True, "package": package}  # Submission is not proof that the application finished opening.

    def apps(self):
        self.verify()
        return sorted(line[8:] for line in self.shell("pm","list","packages").splitlines()
                      if line.startswith("package:") and re.fullmatch(PACKAGE,line[8:]))

    def download(self, url, directory):
        self.verify()
        if not self.whole_vm:
            raise ValueError("下载安装需要整台虚拟机范围授权")
        download_url(url)
        directory=Path(directory)
        directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        handle, name=tempfile.mkstemp(prefix="download-",suffix=".part",dir=directory)
        temporary=Path(name)
        size=0
        digest=hashlib.sha256()
        try:
            with os.fdopen(handle,"wb") as target:
                opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),DownloadRedirect())
                with opener.open(url,timeout=30) as source:
                    length=source.headers.get("Content-Length")
                    if length is not None and not 1<=int(length)<=512_000_000:
                        raise ValueError("安装包体积无效或超过512MB")
                    while chunk:=source.read(65536):
                        if not self.permitted():
                            raise ValueError("下载已暂停")
                        size+=len(chunk)
                        if size>512_000_000:
                            raise ValueError("安装包超过512MB")
                        target.write(chunk)
                        digest.update(chunk)
                    if length is not None and size!=int(length):
                        raise ValueError("安装包下载不完整")
                target.flush()
                os.fsync(target.fileno())
            self.apk(temporary)
            saved=directory/(digest.hexdigest()+".apk")
            os.replace(temporary,saved)
            return dict(path=str(saved),sha256=digest.hexdigest(),bytes=size)
        except (urllib.error.URLError,TimeoutError,OSError,zipfile.BadZipFile):
            raise ValueError("安装包下载失败或文件无效，未安装；请检查网络和来源") from None
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def apk(path):
        path=Path(path)
        if not path.is_file() or not 1<=path.stat().st_size<=512_000_000:
            raise ValueError("安装包不存在或体积超限")
        try:
            with zipfile.ZipFile(path) as archive:
                if not 1<=archive.getinfo("AndroidManifest.xml").file_size<=4_000_000:
                    raise ValueError("安装包清单无效")
        except (zipfile.BadZipFile,KeyError):
            raise ValueError("不是有效的APK安装包") from None
        return path

    def install(self, path):
        self.verify()
        if not self.whole_vm:
            raise ValueError("安装软件需要整台虚拟机范围授权")
        path=self.apk(path)
        if not self.permitted():
            raise ValueError("安装操作已暂停")
        result=self.command("install","-r",str(path),timeout=120)
        if "Success" not in result.splitlines():
            raise UncertainAction("未收到安装成功结果，请核对应用；本次不自动重试")
        return dict(installed=True,apk=path.name)

    def notebook(self, kind, record=None, after=""):
        self.verify()
        if kind not in ("diary", "memory"):
            raise ValueError("记录应用无效")
        package="cn.littlewhale."+kind
        if not self.whole_vm and package not in self.allow:
            raise ValueError("记录应用尚未授权")
        if record is not None:
            if not isinstance(record,dict) or set(record)!={"id","title","body"}:
                raise ValueError("记录格式无效")
            for key,limit in (("id",120),("title",120),("body",12000)):
                if not isinstance(record[key],str) or not record[key].strip() or len(record[key])>limit or "\x00" in record[key]:
                    raise ValueError("记录内容无效或过长")
            if not re.fullmatch(r"[A-Za-z0-9:_-]{1,120}",record["id"]):
                raise ValueError("记录标识无效")
            method,argument="put",json.dumps(record,ensure_ascii=False)
        else:
            if not isinstance(after,str) or not re.fullmatch(r"[A-Za-z0-9:_-]{0,120}",after):
                raise ValueError("记录游标无效")
            method,argument="export",after
        output=self.shell("content","call","--uri","content://"+package+".entries","--method",method,*( ["--arg",argument] if argument else []))
        match=re.search(r"\bdata64=([A-Za-z0-9+/]+={0,2})(?:[},\s]|$)",output)
        if not match:
            raise ValueError("记录应用未返回保存结果，请核对安装和虚拟机连接")
        try:
            return json.loads(base64.b64decode(match[1],validate=True))
        except (ValueError,UnicodeError):
            raise ValueError("记录应用返回格式无效") from None

    def screenshot(self):
        package = self.allowed()
        data = self.command("exec-out", "screencap", "-p", binary=True)
        if not data.startswith(b"\x89PNG\r\n\x1a\n") or self.foreground() != package:
            raise ValueError("截图失败或应用已切换")
        return data

    def tap(self, x, y, expected=None):
        self.allowed()
        self.point(x, y)
        if not self.permitted():
            raise ValueError("手机操作已暂停")
        self.input("tap", x, y,expected=expected)

    def swipe(self, x1, y1, x2, y2, duration=400,expected=None):
        self.allowed()
        self.point(x1, y1)
        self.point(x2, y2)
        if type(duration) is not int or not 100 <= duration <= 1500:
            raise ValueError("滑动时长须为100至1500毫秒")
        if not self.permitted():
            raise ValueError("手机操作已暂停")
        self.input("swipe", x1, y1, x2, y2, duration,expected=expected)

    def ui_xml(self):
        """Internal read-only dump; model access remains gated in observe/perform."""
        self.verify()
        path="/data/local/tmp/littlewhale-ui-"+uuid.uuid4().hex+".xml"
        try:
            # UIAutomator can exit zero with ERROR while the keyboard animates; only this read may retry.
            for attempt in range(2):
                output=self.command("shell",shlex.join(("uiautomator","dump",path)),timeout=20)
                if "ERROR" not in output:
                    break
                if attempt:
                    raise ValueError("当前页面未稳定，暂时无法读取；本次没有操作")
                time.sleep(.5)
            raw=self.shell("cat",path)
        finally:
            self.shell("rm","-f",path)
        try:
            return ET.fromstring(raw)
        except ET.ParseError:
            raise ValueError("当前页面不能读取文字控件，不能猜测点击") from None

    def observe(self,private=False):
        package=self.allowed()
        if package in PRIVATE_APPS:
            raise ValueError("当前为系统设置、账户或核心私有应用，请由本人操作")
        root=self.ui_xml()
        if self.foreground()!=package:
            raise ValueError("读取期间应用已切换，请重新观察")
        nodes=[];sensitive=False
        for number,node in enumerate(root.iter("node")):
            p=node.attrib
            if p.get("package")!=package:
                continue
            editable=p.get("class","").endswith("EditText")
            secret=p.get("password")=="true"
            label=" ".join(p.get(k,"") for k in ("text","content-desc","resource-id"))
            sensitive |= secret or (len(p.get("text",""))<80 and bool(SENSITIVE.search(label)))
            nodes.append(dict(node=number,text="" if secret or editable else p.get("text","")[:160],
                              description="" if secret else p.get("content-desc","")[:160],
                              resource_id=p.get("resource-id","")[:160],bounds=p.get("bounds",""),
                              clickable=p.get("clickable")=="true",enabled=p.get("enabled")=="true",
                              editable=editable,focused=p.get("focused")=="true",
                              **({"value":p.get("text","")} if private and editable and not secret else {})))
        # Per-controller salt detects concurrent text edits without exposing guessable hashes of credentials.
        fingerprint=hashlib.sha256(self.screen_key+json.dumps([package,[n.attrib for n in root.iter("node") if n.get("package")==package]],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        if not private:
            self.observed=fingerprint
        return dict(package=package,screen=fingerprint,size=self.size(),sensitive=bool(sensitive),nodes=nodes[:80])

    def perform(self,request,directory):
        request=action(request);name=request["name"]
        if name in ("web_search","web_read","save_image"):raise ValueError("联网动作由小鲸鱼核心执行")
        if name=="apps":
            return dict(packages=[p for p in self.apps() if p not in PRIVATE_APPS][:300])
        if name=="observe":
            return self.observe()
        if name=="open":
            if request["package"] in PRIVATE_APPS:
                raise ValueError("该应用需要本人操作")
            self.observed=None
            result=self.open(request["package"])
            # Opening is only a submission; the next observation must confirm what actually appeared.
            return result
        if name=="download":
            saved=self.download(request["url"],directory)
            return {k:v for k,v in saved.items() if k!="path"}
        if name=="install":
            path=Path(directory)/(request["sha256"]+".apk")
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=request["sha256"]:
                raise ValueError("仅可安装本程序完整下载并核对哈希的安装包")
            self.observed=None
            return self.install(path)
        if name=="read_notebook":
            page=self.notebook(request["kind"],after=request["after"])
            for row in page.get("entries",[]):
                row["body_truncated"]=len(row.get("body",""))>1500
                row["body"]=row.get("body","")[:1500]
            return page
        self.verify()
        if name=="home":
            self.observed=None
            self.input("keyevent","3")
            return dict(submitted=True)
        if request["screen"]!=self.observed:
            raise ValueError("请重新观察页面；每次观察只允许执行一个界面动作")
        view=self.observe(private=True)
        if request["screen"]!=view["screen"]:
            raise ValueError("页面已经变化，本次未操作；请重新观察")
        if name=="back":
            self.observed=None
            self.input("keyevent","4",expected=view["package"])
            return dict(submitted=True)
        # ponytail: text/password flags refuse exposed auth/payment screens; custom drawn controls remain unverified.
        if view["sensitive"]:
            raise ValueError("当前涉及账户验证、支付或敏感设置，需要本人操作")
        if name=="swipe":
            self.observed=None
            self.swipe(*(request[k] for k in ("x1","y1","x2","y2","duration")),expected=view["package"])
            return dict(submitted=True)
        node=next((n for n in view["nodes"] if n["node"]==request["node"]),None)
        if not node or not node["enabled"] or (name=="tap" and not node["clickable"]) or (name=="type" and not node["editable"]):
            raise ValueError("目标控件不能执行该操作")
        bounds=re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]",node["bounds"])
        if not bounds:
            raise ValueError("控件位置无效")
        x1,y1,x2,y2=map(int,bounds.groups())
        if x2<=x1 or y2<=y1:
            raise ValueError("控件不在可见范围")
        if name=="tap":
            self.observed=None
            self.tap((x1+x2)//2,(y1+y2)//2,expected=view["package"])
            return dict(submitted=True)
        if not node["focused"]:
            raise ValueError("请先点击输入框并重新观察，再输入文字")
        self.observed=None
        response=self.shell("content","call","--uri","content://cn.littlewhale.bridge.control","--method","clipboard","--arg",base64.b64encode(request["text"].encode()).decode())
        if not re.search(r"\bclipboard_set=true(?:[},\s]|$)",response):
            raise ValueError("中文输入助手未确认，本次未粘贴；请核对虚拟机助手版本")
        if not self.permitted() or self.foreground()!=view["package"]:
            raise ValueError("已暂停或应用已切换，未粘贴文字")
        self.input("keyevent","279",expected=view["package"])
        # Read back internally; editable field contents are never included in a screen observation sent to the model.
        deadline=time.monotonic()+6
        while True:
            after=self.observe(private=True)
            found=next((n for n in after["nodes"] if n["node"]==node["node"] and n["resource_id"]==node["resource_id"] and n["editable"]),None)
            if found and found.get("value")!=node.get("value") and request["text"] in found.get("value",""):
                return dict(typed_verified=True)
            if time.monotonic()>=deadline:
                raise UncertainAction("没有读回输入的文字，不能确认输入成功；本次不自动重试")
            time.sleep(.2)


def main():
    parser = argparse.ArgumentParser(description="小鲸鱼独立虚拟手机控制；不会回退到主手机")
    parser.add_argument("--config", type=Path, default=Path.home() / ".littlewhale" / "device.json")
    parser.add_argument("--adb", default="adb")
    parser.add_argument("--adb-port", type=int, default=5037)
    parser.add_argument("--screenshot", type=Path)
    parser.add_argument("--open")
    parser.add_argument("--apps",action="store_true")
    parser.add_argument("--download")
    parser.add_argument("--install",type=Path)
    options = parser.parse_args()
    phone = Phone(json.loads(options.config.read_text("utf-8")), options.adb, options.adb_port)
    result = phone.connect()
    if options.open:
        result = phone.open(options.open)
    if options.apps:
        result={"packages":phone.apps()}
    if options.download:
        result=phone.download(options.download,options.config.parent/"downloads")
    if options.install:
        result=phone.install(options.install)
    if options.screenshot:
        options.screenshot.write_bytes(phone.screenshot())
        result["screenshot"] = str(options.screenshot)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
