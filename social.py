"""Durable GUI notification and local OneBot transports sharing authorization and outbox."""
import base64
import hashlib
import http.client
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request

from whale import atomic, text, Busy, NoRedirect, image_type, QQ_FACE_NAMES

MEDIA_LIMIT=5_000_000


def image_address(host,address):
    ip=ipaddress.ip_address(address)
    # This VM's proxy uses benchmark-range fake DNS. Keep the exception to exact QQ CDNs;
    # TLS still verifies the original hostname before any HTTP bytes are sent.
    return ip.is_global or (host in ("multimedia.nt.qq.com.cn","gchat.qpic.cn","gxh.vip.qq.com") and ip in ipaddress.ip_network("198.18.0.0/15"))


def image_target(value):
    if not isinstance(value,str) or len(value)>2000 or any(ord(c)<=32 or ord(c)==127 for c in value):raise ValueError("图片地址无效")
    parsed=urllib.parse.urlsplit(value)
    if parsed.scheme!="https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment or parsed.port not in (None,443):
        raise ValueError("图片地址须为不含账号的HTTPS链接")
    host=parsed.hostname.lower()
    if host=="localhost" or host.endswith(".local") or host.endswith(".internal"):
        raise ValueError("图片地址不安全")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("图片地址不安全")
    try:
        addresses=list(dict.fromkeys(item[4][0] for item in socket.getaddrinfo(host,443,type=socket.SOCK_STREAM)))
    except OSError:
        raise ValueError("图片地址无法解析") from None
    if addresses and any(not image_address(host,address) for address in addresses) and all(ipaddress.ip_address(a).is_global or ipaddress.ip_address(a) in ipaddress.ip_network("198.18.0.0/15") for a in addresses):
        # Resolve proxy fake DNS through HTTPS, then connect only to validated public IPs.
        request=urllib.request.Request("https://cloudflare-dns.com/dns-query?"+urllib.parse.urlencode(dict(name=host,type="A")),headers={"Accept":"application/dns-json"})
        try:
            with urllib.request.build_opener(NoRedirect).open(request,timeout=8) as response:raw=response.read(65537)
            if len(raw)>65536:raise ValueError("图片DNS响应过大")
            result=json.loads(raw)
            addresses=[r["data"] for r in result.get("Answer",[]) if r.get("type")==1] if result.get("Status")==0 else []
            if any(not ipaddress.ip_address(a).is_global for a in addresses):raise ValueError("图片地址不安全")
        except (OSError,http.client.HTTPException,ValueError,TypeError,KeyError):raise ValueError("图片公网地址无法安全解析") from None
    if not addresses or any(not image_address(host,address) for address in addresses):
        raise ValueError("图片地址不安全")
    return parsed,addresses


def image_url(value):
    image_target(value)
    return value


class ImageConnection(http.client.HTTPSConnection):
    def __init__(self,host,address):
        super().__init__(host,timeout=15)
        self.address=address

    def connect(self):
        # Pin the already-validated address; check the peer before any HTTP request leaves.
        sock=socket.create_connection((self.address,443),self.timeout)
        try:
            if not image_address(self.host,sock.getpeername()[0]):raise ValueError("图片地址不安全")
            self.sock=self._context.wrap_socket(sock,server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


def fetch_bytes(url,limit):
    """Bounded HTTPS reads using the same DNS/peer checks for pages and images."""
    for attempt in range(4):
        parsed,addresses=image_target(url)
        connection=ImageConnection(parsed.hostname,addresses[0])
        try:
            connection.request("GET",urllib.parse.urlunsplit(("","",parsed.path or "/",parsed.query,"")),headers={"Accept":"image/*" if limit==MEDIA_LIMIT else "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5"})
            response=connection.getresponse()
            if response.status in (301,302,303,307,308):
                location=response.getheader("Location")
                if not isinstance(location,str) or not location:raise ValueError("图片重定向无效")
                url=urllib.parse.urljoin(url,location)
                continue
            if response.status!=200:raise ValueError("图片下载HTTP "+str(response.status))
            raw=response.read(limit+1)
            content_type=response.getheader("Content-Type") or ""
            break
        except (OSError,http.client.HTTPException):
            # No collection or send happened: let the model handle this failed read, not an uncertain action.
            raise ValueError("图片或网页下载失败或超时，本次未自动重试") from None
        finally:connection.close()
    else:raise ValueError("图片重定向过多")
    if len(raw)>limit:raise ValueError("图片或网页超过读取大小上限")
    return raw,url,content_type


def fetch_image(url):
    raw,_,_=fetch_bytes(url,MEDIA_LIMIT)
    mime=image_type(raw)
    if not mime:raise ValueError("图片格式不支持")
    return mime,raw

PACKAGES = {"com.tencent.mobileqq": "QQ", "com.tencent.mm": "微信"}
POLICY=dict(private_per_minute=6,group_per_minute=2,total_per_minute=12,total_per_day=500,groups_silent=False)


def policy(value):
    if not isinstance(value,dict) or set(value)!=set(POLICY) or type(value["groups_silent"]) is not bool:
        raise ValueError("消息频率设置格式错误")
    for key,maximum in (("private_per_minute",20),("group_per_minute",5),("total_per_minute",20),("total_per_day",10000)):
        if type(value[key]) is not int or not 1<=value[key]<=maximum:
            raise ValueError("消息频率须为范围内的整数")
    return dict(value)


def identity(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("会话或事件标识无效")
    return value


def qq_message(parts, sender=None, heard=None):
    heard=heard or {}
    message="".join(p["text"] if p["type"]=="text" else "[QQ小表情："+p["name"]+"，编号"+str(p["id"])+"]" if p["type"]=="face" else
        "["+("表情包" if p.get("sticker") else "图片")+"，待读取；说明："+p.get("summary","")+"]" if p["type"]=="image" else
        "[语音转写："+heard[i]+"]" if p["type"]=="voice" and i in heard else "["+p["type"]+"：内容尚未解析]" for i,p in enumerate(parts))
    return ("[群成员QQ "+sender+"] " if sender else "")+message


class Social:
    def __init__(self, whale, packages=PACKAGES):
        self.whale, self.packages = whale, packages
        self.ready=threading.Event()
        self.processing=threading.Lock()
        path = whale.home / "bridge-token"
        if not path.exists():
            atomic(path, secrets.token_urlsafe(32).encode())
        self.token = text(path.read_text("utf-8"), 100)
        with whale.lock, whale.db:
            whale.db.executescript("""
                CREATE TABLE IF NOT EXISTS social_threads(
                    id TEXT PRIMARY KEY, package TEXT NOT NULL, title TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'unknown', person TEXT, enabled INTEGER NOT NULL DEFAULT 0,
                    trigger TEXT NOT NULL DEFAULT '', revision INTEGER NOT NULL DEFAULT 0,
                    latest TEXT, seen REAL, replyable INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS social_inbox(
                    id TEXT PRIMARY KEY, thread TEXT, status TEXT, at REAL);
                CREATE TABLE IF NOT EXISTS social_outbox(
                    event TEXT, part INTEGER, thread TEXT, revision INTEGER, content TEXT,
                    status TEXT NOT NULL DEFAULT 'ready', at REAL, media TEXT, PRIMARY KEY(event,part));
                CREATE TABLE IF NOT EXISTS social_rate(
                    event TEXT,part INTEGER,thread TEXT,kind TEXT,at REAL NOT NULL,PRIMARY KEY(event,part));
                CREATE INDEX IF NOT EXISTS social_rate_time ON social_rate(at);
                CREATE INDEX IF NOT EXISTS social_inbox_work ON social_inbox(status,at);
            """)
            if "media" not in {row[1] for row in whale.db.execute("PRAGMA table_info(social_outbox)")}:
                whale.db.execute("ALTER TABLE social_outbox ADD COLUMN media TEXT")
            # A working request may already have incurred cost. Never replay it after restart.
            whale.db.execute("UPDATE social_inbox SET status='interrupted' WHERE status='working'")
            whale.db.execute("UPDATE social_outbox SET status='uncertain' WHERE status='claimed'")
        self.qq=QqChannel(self)
        self.whale.qq=self.qq

    def snapshot(self):
        with self.whale.lock:
            return dict(threads=[dict(r) for r in self.whale.db.execute(
                "SELECT * FROM social_threads ORDER BY seen DESC LIMIT 100")],
                deliveries=[dict(r) for r in self.whale.db.execute(
                    "SELECT event,part,thread,status,at FROM social_outbox ORDER BY at DESC,part LIMIT 30")],
                helper=self.whale.setting("bridge_status", {}),policy=self.policy(),
                last_submission=self.whale.setting("social_last_submission"),qq=self.qq.health())

    def policy(self):
        return policy(dict(POLICY,**self.whale.setting("social_policy",{})))

    def save_policy(self,body):
        value=policy(body)
        w=self.whale
        with w.lock,w.db:
            w.set_setting("social_policy",value)
            if value["groups_silent"]:
                w.db.execute("UPDATE social_threads SET revision=revision+1 WHERE kind='group'")
                w.db.execute("UPDATE social_outbox SET status='cancelled' WHERE status='ready' AND thread IN (SELECT id FROM social_threads WHERE kind='group')")
        return value

    def configure(self, body):
        thread = identity(body.get("thread"))
        kind = body.get("kind")
        if kind not in ("private", "group") or type(body.get("enabled")) is not bool or type(body.get("owner")) is not bool:
            raise ValueError("请明确选择私聊/群聊、是否本人以及自动回复开关")
        if kind == "group" and body["owner"]:
            raise ValueError("群聊必须使用独立记忆，不能绑定为你本人")
        trigger = text(body.get("trigger", ""), 60, kind != "group")
        person = "owner" if body["owner"] else "social:" + thread
        w = self.whale
        with w.lock, w.db:
            row = w.db.execute("SELECT * FROM social_threads WHERE id=?", (thread,)).fetchone()
            if not row:
                raise ValueError("会话尚未被虚拟机识别")
            route=w.db.execute("SELECT * FROM social_qq WHERE thread=?",(thread,)).fetchone()
            if route:
                if kind!=route["kind"]:raise ValueError("QQ会话类型由账号及群号确定")
                if not body["owner"]:person="qq:"+route["bot"]+":"+route["target"] if kind=="private" else "qq-group:"+thread
            w.db.execute("INSERT OR IGNORE INTO people(id,name) VALUES(?,?)", (person, row["title"]))
            w.db.execute("UPDATE social_threads SET kind=?,person=?,enabled=?,trigger=?,revision=revision+1 WHERE id=?",
                         (kind, person, body["enabled"], trigger, thread))
            w.db.execute("UPDATE social_outbox SET status='cancelled' WHERE thread=? AND status='ready'", (thread,))
        return {"saved": True}

    def receive(self, body):
        if body.get("package") not in self.packages:raise ValueError("不支持的消息来源")
        if body.get("package")=="com.tencent.mobileqq" and self.whale.setting("qq_bot"):
            return dict(status="protocol_only",parts=[])
        return self._receive(body)

    def _receive(self, body, route=None, parts=None, person=None):
        w = self.whale
        thread, event = identity(body.get("thread")), identity(body.get("event"))
        package, title = body.get("package"), text(body.get("title"), 200)
        if (package not in self.packages and not route) or type(body.get("replyable")) is not bool or type(body.get("fresh")) is not bool:
            raise ValueError("不支持的消息来源或状态")
        message = text(body.get("message"), 4000)
        # Notification grouping identity is supplied by the paired helper, never an arbitrary web caller.
        with w.lock, w.db:
            row = w.db.execute("SELECT * FROM social_threads WHERE id=?", (thread,)).fetchone()
            if row and (row["package"] != package or row["title"] != title):
                raise ValueError("会话身份改变，请重新配对")
            if not row and w.db.execute("SELECT count(*) FROM social_threads").fetchone()[0] >= 100:
                raise ValueError("已达100个会话上限")
            w.db.execute("INSERT OR IGNORE INTO social_threads(id,package,title) VALUES(?,?,?)", (thread, package, title))
            if route:
                w.db.execute("INSERT OR IGNORE INTO social_qq VALUES(?,?,?,?)",(thread,*route))
                w.db.execute("UPDATE social_threads SET kind=? WHERE id=? AND kind='unknown'",(route[1],thread))
            existing=w.db.execute("SELECT thread FROM social_inbox WHERE id=?", (event,)).fetchone()
            if existing:
                if existing["thread"]!=thread:raise ValueError("重复事件属于另一个会话")
                job=w.db.execute("SELECT message FROM social_jobs WHERE event=?",(event,)).fetchone()
                if job and job["message"]!=message:raise ValueError("重复事件与原消息内容不一致")
                old=w.db.execute("SELECT parts,person FROM social_parts WHERE event=?",(event,)).fetchone()
                if route and old and (json.loads(old["parts"])!=parts or old["person"]!=person):raise ValueError("重复QQ事件内容不一致")
                return self.pending(event)
            if route and parts and parts[0]["type"]=="recall":
                w.db.execute("INSERT INTO social_inbox VALUES(?,?,'observed',?)",(event,thread,w.clock()))
                w.db.execute("INSERT INTO social_parts VALUES(?,?,?)",(event,person,json.dumps(parts)))
                return dict(status="observed",parts=[])
            w.db.execute("UPDATE social_threads SET latest=?,seen=?,replyable=? WHERE id=?",
                         (event, w.clock(), body["replyable"], thread))
            row = w.db.execute("SELECT * FROM social_threads WHERE id=?", (thread,)).fetchone()
            status = ("baseline" if not body["fresh"] else "disabled" if not row["enabled"]
                      else "no_reply_channel" if not body["replyable"] else "paused" if w.setting("paused", False)
                      else "silent" if row["kind"] == "group" and (self.policy()["groups_silent"] or (row["trigger"] not in message and not (route and any(p.get('type')=='at' and p.get('target')==route[0] for p in parts or [])))) else "queued")
            if status=="queued" and w.db.execute("SELECT count(*) FROM social_inbox WHERE status IN ('queued','working')").fetchone()[0]>=1000:
                raise ValueError("待处理消息已满，请稍后再试")
            w.db.execute("INSERT INTO social_inbox VALUES(?,?,?,?)", (event, thread, status, w.clock()))
            if route:
                w.db.execute("INSERT INTO social_parts VALUES(?,?,?)",(event,person,json.dumps(parts,ensure_ascii=False)))
            if status=="queued":
                w.db.execute("INSERT INTO social_jobs VALUES(?,?,?,?)",(event,message,row["person"],row["revision"]))
        self.ready.set()
        return self.pending(event)

    def work(self,stopped):
        while not stopped.is_set():
            self.ready.clear()
            try:
                processed=self.qq.deliver_one() or self.process_one()
                if self.whale.setting("social_worker_error",False):self.whale.set_setting("social_worker_error",False)
            except Exception:
                self.whale.set_setting("social_worker_error",True)
                processed=False
            if not processed:self.ready.wait(.5)

    def tick(self):
        w=self.whale
        with w.lock:
            owners=w.db.execute("""SELECT t.* FROM social_threads t JOIN social_qq q ON q.thread=t.id
                WHERE t.enabled=1 AND t.person='owner' AND q.kind='private' AND q.bot=?""",(w.setting("qq_bot"),)).fetchall()
            target=owners[0] if len(owners)==1 else None
        result=w.tick(thread="qq:"+target["id"] if target else None)
        if not result or not target or not (result["lines"] or result.get("photo") or "face" in result):return result
        event=hashlib.sha256(("proactive:"+result["event_id"]).encode()).hexdigest()
        with w.lock,w.db:
            current=w.db.execute("SELECT * FROM social_threads WHERE id=?",(target["id"],)).fetchone()
            if not current["enabled"] or current["revision"]!=target["revision"] or w.setting("paused",False) or not w.config["proactive"]:
                return result
            inserted=w.db.execute("INSERT OR IGNORE INTO social_inbox VALUES(?,?,'generated',?)",(event,target["id"],w.clock())).rowcount
            if inserted:
                photo=result.get("photo") or ("face:"+str(result["face"]) if "face" in result else None)
                for part,line in enumerate(result["lines"]):
                    w.db.execute("INSERT INTO social_outbox(event,part,thread,revision,content,media,at) VALUES(?,?,?,?,?,?,?)",(event,part,target["id"],target["revision"],line,photo if part==0 and target["package"]=="onebot.qq" else None,w.clock()))
                if photo and not result["lines"]:
                    w.db.execute("INSERT INTO social_outbox(event,part,thread,revision,content,media,at) VALUES(?,0,?,?,'',?,?)",(event,target["id"],target["revision"],photo,w.clock()))
        self.ready.set()
        return result

    def process_one(self):
        if not self.processing.acquire(blocking=False):return False
        try:
            return self._process_one()
        finally:self.processing.release()

    def _process_one(self):
        w=self.whale
        if w.behavior.locked():return False
        with w.lock,w.db:
            job=w.db.execute("""SELECT j.*,i.thread,i.at FROM social_jobs j JOIN social_inbox i ON i.id=j.event
                WHERE i.status='queued' ORDER BY i.at,i.rowid LIMIT 1""").fetchone()
            if not job:return False
            event,thread=job["event"],job["thread"]
            row=w.db.execute("SELECT * FROM social_threads WHERE id=?",(thread,)).fetchone()
            if (not row or not row["enabled"] or not row["replyable"] or row["person"]!=job["person"] or row["revision"]!=job["revision"]
                    or (row["package"]!="onebot.qq" and row["latest"]!=event) or w.setting("paused",False) or not 0<=w.clock()-job["at"]<=300
                    or (row["kind"]=="group" and self.policy()["groups_silent"])):
                w.db.execute("UPDATE social_inbox SET status='cancelled' WHERE id=?",(event,))
                return True
            w.db.execute("UPDATE social_inbox SET status='working' WHERE id=?",(event,))
            stored=w.db.execute("SELECT parts,person FROM social_parts WHERE event=?",(event,)).fetchone()
        try:
            images=[];missing=False;sticker=False;reference=False;errors=[]
            message=job['message'];voice_input=False;hearing_error=None
            if stored:
                parts=json.loads(stored["parts"])
                recordings=[(i,p) for i,p in enumerate(parts) if isinstance(p,dict) and p.get('type')=='voice']
                if recordings:
                    hearing_stage='record'
                    try:
                        # ponytail: handle QQ's usual single recording; multi-record packets need bounded sequential ASR.
                        if len(recordings)!=1:raise ValueError('一次请发一条语音')
                        from speech import transcribe
                        i,part=recordings[0]
                        raw=self.qq.record_wave(part)
                        hearing_stage='asr'
                        transcript=text(transcribe(w.home,raw),4000)
                        # Keep original parts/job immutable so OneBot retries still compare exactly.
                        message=qq_message(parts,stored['person'].rsplit(':',1)[-1] if row['kind']=='group' else None,{i:transcript})
                        voice_input=True
                        w.set_setting('hearing_status',dict(at=w.clock(),ok=True,local=True,bytes=len(raw)))
                    except (ValueError,OSError,http.client.HTTPException) as error:
                        hearing_error='这条语音暂时没听清，先用文字告诉我吧' if len(recordings)==1 else '一次先发一条语音，我才能听清楚'
                        w.set_setting('hearing_status',dict(at=w.clock(),ok=False,local=True,stage=hearing_stage,error=type(error).__name__))
                image_parts=[] if hearing_error else [p for p in parts if isinstance(p,dict) and p.get("type")=="image"]
                # ponytail: explicit Chinese references reuse one same-sender picture within 5 minutes;
                # broader multilingual/image search should become a deliberate retrieval tool.
                if not hearing_error and not image_parts and re.search(r"这[张幅个].*(?:图|照|表情)|这[张幅]图|刚才.*(?:图|照|表情)|上[一张个].*(?:图|照|表情)|收藏.*(?:图|照|表情)|发.*(?:回来|还给)|描述.*图",message):
                    with w.lock:
                        recent=w.db.execute("SELECT p.parts FROM social_parts p JOIN social_inbox i ON i.id=p.event WHERE i.thread=? AND p.person=? AND i.id!=? AND i.at BETWEEN ? AND ? ORDER BY i.at DESC,i.rowid DESC LIMIT 12",(thread,stored["person"],event,job["at"]-300,job["at"])).fetchall()
                    for previous in recent:
                        image_parts=[p for p in json.loads(previous["parts"]) if isinstance(p,dict) and p.get("type")=="image"]
                        if image_parts:
                            image_parts=image_parts[:1];reference=True
                            break
                missing=len(image_parts)>4
                for part in image_parts[:4]:
                    try:
                        image=fetch_image(part.get("url"))
                        if sum(len(raw) for _,raw in images)+len(image[1])>10_000_000:
                            missing=True
                            continue
                        images.append(image)
                        if len(images)==1:sticker=part.get("sticker",False)
                    except (ValueError,OSError,http.client.HTTPException) as error:
                        missing=True
                        errors.append(str(error)[:80] if type(error) is ValueError and str(error).startswith("图片") else type(error).__name__)
                if image_parts:
                    w.set_setting("qq_media",dict(at=w.clock(),received=len(image_parts),attached=len(images),missing=missing or len(image_parts)>4,
                        reference=reference,errors=errors,formats=[mime for mime,_ in images],bytes=sum(len(raw) for _,raw in images)))
            message+=("\n[系统：附图来自本会话同一人五分钟内最近发送的图片，用于本次指代。]" if reference else "")+("\n[系统：部分图片下载失败或超出数量/大小上限，无法判断那些图片的内容，请如实说明。]" if missing else "")
            with w.lock,w.db:
                current=w.db.execute("SELECT * FROM social_threads WHERE id=?",(thread,)).fetchone()
                if current['revision']!=row['revision'] or w.setting('paused',False):
                    w.db.execute("UPDATE social_inbox SET status='cancelled' WHERE id=?",(event,))
                    return True
            if hearing_error or len(message)>4000:
                result=dict(lines=[hearing_error or '这条消息有点长，分开几条发给我吧'])
            else:
                result = w.chat(message, "social:" + event, person=job["person"],thread=("qq:" if row["package"]=="onebot.qq" else "gui:")+thread,images=images,sticker=sticker,voice_input=voice_input)
            voice=None
            if result.get('voice') and row['package']=='onebot.qq' and job['person']=='owner':
                try:
                    from speech import synthesize
                    if not w.config['tts_enabled']:raise ValueError('语音回复尚未开启')
                    voice=synthesize(w.home,'\n'.join(result['lines']),voice=w.config['tts_voice'])
                    w.set_setting('voice_status',dict(at=w.clock(),ok=True,local=True))
                except (ValueError,OSError):
                    w.set_setting('voice_status',dict(at=w.clock(),ok=False,local=True))
                    result['lines']=[*result['lines'][:2],'语音暂时没生成成功，先用文字回你']
            with w.lock, w.db:
                current = w.db.execute("SELECT * FROM social_threads WHERE id=?", (thread,)).fetchone()
                if current["revision"] != row["revision"] or (row["package"]!="onebot.qq" and current["latest"] != event) or w.setting("paused", False):
                    status = "cancelled"
                else:
                    status = "generated"
                    photo = (result.get("photo") or ("face:"+str(result["face"]) if "face" in result else None)) if row["package"] == "onebot.qq" else None
                    if voice:
                        w.db.execute("INSERT INTO social_outbox(event,part,thread,revision,content,media,at) VALUES(?,0,?,?,'',?,?)",(event,thread,row['revision'],'voice:'+voice,w.clock()))
                    for part, content in enumerate([] if voice else result["lines"]):
                        w.db.execute("INSERT INTO social_outbox(event,part,thread,revision,content,media,at) VALUES(?,?,?,?,?,?,?)",
                                     (event, part, thread, row["revision"], content, photo if part == 0 else None, w.clock()))
                    if photo and not result["lines"]:
                        w.db.execute("INSERT INTO social_outbox(event,part,thread,revision,content,media,at) VALUES(?,0,?,?,'',?,?)",
                                     (event, thread, row["revision"], photo, w.clock()))
                w.db.execute("UPDATE social_inbox SET status=? WHERE id=?", (status, event))
            return True
        except Busy:
            with w.lock,w.db:
                w.db.execute("UPDATE social_inbox SET status='queued' WHERE id=? AND status='working'",(event,))
            return False
        except Exception:
            with w.lock, w.db:
                w.db.execute("UPDATE social_inbox SET status='failed' WHERE id=?", (event,))
            return True

    def poll(self,body):
        event,thread=identity(body.get("event")),identity(body.get("thread"))
        with self.whale.lock:
            row=self.whale.db.execute("SELECT thread FROM social_inbox WHERE id=?",(event,)).fetchone()
            if row and row["thread"]!=thread:raise ValueError("消息不属于该会话")
            return self.pending(event)

    def pending(self, event):
        with self.whale.lock:
            row = self.whale.db.execute("SELECT status FROM social_inbox WHERE id=?", (event,)).fetchone()
            return dict(status=row[0] if row else "missing", parts=[r[0] for r in self.whale.db.execute(
                "SELECT part FROM social_outbox WHERE event=? AND status='ready' ORDER BY part", (event,))])

    def claim(self, body):
        event, thread, part = identity(body.get("event")), identity(body.get("thread")), body.get("part")
        if type(part) is not int or not 0 <= part <= 2:
            raise ValueError("回复序号无效")
        w = self.whale
        with w.lock, w.db:
            row = w.db.execute("""SELECT o.*,t.enabled,t.latest,t.kind,t.package,t.revision AS current_revision FROM social_outbox o
                JOIN social_threads t ON t.id=o.thread WHERE o.event=? AND o.part=? AND o.thread=?""", (event, part, thread)).fetchone()
            if not row or row["status"] != "ready":
                return {"send": False}
            limits=self.policy()
            if (w.setting("paused", False) or (row["kind"]=="group" and limits["groups_silent"]) or not row["enabled"] or (row["package"]!="onebot.qq" and row["latest"] != event)
                    or row["revision"] != row["current_revision"] or not 0 <= w.clock() - row["at"] <= 300):
                w.db.execute("UPDATE social_outbox SET status='cancelled' WHERE event=? AND part=?", (event, part))
                return {"send": False}
            now=w.clock()
            w.db.execute("DELETE FROM social_rate WHERE at<?",(now-172800,))
            # Count actual claims, including failed/uncertain sends. Future timestamps retain quota after a clock rollback.
            total=w.db.execute("SELECT count(*) FROM social_rate WHERE at>?",(now-60,)).fetchone()[0]
            per_thread=w.db.execute("SELECT count(*) FROM social_rate WHERE at>? AND thread=?",(now-60,thread)).fetchone()[0]
            daily=w.db.execute("SELECT count(*) FROM social_rate WHERE at>?",(now-86400,)).fetchone()[0]
            if daily>=limits["total_per_day"] or total>=limits["total_per_minute"] or per_thread>=limits["group_per_minute" if row["kind"]=="group" else "private_per_minute"]:
                w.db.execute("UPDATE social_outbox SET status='throttled' WHERE event=? AND status='ready'",(event,))
                w.db.execute("UPDATE social_inbox SET status='throttled' WHERE id=?",(event,))
                return dict(send=False,reason="rate_limited")
            # Commit before crossing the Android send boundary. An uncertain submission is never replayed.
            w.db.execute("INSERT INTO social_rate VALUES(?,?,?,?,?)",(event,part,thread,row["kind"],now))
            w.db.execute("UPDATE social_outbox SET status='claimed' WHERE event=? AND part=?", (event, part))
            return dict(send=True, content=row["content"], media=row["media"])

    def acknowledge(self, body):
        event, part, status = identity(body.get("event")), body.get("part"), body.get("status")
        if type(part) is not int or not 0 <= part <= 2 or status not in ("submitted", "cancelled", "send_failed", "uncertain"):
            raise ValueError("发送状态无效")
        with self.whale.lock, self.whale.db:
            changed=self.whale.db.execute("UPDATE social_outbox SET status=? WHERE event=? AND part=? AND status='claimed'", (status, event, part)).rowcount
            if changed and status=="submitted":self.whale.set_setting("social_last_submission",self.whale.clock())
        return {"saved": True}

    def report(self, body):
        if type(body.get("connected")) is not bool:
            raise ValueError("连接状态无效")
        status = dict(connected=body["connected"], at=self.whale.clock(),
                      version=text(body.get("version"), 30), sdk=body.get("sdk"))
        if type(status["sdk"]) is not int or not 24 <= status["sdk"] <= 35:
            raise ValueError("系统版本无效")
        status['music']=dict(available=False,track=None)
        if status['connected'] and 'music' in body:
            try:
                music=body['music']
                if not isinstance(music,dict) or type(music.get('available')) is not bool:raise ValueError('Invalid music status')
                track=music.get('track')
                if not music['available'] and track is not None:raise ValueError('Unavailable music has a track')
                if track is not None:
                    if not isinstance(track,dict) or track.get('state') not in ('playing','paused','stopped','buffering','unknown'):raise ValueError('Invalid playback')
                    track=dict(package=text(track.get('package'),200),title=text(track.get('title'),200),artist=text(track.get('artist'),200,True),
                               state=track['state'],position_ms=track.get('position_ms'),duration_ms=track.get('duration_ms'))
                    for key,minimum in (('position_ms',-1),('duration_ms',0)):
                        if type(track[key]) is not int or not minimum<=track[key]<=604800000:raise ValueError('Invalid playback time')
                status['music']=dict(available=music['available'],track=track)
            except (ValueError,TypeError):
                # An optional player's bad metadata must not break QQ health/restart reports.
                pass
        self.whale.set_setting("bridge_status", status)
        # No QQ polling or relogin: retained protocol field is false for older installed helpers.
        return {"paused": self.whale.setting("paused", False), "restart_qq": False}


def qq_id(value):
    if type(value) not in (str,int) or not re.fullmatch(r"[1-9][0-9]{0,19}",str(value)):
        raise ValueError("QQ账号或群号无效")
    return str(value)


class QqChannel:
    name="qq"

    def __init__(self,social):
        self.social=social
        self.w=social.whale
        path=self.w.home/"qq-token"
        if not path.exists():atomic(path,secrets.token_urlsafe(32).encode())
        self.token=text(path.read_text("utf-8"),100)
        self.next_send=0
        self.delivery_lock=threading.Lock()

    def health(self):
        return dict(configured=bool(self.w.setting("qq_bot")),features=self.features(),status=self.w.setting("qq_status",{}),ingress=self.w.setting("qq_ingress",{}),watch={},media=self.w.setting("qq_media",{}))

    def features(self):
        return self.w.setting('qq_features',dict(favorites_enabled=True,qzone_enabled=True,qzone_visibility='friends'))

    def save_features(self,body):
        if not isinstance(body,dict) or set(body)!={'favorites_enabled','qzone_enabled','qzone_visibility'} or any(type(body[k]) is not bool for k in ('favorites_enabled','qzone_enabled')) or body['qzone_visibility'] not in ('friends','private','public'):
            raise ValueError('请明确收藏、空间发布开关与可见范围')
        self.w.set_setting('qq_features',body)
        return dict(saved=True)

    def album_file(self,digest):
        if not isinstance(digest,str) or not re.fullmatch(r'[0-9a-f]{64}',digest):raise ValueError('相册图片标识无效')
        with self.w.lock:
            row=self.w.db.execute('SELECT file,reason FROM album WHERE hash=?',(digest,)).fetchone()
        if not row:raise ValueError('相册图片不存在')
        directory=(self.w.home/'album').resolve()
        path=(directory/row['file']).resolve()
        if path.parent!=directory or not re.fullmatch(digest+r'\.(?:png|jpg|gif|webp)',path.name) or not 1<=path.stat().st_size<=MEDIA_LIMIT:
            raise ValueError('相册图片路径或大小无效')
        raw=path.read_bytes()
        if not image_type(raw) or hashlib.sha256(raw).hexdigest()!=digest:raise ValueError('相册图片格式或校验无效')
        return path,raw,row['reason']

    def perform(self,requested):
        from phone import action,UncertainAction
        requested=action(requested)
        name=requested['name'];w=self.w
        if name not in ('qq_favorite','qq_post'):raise ValueError('不支持的QQ操作')
        with w.lock:
            if w.setting('paused',False):raise ValueError('QQ操作已暂停')
            if not self.features()['favorites_enabled' if name=='qq_favorite' else 'qzone_enabled']:raise ValueError('此项QQ能力已关闭')
        try:
            if qq_id(self.call('get_login_info',{}).get('user_id'))!=qq_id(w.setting('qq_bot')):raise ValueError('QQ登录账号与配对账号不一致')
        except (OSError,http.client.HTTPException):raise ValueError('QQ账号状态暂不可读，未执行操作') from None
        if name=='qq_favorite':
            digest=requested['image'];path,raw,_=self.album_file(digest);md5=hashlib.md5(raw).hexdigest()
            def present():
                try:return any(isinstance(p.get('md5'),str) and p['md5'].lower()==md5 for p in self.call('fetch_custom_face_detail',dict(count=200)))
                except (OSError,ValueError,http.client.HTTPException):raise ValueError('QQ表情收藏暂不可读取') from None
            if present():return dict(hash=digest,qq_favorite=True,already_present=True)
            key='qq_favorite:'+digest
            with w.lock:
                if w.setting('paused',False) or not self.features()['favorites_enabled']:raise ValueError('QQ表情收藏已暂停')
                if w.setting(key):raise UncertainAction('此前收藏请求尚待核对，不重复上传')
                w.set_setting(key,dict(status='claimed',at=w.clock()))
            try:
                self.call('add_custom_face',dict(file=str(path)))
                if not present():raise ValueError('收藏尚未回读确认')
            except Exception:
                w.set_setting(key,dict(status='uncertain',at=w.clock()))
                raise UncertainAction('QQ表情收藏结果待核对，不会自动重复上传') from None
            w.set_setting(key,dict(status='done',at=w.clock()))
            return dict(hash=digest,qq_favorite=True,already_present=False)
        paths=[self.album_file(digest)[0] for digest in requested['photos']]
        with w.lock:
            features=self.features()
            if w.setting('paused',False) or not features['qzone_enabled']:raise ValueError('空间发布已暂停')
            now=w.clock()
            attempts=[t for t in w.setting('qq_post_attempts',[]) if now-t<86400]
            if len(attempts)>=3 or (attempts and now-max(attempts)<600):raise ValueError('空间动态限流：24小时最多3条，间隔至少10分钟')
            w.set_setting('qq_post_attempts',attempts+[now])
            visibility=features['qzone_visibility']
        try:
            result=self.call('send_qzone_msg',dict(content=requested['content'],images=['file://'+str(p) for p in paths],ugc_right={'public':1,'friends':4,'private':64}[visibility]))
            tid=text(result.get('tid'),200)
        except Exception:raise UncertainAction('空间动态发布结果待核对，不会自动重发') from None
        return dict(published=True,tid=tid,visibility=visibility)

    def pair(self):
        bot=qq_id(self.call("get_login_info",{}).get("user_id"))
        before=self.w.setting("qq_bot")
        if before and before!=bot:raise ValueError("QQ账号已更换，请先关闭旧账号会话授权")
        self.w.set_setting("qq_bot",bot)
        self.w.set_setting("qq_status",dict(at=self.w.clock(),event="paired"))
        return self.health()

    def authorize_owner(self,body):
        if set(body)!={"user_id"}:raise ValueError("请提供本人的QQ号")
        bot=qq_id(self.w.setting("qq_bot"));target=qq_id(body["user_id"])
        if target==bot:raise ValueError("本人QQ号不能是小鲸鱼的账号")
        thread=hashlib.sha256(("qq:"+bot+":private:"+target).encode()).hexdigest()
        with self.w.lock,self.w.db:
            if not self.w.db.execute("SELECT 1 FROM social_threads WHERE id=?",(thread,)).fetchone() and self.w.db.execute("SELECT count(*) FROM social_threads").fetchone()[0]>=100:
                raise ValueError("已达100个会话上限")
            self.w.db.execute("INSERT OR IGNORE INTO social_threads(id,package,title,kind,replyable) VALUES(?,'onebot.qq',?,'private',1)",(thread,"QQ private "+target))
            self.w.db.execute("INSERT OR IGNORE INTO social_qq VALUES(?,?,'private',?)",(thread,bot,target))
            return self.social.configure(dict(thread=thread,kind="private",owner=True,enabled=True,trigger=""))

    def call(self,action,body):
        if action not in ("get_login_info","get_status","send_msg","get_record","fetch_custom_face_detail","add_custom_face","send_qzone_msg"):raise ValueError("不允许的QQ操作")
        if action=='fetch_custom_face_detail' and (not isinstance(body,dict) or set(body)!={'count'} or type(body['count']) is not int or not 1<=body['count']<=200):raise ValueError('表情收藏读取数量无效')
        if action=='add_custom_face':
            if not isinstance(body,dict) or set(body)!={'file'} or not isinstance(body['file'],str):raise ValueError('表情收藏参数无效')
            path=Path(body['file'])
            if path!=self.album_file(path.stem)[0]:raise ValueError('只允许收藏经过校验的相册文件')
        if action=='send_qzone_msg':
            if not isinstance(body,dict) or set(body)!={'content','images','ugc_right'} or type(body['ugc_right']) is not int or body['ugc_right'] not in (1,4,64) or not isinstance(body['images'],list):raise ValueError('空间发布参数无效')
            hashes=[]
            for value in body['images']:
                if not isinstance(value,str) or not value.startswith('file://'):raise ValueError('空间配图只允许已校验的相册文件')
                path=Path(value[7:])
                if path!=self.album_file(path.stem)[0]:raise ValueError('空间配图路径无效')
                hashes.append(path.stem)
            from phone import action as validate
            validate(dict(name='qq_post',content=body['content'],photos=hashes))
        if action=='get_record' and os.environ.get('LITTLEWHALE_DISABLE_QQ_AUDIO')=='1':raise ValueError('此实例尚未启用语音接收')
        if action=='get_record' and (not isinstance(body,dict) or set(body)!={'file_id','out_format'} or body['out_format']!='wav'
                or not isinstance(body['file_id'],str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}(?:\.(?:amr|silk|wav|ogg|mp3|m4a))?',body['file_id'],re.ASCII|re.IGNORECASE)):
            raise ValueError('QQ语音标识无效')
        limit=8_100_000 if action=='get_record' else 1_000_000
        request=urllib.request.Request("http://127.0.0.1:3000/"+action,json.dumps(body).encode(),
            {"Content-Type":"application/json","Authorization":"Bearer "+self.token})
        with urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect).open(request,timeout=60 if action=='send_qzone_msg' else 20 if action=='get_record' else 12) as response:
            raw=response.read(limit+1)
        if len(raw)>limit:raise ValueError("QQ响应超限")
        value=json.loads(raw)
        if not isinstance(value,dict) or value.get("status")!="ok" or type(value.get('retcode')) is not int or value['retcode']!=0:raise ValueError("QQ接口未成功")
        data=value.get('data')
        if action=='fetch_custom_face_detail':
            if not isinstance(data,list) or len(data)>200 or any(not isinstance(p,dict) for p in data):raise ValueError('QQ表情详情格式无效')
        elif not isinstance(data,dict) and not (action=='add_custom_face' and data is None):raise ValueError('QQ接口响应格式无效')
        return value["data"]

    def record_wave(self,part):
        data=self.call('get_record',dict(file_id=part.get('file'),out_format='wav'))
        encoded=data.get('base64')
        if not isinstance(encoded,str) or not encoded or len(encoded)>8_000_000:raise ValueError('QQ语音数据无效或过大')
        try:raw=base64.b64decode(encoded,validate=True)
        except ValueError:raise ValueError('QQ语音编码无效') from None
        from speech import wav_info
        wav_info(raw)
        return raw

    def receive(self,body):
        bot=qq_id(body.get("self_id"))
        if bot!=self.w.setting("qq_bot"):raise ValueError("QQ账号与配对账号不符")
        post=body.get("post_type")
        if post=="meta_event":
            self.w.set_setting("qq_status",dict(at=self.w.clock(),event="heartbeat"))
            return dict(status="observed")
        if post not in ("message","message_sent","notice"):return dict(status="ignored")
        if post=="notice" and body.get("notice_type") not in ("friend_recall","group_recall"):return dict(status="ignored")
        if post=="message_sent" or body.get("user_id")==body.get("self_id"):return dict(status="self_ignored")
        kind="group" if "group_id" in body else "private"
        target=qq_id(body.get("group_id") if kind=="group" else body.get("user_id"))
        sender=qq_id(body.get("user_id"))
        if sender==bot:return dict(status="self_ignored")
        if post=="message" and body.get("message_type")!=kind:raise ValueError("QQ会话类型不一致")
        thread=hashlib.sha256(("qq:"+bot+":"+kind+":"+target).encode()).hexdigest()
        message_id=body.get("message_id")
        if type(message_id) not in (str,int) or not re.fullmatch(r"-?[0-9]{1,20}",str(message_id)):
            if post=="notice":return dict(status="ignored")
            raise ValueError("QQ消息编号无效")
        event=hashlib.sha256(("qq:"+bot+":"+post+":"+str(body.get("notice_type",""))+":"+kind+":"+target+":"+str(message_id)).encode()).hexdigest()
        at=body.get("time")
        if type(at) not in (int,float) or not math.isfinite(at):raise ValueError("QQ事件时间无效")
        parts=[]
        if post=="notice":
            if body.get("notice_type") not in ("friend_recall","group_recall"):return dict(status="ignored")
            parts=[dict(type="recall",message_id=str(message_id))]
            message="[消息已撤回；不恢复内容]"
        else:
            segments=body.get("message")
            if not isinstance(segments,list) or not 1<=len(segments)<=100:raise ValueError("QQ消息须为消息段数组")
            for segment in segments:
                if not isinstance(segment,dict) or not isinstance(segment.get("data"),dict):raise ValueError("QQ消息段无效")
                typ,data=segment.get("type"),segment["data"]
                if typ=="text":parts.append(dict(type="text",text=text(data.get("text"),4000,True)))
                elif typ in ("image","mface","record"):
                    part=dict(type="voice" if typ=="record" else "image",file=text(data.get("file",""),2000,True),url=text(data.get("url",""),2000,True))
                    if typ!="record":part.update(sticker=typ=="mface" or data.get("sub_type") in (1,"1") or bool(data.get("emoji_id")),summary=text(data.get("summary") or "",120,True))
                    parts.append(part)
                elif typ=="face":
                    identifier=str(data.get("id",""))
                    if not re.fullmatch(r"[0-9]{1,5}",identifier):raise ValueError("QQ表情编号无效")
                    parts.append(dict(type="face",id=int(identifier),name=QQ_FACE_NAMES.get(int(identifier),"未知表情")))
                elif typ=="reply":parts.append(dict(type="reply",message_id=str(data.get("id",""))[:30]))
                elif typ=="at":parts.append(dict(type="at",target=str(data.get("qq",""))[:30]))
                else:parts.append(dict(type="unsupported"))
            message=qq_message(parts,sender if kind=='group' else None)
        fresh=post=="message" and 0<=self.w.clock()-at<=120
        # Keep immutable references; the authorized worker reads images or loopback audio later.
        return self.social._receive(dict(thread=thread,event=event,package="onebot.qq",title="QQ "+kind+" "+target,
            message=text(message,4000),fresh=fresh,replyable=post=="message"),route=(bot,kind,target),parts=parts,person="qq:"+bot+":"+sender)

    def send(self,thread,parts):
        if not parts or any(p.get("type") not in ("text","image","face","voice") for p in parts):raise ValueError("QQ消息内容无效")
        with self.w.lock:
            route=self.w.db.execute("SELECT * FROM social_qq WHERE thread=?",(identity(thread),)).fetchone()
        if not route or route["bot"]!=self.w.setting("qq_bot"):raise ValueError("QQ会话未配对")
        segments=[]
        for part in parts:
            if part["type"]=="text":
                segments.append(dict(type="text",data=dict(text=text(part.get("text"),240))))
            elif part["type"]=="face":
                face=part.get("id")
                if type(face) is not int or face not in QQ_FACE_NAMES:raise ValueError("QQ表情编号无效")
                segments.append(dict(type="face",data=dict(id=str(face))))
            elif part['type']=='voice':
                if len(parts)!=1:raise ValueError('语音须单独发送')
                with self.w.lock:
                    owner=self.w.db.execute("SELECT person,enabled FROM social_threads WHERE id=?",(thread,)).fetchone()
                if route['kind']!='private' or not owner or owner['person']!='owner' or not owner['enabled']:raise ValueError('语音仅限已授权的主人私聊')
                from speech import audio
                raw=audio(self.w.home,part.get('file'))
                segments.append(dict(type='record',data=dict(file='base64://'+base64.b64encode(raw).decode())))
            else:
                digest=part.get("file")
                _,raw,reason=self.album_file(digest)
                sticker=reason.startswith("[表情包]")
                segments.append(dict(type="image",data=dict(file="base64://"+base64.b64encode(raw).decode(),sub_type=1 if sticker else 0,summary="[表情包]" if sticker else "[图片]")))
        body=dict(message_type=route["kind"],message=segments,auto_escape=True)
        body["group_id" if route["kind"]=="group" else "user_id"]=int(route["target"])
        result=self.call("send_msg",body)
        if type(result.get("message_id")) not in (int,str):raise ValueError("QQ发送结果不明确")
        return str(result["message_id"])

    def deliver_one(self):
        if self.w.clock()<self.next_send or not self.delivery_lock.acquire(blocking=False):return False
        try:
            with self.w.lock:
                row=self.w.db.execute("""SELECT o.* FROM social_outbox o JOIN social_qq q ON q.thread=o.thread
                    WHERE o.status='ready' ORDER BY o.at,o.event,o.part LIMIT 1""").fetchone()
            if not row:return False
            claim=self.social.claim(dict(thread=row["thread"],event=row["event"],part=row["part"]))
            if not claim["send"]:return True
            status="uncertain"
            try:
                parts=[]
                if claim.get("media"):
                    media=claim["media"]
                    parts.append(dict(type='voice',file=media[6:]) if re.fullmatch(r'voice:[0-9a-f]{64}',media) else dict(type="face",id=int(media[5:])) if re.fullmatch(r"face:[0-9]{1,5}",media) else dict(type="image",file=media))
                if claim.get("content"):parts.append(dict(type="text",text=claim["content"]))
                self.send(row["thread"],parts)
                status="submitted"
            except Exception:
                # No retry: HTTP failure can happen after QQ accepted the message.
                pass
            self.social.acknowledge(dict(event=row["event"],part=row["part"],status=status))
            if status=='submitted' and row['media'] and re.fullmatch(r'voice:[0-9a-f]{64}',row['media']):
                with self.w.lock:
                    retained=self.w.db.execute("SELECT 1 FROM social_outbox WHERE media=? AND status IN ('ready','claimed','uncertain') LIMIT 1",(row['media'],)).fetchone()
                if not retained:
                    try:(self.w.home/'voice'/(row['media'][6:]+'.wav')).unlink(missing_ok=True)
                    except OSError:pass
            if status=="uncertain":
                with self.w.lock,self.w.db:
                    self.w.db.execute("UPDATE social_outbox SET status='cancelled' WHERE event=? AND status='ready'",(row["event"],))
            self.next_send=self.w.clock()+2
            return True
        finally:self.delivery_lock.release()
