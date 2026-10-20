"""One runnable offline check; uses no API key and sends no QQ/WeChat messages."""
import base64
import datetime as dt
from email.message import Message
from contextlib import closing
import http.client
import hashlib
import hmac
import io
import json
import os
import sqlite3
from pathlib import Path
import tempfile
import threading
from unittest.mock import patch
import zipfile

from app import Handler, Server, acquire
from whale import Whale, configuration, decision, ROLE, LEGACY_ROLE, PROTOCOL, backup_database,complete,web_request
from phone import Phone, DownloadRedirect, download_url, action, UncertainAction
from social import Social


def rejects(action):
    try:
        action()
    except (ValueError, KeyError, TypeError, OSError):
        return
    raise AssertionError("Invalid action was accepted")


def run():
    config=configuration(dict(api_key='offline-test'))
    with patch('whale.urllib.request.build_opener') as opener,patch.dict(os.environ,{'DEEPSEEK_API_KEY':''}):
        opener.return_value.open.return_value=io.BytesIO(json.dumps(dict(choices=[dict(finish_reason='stop',message=dict(content='{"lines":["ok"]}'))],usage={})).encode())
        with patch('whale.time.monotonic',side_effect=[100,103.25]):
            result,usage=complete(config,[dict(role='user',content='test JSON')])
        assert result['lines']==['ok'] and usage['request_seconds']==3.25 and usage['reasoning_effort']=='none'
        assert json.loads(opener.return_value.open.call_args[0][0].data)['thinking']==dict(type='disabled')
        for effort,limit in (('low',4096),('high',8192),('max',16384)):
            opener.return_value.open.return_value=io.BytesIO(json.dumps(dict(choices=[dict(finish_reason='stop',message=dict(content='{"lines":["ok"]}',reasoning_content='private'))],usage={})).encode())
            assert complete(configuration(dict(config,reasoning_effort=effort)),[dict(role='user',content='test JSON')])[0]==dict(lines=['ok'])
            sent=json.loads(opener.return_value.open.call_args[0][0].data)
            assert sent['thinking']==dict(type='enabled') and sent['reasoning_effort']==effort and sent['max_tokens']==limit
            assert opener.return_value.open.call_args.kwargs['timeout']==180
        for bad in ('medium','',None,1):rejects(lambda:configuration(dict(reasoning_effort=bad)))
        opener.return_value.open.return_value=io.BytesIO(json.dumps(dict(choices=[dict(finish_reason='length',message=dict(content='{"lines":'))])).encode())
        rejects(lambda:complete(config,[dict(role='user',content='test JSON')]))
    def chunk_body(blob,**headers):
        handler=Handler.__new__(Handler)
        handler.headers=Message();handler.headers['Transfer-Encoding']='chunked'
        for key,value in headers.items():handler.headers[key.replace('_','-')]=value
        handler.rfile=io.BytesIO(blob)
        return handler.read_body(20,chunked=True)
    assert chunk_body(b'2\r\nab\r\n1\r\nc\r\n0\r\n\r\n')==b'abc'
    for blob in (b'15\r\n',b'2\r\na',b'x\r\n',b'1;x=y\r\na\r\n0\r\n\r\n',b'0\r\n\r\n',b'1\r\na\r\n0\r\nX: y\r\n\r\n'):
        rejects(lambda:chunk_body(blob))
    rejects(lambda:chunk_body(b'1\r\na\r\n0\r\n\r\n',Content_Length='1'))
    calls = []
    now = [dt.datetime(2026, 10, 19, 12, tzinfo=dt.timezone(dt.timedelta(hours=8))).timestamp()]

    def model(config, messages):
        calls.append(messages)
        return dict(lines=["记住啦，给你留一片海。", "今天还想聊什么？"],
                    memories=[dict(key="爱好", value="喜欢蓝色和大海")],
                    impression="喜欢分享生活", diary="他今天说喜欢海。"), {"total_tokens": 20}

    with tempfile.TemporaryDirectory() as temporary:
        legacy_home=Path(temporary)/"legacy-schema"
        legacy_home.mkdir()
        legacy_path=legacy_home/"memory.sqlite3"
        legacy=sqlite3.connect(legacy_path)
        legacy.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA wal_autocheckpoint=0;
            CREATE TABLE people(id TEXT PRIMARY KEY,name TEXT NOT NULL,impression TEXT NOT NULL DEFAULT '');
            CREATE TABLE messages(id INTEGER PRIMARY KEY,person TEXT,role TEXT,content TEXT,at REAL,event TEXT);
            CREATE TABLE events(id TEXT PRIMARY KEY,person TEXT,input TEXT,status TEXT,result TEXT,at REAL);
            CREATE TABLE memory(person TEXT,key TEXT,value TEXT,updated REAL,PRIMARY KEY(person,key));
            CREATE TABLE diary(id INTEGER PRIMARY KEY,at REAL,content TEXT,person TEXT);
            CREATE TABLE social_inbox(id TEXT PRIMARY KEY,thread TEXT,status TEXT,at REAL);
            INSERT INTO people VALUES('owner','你','原印象');
            INSERT INTO memory VALUES('owner','偏好','蓝色',1);
            INSERT INTO diary VALUES(1,1,'原日记','owner');
            INSERT INTO messages VALUES(1,'owner','user','本机旧聊天',1,'old-local');
            INSERT INTO messages VALUES(2,'owner','user','QQ旧聊天',2,'social:old-qq');
            INSERT INTO messages VALUES(3,'owner','user','无法还原会话的旧聊天',3,'social:unknown');
            INSERT INTO events VALUES('old-local','owner','本机旧聊天','done','{"lines":["旧回复"]}',1);
            INSERT INTO events VALUES('social:old-qq','owner','QQ旧聊天','done','{}',2);
            INSERT INTO social_inbox VALUES('old-qq','qq-thread','generated',2);
        """)
        original_connect=sqlite3.connect
        def deny_alter(*args,**kwargs):
            db=original_connect(*args,**kwargs)
            db.set_authorizer(lambda op,*_:sqlite3.SQLITE_DENY if op==sqlite3.SQLITE_ALTER_TABLE else sqlite3.SQLITE_OK)
            return db
        with patch("sqlite3.connect",deny_alter):
            try:Whale(legacy_home,model,lambda:now[0])
            except sqlite3.DatabaseError:pass
            else:raise AssertionError("Migration ignored failed ALTER TABLE")
        assert legacy.execute("PRAGMA user_version").fetchone()[0]==0
        assert "thread" not in {r[1] for r in legacy.execute("PRAGMA table_info(messages)")}
        assert legacy.execute("SELECT count(*) FROM messages").fetchone()[0]==3
        migrated=Whale(legacy_home,model,lambda:now[0])
        assert migrated.snapshot()["schema_version"]==3
        assert [r[0] for r in migrated.db.execute("SELECT thread FROM messages ORDER BY id")]==["local:owner","gui:qq-thread","legacy:owner"]
        assert migrated.db.execute("SELECT thread FROM events WHERE id='social:old-qq'").fetchone()[0]=="gui:qq-thread"
        assert [m["content"] for m in migrated.prompt("owner",False,"next")[1:]]==["本机旧聊天"]
        assert migrated.snapshot()["diary"][0]["content"]=="原日记"
        backups=list(legacy_home.glob("memory.before-schema-3.*.sqlite3"))
        assert len(backups)==2
        for backup in backups:
            with closing(original_connect(backup)) as db:
                assert db.execute("PRAGMA user_version").fetchone()[0]==0
                assert db.execute("SELECT count(*) FROM messages").fetchone()[0]==3
        migrated.close()
        migrated=Whale(legacy_home,model,lambda:now[0])
        assert len(list(legacy_home.glob("memory.before-schema-3.*.sqlite3")))==2
        migrated.close()
        # Prove update rollback can restore both the data and old schema, including uncheckpointed WAL.
        legacy.close()
        backup_database(backups[-1],legacy_path)
        legacy=original_connect(legacy_path)
        assert legacy.execute("PRAGMA user_version").fetchone()[0]==0
        assert "thread" not in {r[1] for r in legacy.execute("PRAGMA table_info(events)")}
        assert legacy.execute("SELECT value FROM memory").fetchone()[0]=="蓝色"
        legacy.execute("PRAGMA user_version=4")
        legacy.commit()
        rejects(lambda:Whale(legacy_home,model,lambda:now[0]))
        assert legacy.execute("PRAGMA user_version").fetchone()[0]==4
        legacy.close()
        print("PASS: WAL-aware schema backup, transactional migration rollback, legacy thread recovery, restart, restore and newer-schema rejection")

        # A version-1 database (thread columns, no social_jobs) upgrades to version 3 with its own backup.
        v1_home=Path(temporary)/"v1-schema"
        v1_home.mkdir()
        v1_path=v1_home/"memory.sqlite3"
        v1=sqlite3.connect(v1_path)
        v1.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE people(id TEXT PRIMARY KEY,name TEXT NOT NULL,impression TEXT NOT NULL DEFAULT '');
            CREATE TABLE messages(id INTEGER PRIMARY KEY,person TEXT,role TEXT NOT NULL,content TEXT NOT NULL,at REAL NOT NULL,event TEXT NOT NULL,thread TEXT NOT NULL DEFAULT '');
            CREATE TABLE events(id TEXT PRIMARY KEY,person TEXT,input TEXT,status TEXT,result TEXT,at REAL,thread TEXT NOT NULL DEFAULT '');
            CREATE TABLE memory(person TEXT,key TEXT,value TEXT NOT NULL,updated REAL NOT NULL,PRIMARY KEY(person,key));
            CREATE TABLE diary(id INTEGER PRIMARY KEY,at REAL NOT NULL,content TEXT NOT NULL,person TEXT);
            CREATE INDEX messages_thread_history ON messages(thread,person,id);
            INSERT INTO people VALUES('owner','你','原印象');
            INSERT INTO memory VALUES('owner','偏好','蓝色',1);
            INSERT INTO diary VALUES(1,1,'升级前的日记','owner');
            INSERT INTO messages VALUES(1,'owner','user','升级前聊天',1,'local:evt','local:owner');
        """)
        v1.execute("PRAGMA user_version=1")
        v1.commit()
        v1.close()
        upgraded=Whale(v1_home,model,lambda:now[0])
        assert upgraded.snapshot()["schema_version"]==3
        assert upgraded.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='social_jobs'").fetchone()
        assert upgraded.db.execute("SELECT content FROM messages WHERE id=1").fetchone()[0]=="升级前聊天"
        assert upgraded.db.execute("SELECT value FROM memory").fetchone()[0]=="蓝色"
        assert upgraded.snapshot()["diary"][0]["content"]=="升级前的日记"
        assert len(list(v1_home.glob("memory.before-schema-3.*.sqlite3")))==1
        upgraded.close()
        upgraded=Whale(v1_home,model,lambda:now[0])
        assert len(list(v1_home.glob("memory.before-schema-3.*.sqlite3")))==1
        upgraded.close()
        print("PASS: version-1 database upgrades to version 3, keeps rows and backs up before adding social_jobs")

        thread_calls=[]
        def thread_model(config,messages):
            thread_calls.append(messages)
            return dict(lines=["已收到"],memories=[dict(key="共同偏好",value="海蓝色")]),{}
        separated=Whale(Path(temporary)/"thread-check",thread_model,lambda:now[0])
        separated.chat("只在QQ说过的话","qq-1",thread="gui:qq-owner")
        separated.chat("只在微信说过的话","wx-1",thread="gui:wx-owner")
        assert "只在QQ说过的话" not in str(thread_calls[-1][1:])
        assert "海蓝色" in thread_calls[-1][0]["content"]
        separated.chat("继续QQ聊天","qq-2",thread="gui:qq-owner")
        assert "只在QQ说过的话" in str(thread_calls[-1][1:]) and "只在微信说过的话" not in str(thread_calls[-1][1:])
        rejects(lambda:separated.chat("只在QQ说过的话","qq-1",thread="gui:wx-owner"))
        separated.chat("本机管理聊天","local-1")
        assert "只在QQ说过的话" not in str(thread_calls[-1][1:])
        assert [r["content"] for r in separated.snapshot()["messages"]]==["本机管理聊天","已收到"]
        separated.close()
        separated=Whale(Path(temporary)/"thread-check",thread_model,lambda:now[0])
        separated.chat("微信重启后继续","wx-2",thread="gui:wx-owner")
        assert "只在微信说过的话" in str(thread_calls[-1][1:]) and "继续QQ聊天" not in str(thread_calls[-1][1:])
        separated.close()
        print("PASS: shared person memory, isolated QQ/WeChat/local histories, cross-thread duplicate rejection and restart persistence")

        diagnostics=[]
        def diagnostic_model(config,messages):
            diagnostics.append(messages)
            return dict(lines=["连接成功"],memories=[dict(key="不应保存",value="连接测试")],diary="不应成为日记",impression="不应改变印象"),{"total_tokens":2}
        diagnostic=Whale(Path(temporary)/"diagnostic-check",diagnostic_model,lambda:now[0])
        diagnostic.save_config({"daily_calls":2})
        assert diagnostic.test_connection()["ok"]
        snapshot=diagnostic.snapshot()
        assert not snapshot["messages"] and not snapshot["memories"] and not snapshot["diary"] and not snapshot["people"][0]["impression"]
        assert snapshot["calls_today"]==1 and snapshot["model_status"]["ok"]
        def failed_diagnostic(*args):raise ValueError("测试连接失败")
        diagnostic.provider=failed_diagnostic
        rejects(diagnostic.test_connection)
        assert diagnostic.snapshot()["calls_today"]==2 and not diagnostic.snapshot()["model_status"]["ok"]
        rejects(lambda:diagnostic.chat("超过同一调用额度","shared-diagnostic-limit"))
        assert not diagnostic.snapshot()["messages"]
        diagnostic.set_setting("paused",True)
        rejects(diagnostic.test_connection)
        assert diagnostic.snapshot()["calls_today"]==2
        diagnostic.close()
        print("PASS: isolated model connection check, no personal context or memory writes, shared quota, failure accounting and pause")
        unlimited_home=Path(temporary)/"unlimited-check"
        unlimited=Whale(unlimited_home,diagnostic_model,lambda:now[0])
        assert unlimited.public_config()["daily_calls"]==0
        for invalid in (-1,True,None,0.5):
            rejects(lambda:configuration({"daily_calls":invalid}))
        with unlimited.lock,unlimited.db:
            unlimited.db.executemany("INSERT INTO calls(at) VALUES(?)",[(now[0],)]*1001)
        unlimited.chat("不限每日次数","unlimited-chat")
        unlimited.test_connection()
        assert unlimited.snapshot()["calls_today"]==1003
        unlimited.save_config({"daily_calls":1})
        rejects(unlimited.test_connection)
        unlimited.save_config({"daily_calls":0})
        unlimited.close()
        unlimited=Whale(unlimited_home,diagnostic_model,lambda:now[0])
        unlimited.test_connection()
        assert unlimited.public_config()["daily_calls"]==0 and unlimited.snapshot()["calls_today"]==1004
        unlimited.close()
        print("PASS: unlimited daily calls beyond 1000, accounting, optional cap, validation and restart persistence")
        role_home=Path(temporary)/"role-check"
        role_home.mkdir()
        (role_home/"role.md").write_text(LEGACY_ROLE,encoding="utf-8")
        role_whale=Whale(role_home,model,lambda:now[0])
        assert (role_home/"role.md").read_text("utf-8")==ROLE
        assert (role_home/"role.before-personality.md").read_text("utf-8")==LEGACY_ROLE
        assert "只输出JSON" not in ROLE and "你会嘴硬" in ROLE
        assert PROTOCOL in role_whale.prompt("owner",False,"role-check")[0]["content"]
        role_whale.close()
        custom="我自己的角色卡，不写程序格式。"
        (role_home/"role.md").write_text(custom,encoding="utf-8")
        role_whale=Whale(role_home,model,lambda:now[0])
        assert (role_home/"role.md").read_text("utf-8")==custom
        assert custom in role_whale.prompt("owner",False,"role-check")[0]["content"]
        assert PROTOCOL in role_whale.prompt("owner",False,"role-check")[0]["content"]
        assert (role_home/"role.before-personality.md").read_text("utf-8")==LEGACY_ROLE
        role_whale.close()
        print("PASS: tutorial personality, default-role backup/migration, custom-role preservation and independent output protocol")
        home = Path(temporary) / "whale"
        whale = Whale(home, model, lambda: now[0])
        first = whale.chat("我喜欢蓝色和大海", "first")
        assert len(first["lines"]) == 2
        assert whale.chat("我喜欢蓝色和大海", "first") == first and len(calls) == 1
        rejects(lambda: whale.chat("different input", "first"))
        assert "api_key" not in whale.snapshot()["config"]
        whale.close()
        whale = Whale(home, model, lambda: now[0] + 86400)
        whale.chat("你还记得我喜欢什么吗？", "next-day")
        assert "喜欢蓝色和大海" in calls[-1][0]["content"]
        assert "seconds_since_last_user_message" in calls[-1][0]["content"]
        assert '"seconds_since_last_user_message": 86400' in calls[-1][0]["content"]
        assert whale.snapshot()["people"][0]["impression"] == "喜欢分享生活"
        diary = [json.loads(line) for line in (home / "diary.jsonl").read_text("utf-8").splitlines()]
        assert len(diary) == 2 and diary[0]["content"] == "他今天说喜欢海。"
        print("PASS: conversation, deduplication, next-day memory, time, impression, durable JSONL diary")

        with whale.lock, whale.db:
            whale.db.execute("INSERT INTO people(id,name) VALUES('other','同名用户')")
        whale.chat("你好", "other-1", "other")
        assert "喜欢蓝色和大海" not in calls[-1][0]["content"]
        rejects(lambda: whale.chat("冒充用户", "unknown-1", "unbound"))
        whale.set_setting("paused", True)
        rejects(lambda: whale.chat("你好", "paused"))
        count = len(calls)
        whale.save_config({"proactive": True})
        assert whale.tick() is None and len(calls) == count
        whale.set_setting("paused", False)

        def pause_during_request(config, messages):
            whale.set_setting("paused", True)
            return model(config, messages)

        whale.provider = pause_during_request
        before = len(whale.snapshot()["messages"])
        rejects(lambda: whale.chat("测试暂停", "inflight-pause"))
        assert len(whale.snapshot()["messages"]) == before + 1
        whale.set_setting("paused", False)
        whale.provider = model
        rejects(lambda: whale.chat("测试暂停", "inflight-pause"))
        print("PASS: person isolation, unknown-person rejection, pause and in-flight response suppression")

        whale.clock = lambda: now[0]
        whale.save_config({"daily_calls": 1})
        rejects(lambda: whale.chat("达到限额", "limit"))
        whale.save_config({"daily_calls": 60, "probability": 1, "proactive": True})
        whale.clock = lambda: now[0] + 86400 + 3600
        whale.set_setting("next_check", 0)
        assert whale.tick() is not None
        count = len(calls)
        assert whale.tick() is None and len(calls) == count
        whale.clock = lambda: now[0] + 11 * 3600
        whale.set_setting("next_check", 0)
        assert whale.tick() is None  # 23:00, quiet period spans midnight.
        whale.clock = lambda: now[0] + 18 * 3600
        assert whale.tick() is None  # 06:00.
        print("PASS: daily call cap, persisted random scheduling, proactive response, overnight quiet hours")

        whale.clock = lambda: now[0] + 2 * 86400
        for i in range(4):
            whale.chat("日记限额", "diary-limit-" + str(i))
        assert len([d for d in whale.snapshot()["diary"] if d["at"] == whale.clock()]) == 3
        rejects(lambda: decision(dict(lines=["x" * 241])))
        rejects(lambda: decision(dict(lines=[])))
        rejects(lambda: configuration({"base_url": "http://unsafe.test"}))
        rejects(lambda: configuration({"base_url": "https://name:secret@example.com"}))
        rejects(lambda: configuration({"probability": float("nan")}))
        whale.save_config({"api_key": "test-secret-only"})
        rejects(lambda: whale.save_config({"base_url": "https://different.test"}))
        assert "test-secret-only" not in json.dumps(whale.snapshot())
        rejects(lambda: whale.collect(b"not an image", "unsafe"))
        image = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6S0kAAAAASUVORK5CYII=")
        digest = whale.collect(image, "一小片白色")
        assert whale.collect(image, "重新收藏") == digest and len(whale.snapshot()["album"]) == 1
        print("PASS: diary limit, model-output validation, secret redaction, HTTPS and album boundary checks")

        from whale import pictures, strip_images
        from social import fetch_image, image_type, image_url
        rejects(lambda: pictures([("image/png", b"not a png")]))
        rejects(lambda: pictures([("image/jpeg", image)]))
        rejects(lambda: pictures([("image/png", image)] * 5))
        verdicts=[]
        def seeing_model(config, messages):
            verdicts.append(messages)
            return dict(lines=["看到了"]), {}
        whale.provider = seeing_model
        whale.chat("看看这片海", "vision-1", images=[("image/png", image)])
        turn = verdicts[-1][-1]
        assert turn["role"] == "user" and isinstance(turn["content"], list)
        assert turn["content"][0] == dict(type="text", text="看看这片海")
        assert turn["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
        assert "data:image" not in json.dumps(whale.snapshot()["messages"])
        assert strip_images([dict(role="user", content=[dict(type="text", text="海"), dict(type="image_url", image_url={})])])[0]["content"] == "海"
        whale.provider = model
        for bad in ("http://example.com/a.png", "https://127.0.0.1/a.png", "https://localhost/a.png",
                    "https://user:pw@example.com/a.png", "https://example.com/a.png#part"):
            rejects(lambda: image_url(bad))
        rejects(lambda: decision(dict(lines=["x"], photo="../etc/passwd")))
        whale.provider = lambda *_: (dict(lines=["送你一张"], photo=digest[:16]), {})
        picked = whale.chat("挑一张", "photo-1")
        assert picked["photo"] == digest
        whale.provider = lambda *_: (dict(lines=["收下啦"], collect="这片白很安静"), {})
        second = image + b"x"
        whale.chat("再收一张", "collect-1", images=[("image/png", second)])
        assert len(whale.snapshot()["album"]) == 2
        whale.chat("再收一次", "collect-2", images=[("image/png", second)])
        assert len(whale.snapshot()["album"]) == 2
        whale.provider = lambda *_: (dict(lines=["不是给你的"], photo=digest[:16]), {})
        assert "photo" not in whale.chat("看看", "photo-other", "other")
        whale.provider = model
        assert image_type(image) == "image/png" and image_type(b"<html>") is None
        with patch("social.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]):
            assert image_url("https://example.com/a.png") == "https://example.com/a.png"
            with patch("social.ImageConnection") as connection:
                response=connection.return_value.getresponse.return_value
                response.status=200;response.read.return_value=image
                assert fetch_image("https://example.com/a.png") == ("image/png", image)
                connection.assert_called_with('example.com','93.184.216.34')
                connection.return_value.request.assert_called_with('GET','/a.png',headers={'Accept':'image/*'})
                response.read.return_value=b"<html>not an image</html>"
                rejects(lambda: fetch_image("https://example.com/a.png"))
                response.status=302
                rejects(lambda: fetch_image("https://example.com/a.png"))
                for failure in (TimeoutError('private-network-detail'),http.client.IncompleteRead(b'private-data',10)):
                    connection.return_value.request.side_effect=failure
                    before=connection.return_value.request.call_count
                    try:fetch_image("https://example.com/a.png")
                    except ValueError as error:assert '下载失败或超时' in str(error) and 'private' not in str(error)
                    else:raise AssertionError('Failed network read was not classified')
                    assert connection.return_value.request.call_count==before+1
                    assert connection.return_value.close.called
        from social import ImageConnection
        with patch('social.socket.create_connection') as connect:
            connect.return_value.getpeername.return_value=('10.0.0.1',443)
            conn=ImageConnection('example.com','93.184.216.34')
            with patch.object(conn._context,'wrap_socket') as tls:
                rejects(conn.connect)
                assert not tls.called
            connect.return_value.close.assert_called_once()
        reject_private = lambda: image_url("https://example.com/a.png")
        with patch("social.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("10.0.0.5", 443))]):
            rejects(reject_private)
            rejects(lambda:image_url('https://multimedia.nt.qq.com.cn/download'))
        with patch('social.socket.getaddrinfo',return_value=[(2,1,6,'',('198.18.2.55',443))]),patch('social.urllib.request.build_opener') as dns:
            dns.return_value.open.side_effect=lambda *a,**k:io.BytesIO(json.dumps(dict(Status=0,Answer=[dict(type=1,data='127.0.0.1')])).encode())
            rejects(reject_private)
            rejects(lambda:image_url('https://multimedia.nt.qq.com.cn.evil.test/a'))
            assert image_url('https://multimedia.nt.qq.com.cn/download').startswith('https://')
            dns.return_value.open.side_effect=lambda *a,**k:io.BytesIO(json.dumps(dict(Status=0,Answer=[dict(type=1,data='93.184.216.34')])).encode())
            from social import image_target
            assert image_target('https://example.com/a.png')[1]==['93.184.216.34']
        with patch('social.socket.create_connection') as connect:
            connect.return_value.getpeername.return_value=('198.18.2.55',443)
            conn=ImageConnection('multimedia.nt.qq.com.cn','198.18.2.55')
            with patch.object(conn._context,'wrap_socket') as tls:
                conn.connect()
                tls.assert_called_once_with(connect.return_value,server_hostname='multimedia.nt.qq.com.cn')
        print("PASS: image blocks reach the model while history stays text, mime/size limits, HTTPS-only and private-network refusal")

        saved = whale.provider
        whale.provider = lambda *_: (dict(lines=["x" * 241], memories=[dict(key="坏数据", value="不能保存")]), {})
        rejects(lambda: whale.chat("格式错误", "bad-output"))
        assert not any(m["key"] == "坏数据" for m in whale.snapshot()["memories"])
        diagnostic=whale.setting("last_model_error")
        assert diagnostic['stage']=='decision' and diagnostic['line_lengths']==[241] and not diagnostic['action_requested']
        assert '不能保存' not in json.dumps(diagnostic,ensure_ascii=False) and 'x'*20 not in json.dumps(diagnostic)
        whale.provider = saved
        whale.db.execute("INSERT INTO events(id,person,input,status,result,at,thread) VALUES('crash','owner','incomplete','working',NULL,?,'local:owner')", (now[0],))
        whale.db.commit()
        whale.close()
        whale = Whale(home, model, lambda: now[0])
        rejects(lambda: whale.chat("incomplete", "crash"))
        assert whale.db.execute("SELECT status FROM events WHERE id='crash'").fetchone()[0] == "interrupted"
        print("PASS: malformed decisions do not partially commit and interrupted requests are never replayed")

        with Server(("127.0.0.1", 0), Handler) as server:
            server.whale, server.token = whale, "local-test-token"
            server.social = Social(whale)
            rejects(lambda: Server(("127.0.0.1", server.server_port), Handler))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            def request(path, body=None, auth=True, origin=None, host=None, token=None,qq_sign=False,chunks=False):
                for attempt in range(2):
                    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
                    try:
                        headers = {"Content-Type": "application/json"}
                        if auth:
                            headers["Authorization"] = "Bearer " + (token or "local-test-token")
                        if origin:
                            headers["Origin"] = origin
                        if host:
                            headers["Host"] = host
                        if qq_sign:headers['X-Signature']='sha1='+hmac.new(server.social.qq.token.encode(),json.dumps(body).encode(),hashlib.sha1).hexdigest()
                        connection.request("POST" if body is not None else "GET", path,
                                           None if body is None else iter([json.dumps(body).encode()[:9],json.dumps(body).encode()[9:]]) if chunks else json.dumps(body), headers,encode_chunked=chunks)
                        response = connection.getresponse()
                        data = response.read()
                        return response.status, data
                    except (ConnectionAbortedError,ConnectionResetError):
                        if attempt: raise
                    finally:
                        connection.close()
            try:
                assert request("/api/state", auth=False)[0] == 401
                assert request("/api/state", origin="https://evil.test")[0] == 403
                assert request("/api/state", host="evil.test")[0] == 403
                status, raw = request("/api/state")
                assert status == 200 and b"test-secret-only" not in raw
                assert request("/api/config", {"daily_calls": "oops"})[0] == 400
                assert request("/api/test-model",{},auth=False)[0]==401
                assert request("/api/test-model",{"message":"unexpected"})[0]==400
                assert request("/api/social-policy",{},auth=False)[0]==401
                assert request("/api/social-policy",{})[0]==400
                assert request("/api/pause", {"paused": True})[0] == 200
                assert request("/api/chat", {"message": "暂停", "event_id": "http-paused"})[0] == 400
                assert request("/api/pause", {"paused": False})[0] == 200
                image_status, _ = request("/api/chat", {"message": "看看这个", "event_id": "http-image", "image": base64.b64encode(image).decode()})
                assert image_status == 200 and isinstance(calls[-1][-1]["content"], list)
                assert request("/api/chat", {"message": "坏图", "event_id": "http-bad-image", "image": "aGVsbG8="})[0] == 400
                before_probe=whale.snapshot()
                assert request('/api/test-model',dict(image=base64.b64encode(image).decode()))[0]==200
                assert isinstance(calls[-1][-1]['content'],list) and len(calls[-1])==2
                assert whale.snapshot()['messages']==before_probe['messages'] and whale.snapshot()['memories']==before_probe['memories']
                assert request('/api/test-model',dict(image='aGVsbG8='))[0]==400
                assert request("/album/" + whale.snapshot()["album"][0]["file"])[1] == image
                assert request("/album/../config.json")[0] == 404
                assert b"local-test-token" not in request("/")[1]
                assert b"export class Humanizer" in request("/vendor/ai-companion/humanize.js")[1]
                assert request("/vendor/ai-companion/humanize.js",host="evil.test")[0]==403
                assert request("/vendor/ai-companion/../../../config.json")[0]==404
                assert request("/bridge/status", {"connected": True, "sdk": 25, "version": "0.1"})[0] == 401
                accepted_calls = len(calls)
                accepted = request("/bridge/event", dict(thread="5" * 64, event="6" * 64, package="com.tencent.mm",
                                                         title="HTTP联系人", message="你好", replyable=True, fresh=True),
                                   token=server.social.token)
                assert accepted[0] == 202 and json.loads(accepted[1]) == dict(status="disabled", parts=[])
                assert len(calls) == accepted_calls
                assert request("/api/social")[0] == 200
                whale.set_setting("qq_bot","123456")
                heartbeat=dict(self_id=123456,post_type="meta_event")
                assert request("/bridge/qq/event",heartbeat,token=server.social.token)[0]==401
                assert request("/bridge/qq/event",heartbeat,qq_sign=True)[0]==202
                assert request("/bridge/qq/event",heartbeat,qq_sign=True,chunks=True)[0]==202
                assert request("/bridge/qq/event",heartbeat,chunks=True)[0]==401
                assert request("/bridge/qq/event",dict(heartbeat,self_id=654321),qq_sign=True)[0]==400
                whale.set_setting("qq_bot",None)
            finally:
                server.shutdown()
                thread.join(3)
        whale.set_setting("paused", False)
        whale.clock = lambda: now[0] + 3 * 86400
        social = Social(whale)
        thread_id, event = "a" * 64, "b" * 64
        packet = dict(thread=thread_id, event=event, package="com.tencent.mm", title="测试联系人",
                      message="你好", replyable=True, fresh=True)
        before = len(calls)
        assert social.receive(packet)["status"] == "disabled" and len(calls) == before
        assert not social.process_one() and len(calls) == before
        assert not whale.db.execute("SELECT 1 FROM messages WHERE event=?", ("social:" + event,)).fetchone()
        social.configure(dict(thread=thread_id, kind="private", owner=True, enabled=True, trigger=""))
        assert social.receive(dict(packet, event="c" * 64, fresh=False))["status"] == "baseline"
        queued = social.receive(dict(packet, event="d" * 64))
        assert queued == dict(status="queued", parts=[]) and len(calls) == before
        assert social.process_one() and len(calls) == before + 1
        generated = social.pending("d" * 64)
        assert generated == dict(status="generated", parts=[0, 1])
        assert '"thread": "gui:'+thread_id+'"' in calls[-1][0]["content"]
        assert all(r[0]=="gui:"+thread_id for r in whale.db.execute("SELECT thread FROM messages WHERE event=?",("social:"+"d"*64,)))
        assert social.receive(dict(packet, event="d" * 64)) == generated and len(calls) == before + 1
        claim = dict(thread=thread_id, event="d" * 64, part=0)
        assert social.claim(claim)["send"]
        assert not social.claim(claim)["send"]
        social.acknowledge(dict(event="d" * 64, part=0, status="submitted"))
        whale.set_setting("paused", True)
        assert not social.claim(dict(claim, part=1))["send"]
        whale.set_setting("paused", False)
        assert social.receive(dict(packet, event="e" * 64, replyable=False))["status"] == "no_reply_channel"
        social.configure(dict(thread=thread_id, kind="group", owner=False, enabled=True, trigger="小鲸鱼"))
        rejects(lambda: social.configure(dict(thread=thread_id, kind="group", owner=True, enabled=True, trigger="小鲸鱼")))
        assert social.receive(dict(packet, event="f" * 64))["status"] == "silent"
        assert social.receive(dict(packet, event="1" * 64, message="小鲸鱼你好"))["status"] == "queued"
        assert social.process_one()
        assert '"person": "social:' in calls[-1][0]["content"]
        assert "喜欢蓝色和大海" not in calls[-1][0]["content"]
        social.configure(dict(thread=thread_id, kind="group", owner=False, enabled=False, trigger="小鲸鱼"))
        assert not social.claim(dict(thread=thread_id, event="1" * 64, part=0))["send"]
        rejects(lambda: social.receive(dict(packet, package="com.bank.app")))
        rejects(lambda: social.receive(dict(packet, title="另一个同名会话")))
        assert social.token not in str(social.snapshot())
        social.configure(dict(thread=thread_id, kind="private", owner=True, enabled=True, trigger=""))
        assert social.receive(dict(packet, event="9" * 64))["status"] == "queued"
        whale.close()
        whale = Whale(home, model, lambda: now[0] + 3 * 86400)
        restored = Social(whale)
        assert restored.token == social.token and restored.snapshot()["deliveries"]
        assert restored.pending("9" * 64)["status"] == "queued"
        before = len(calls)
        assert restored.process_one() and len(calls) == before + 1
        assert restored.pending("9" * 64)["status"] == "generated"
        assert not restored.claim(claim)["send"]
        whale.close()
        print("PASS: scoped social authorization, baseline silence, durable inbox/worker queue, deduplication, one-time claim, pause, group isolation and restart-kept reply")
        rate_home=Path(temporary)/"rate-check"
        rate_now=[now[0]]
        rate_whale=Whale(rate_home,model,lambda:rate_now[0])
        rate=Social(rate_whale)
        limits=dict(private_per_minute=1,group_per_minute=1,total_per_minute=2,total_per_day=500,groups_silent=False)
        rate.save_policy(limits)
        rejects(lambda:rate.save_policy(dict(limits,private_per_minute=True)))
        rejects(lambda:rate.save_policy(dict(limits,group_per_minute=6)))
        thread_a="2"*64;thread_b="3"*64
        def packet(thread,event):return dict(thread=thread,event=event,package="com.tencent.mobileqq",title="限流测试",message="测试",replyable=True,fresh=True)
        for tid in (thread_a,thread_b):
            rate.receive(packet(tid,tid))
            rate.configure(dict(thread=tid,kind="private",owner=False,enabled=True,trigger=""))
        event_a="4"*64;event_b="5"*64
        rate.receive(packet(thread_a,event_a))
        assert rate.process_one()
        assert rate.claim(dict(thread=thread_a,event=event_a,part=0))["send"]
        assert not rate.claim(dict(thread=thread_a,event=event_a,part=1))["send"]
        assert not rate.pending(event_a)["parts"]
        rate.acknowledge(dict(event=event_a,part=0,status="send_failed"))
        rate_whale.close()
        rate_whale=Whale(rate_home,model,lambda:rate_now[0]);rate=Social(rate_whale)
        rate.receive(packet(thread_a,event_b))
        assert rate.process_one()
        assert not rate.claim(dict(thread=thread_a,event=event_b,part=0))["send"],"Restart or failed send erased quota"
        rate_now[0]-=10
        event_b="6"*64;rate.receive(packet(thread_a,event_b))
        assert rate.process_one()
        assert not rate.claim(dict(thread=thread_a,event=event_b,part=0))["send"],"Clock rollback bypassed quota"
        rate_now[0]+=71
        event_b="7"*64;rate.receive(packet(thread_a,event_b))
        assert rate.process_one()
        assert rate.claim(dict(thread=thread_a,event=event_b,part=0))["send"]
        event_c="8"*64;rate.receive(packet(thread_b,event_c))
        assert rate.process_one()
        assert rate.claim(dict(thread=thread_b,event=event_c,part=0))["send"]
        assert not rate.claim(dict(thread=thread_b,event=event_c,part=1))["send"]
        thread_c="b"*64
        rate.receive(packet(thread_c,"c"*64))
        rate.configure(dict(thread=thread_c,kind="private",owner=False,enabled=True,trigger=""))
        rate.receive(packet(thread_c,"d"*64))
        assert rate.process_one()
        assert not rate.claim(dict(thread=thread_c,event="d"*64,part=0))["send"],"Global quota failed across three threads"
        rate.acknowledge(dict(event=event_c,part=0,status="submitted"))
        assert rate.snapshot()["last_submission"]==rate_now[0]
        rate.configure(dict(thread=thread_b,kind="group",owner=False,enabled=True,trigger="测试"))
        rate.save_policy(dict(limits,groups_silent=True))
        before=rate_whale.snapshot()["calls_today"]
        assert rate.receive(packet(thread_b,"9"*64))["status"]=="silent"
        assert rate_whale.snapshot()["calls_today"]==before
        rate.save_policy(limits)
        def mute_during_model(config,messages):
            rate.save_policy(dict(limits,groups_silent=True))
            return model(config,messages)
        rate_whale.provider=mute_during_model
        assert rate.receive(packet(thread_b,"a"*64))["status"]=="queued"
        assert rate.process_one()
        assert rate.pending("a"*64)["status"]=="cancelled"
        rate_whale.provider=model
        rate_now[0]+=61
        rate.save_policy(dict(limits,private_per_minute=3,total_per_minute=4))
        rate.receive(packet(thread_b,"e"*64))
        assert rate.process_one()
        assert rate.claim(dict(thread=thread_b,event="e"*64,part=0))["send"]
        assert not rate.claim(dict(thread=thread_b,event="e"*64,part=1))["send"],"Group limit incorrectly used private quota"
        rate_whale.close()
        print("PASS: persistent per-thread/global quotas, failed-send accounting, clock rollback, terminal throttling and in-flight group silence")
        lock = acquire(home)
        rejects(lambda: acquire(home))
        lock.close()
        print("PASS: real local HTTP authentication, cross-site/Host rejection, settings, pause and file isolation")
        config = dict(serial="127.0.0.1:5666", android_id="1234567890abcdef", sdk=25,
                      fingerprint="verified-vm", allow_packages=["com.android.browser"])
        rejects(lambda: Phone(dict(config, serial="USB-PHONE")))
        rejects(lambda: Phone(dict(config, serial="10.0.0.1:5555")))
        phone = Phone(config, permitted=lambda: False)
        rejects(phone.verify)
        phone = Phone(config)
        phone.size = lambda: (720, 1280)
        rejects(lambda: phone.point(720, 1))
        rejects(lambda: phone.point(True, 1))
        phone.point(719, 1279)
        phone.shell = lambda *args: "different-device"
        rejects(phone.verify)
        print("PASS: VM-only transport, paired identity, pause and coordinate validation")
        rejects(lambda: Phone(dict(config, allow_packages="com.android.browser")))
        rejects(lambda: Phone(dict(config, app_scope="anything")))
        whole = Phone(dict(config, app_scope="whole_vm", allow_packages=[]))
        commands=[]
        whole.verify=lambda: None
        whole.shell=lambda *args: commands.append(args) or ""
        whole.open("org.example.newapp")
        assert commands[-1][2]=="org.example.newapp"
        rejects(lambda: whole.open("org.example;reboot"))
        limited=Phone(config)
        limited.verify=lambda: None
        rejects(lambda: limited.open("org.example.newapp"))
        rejects(lambda: limited.download("https://example.test/app.apk",home/"downloads"))
        for address in ("http://example.test/app.apk", "https://user:pass@example.test/a", "https://example.test/a#frag"):
            rejects(lambda: download_url(address))
        rejects(lambda: DownloadRedirect().redirect_request(None,None,302,"",{},"http://example.test/app.apk"))
        package=io.BytesIO()
        with zipfile.ZipFile(package,"w") as archive:
            archive.writestr("AndroidManifest.xml",b"manifest-for-offline-check")
        payload=package.getvalue()
        class Response(io.BytesIO):
            headers={"Content-Length":str(len(payload))}
        class Opener:
            def open(self,*args,**kwargs):
                return Response(payload)
        folder=home/"downloads"
        with patch("phone.urllib.request.build_opener",return_value=Opener()):
            downloaded=whole.download("https://example.test/app.apk",folder)
            assert Path(downloaded["path"]).read_bytes()==payload
            assert downloaded["bytes"]==len(payload) and not list(folder.glob("*.part"))
            whole.permitted=lambda: False
            rejects(lambda: whole.download("https://example.test/app.apk",folder))
            assert not list(folder.glob("*.part"))
            whole.permitted=lambda: True
            Response.headers={"Content-Length":str(len(payload)+1)}
            rejects(lambda: whole.download("https://example.test/app.apk",folder))
            assert not list(folder.glob("*.part"))
        whole.command=lambda *args,**kwargs: commands.append((args,kwargs)) or "Success"
        assert whole.install(downloaded["path"])["installed"]
        assert commands[-1][0][0:2]==("install","-r")
        rejects(lambda: limited.install(downloaded["path"]))
        invalid=home/"invalid.apk"
        invalid.write_bytes(b"not an apk")
        rejects(lambda: whole.install(invalid))
        print("PASS: whole-VM app scope, package validation, HTTPS-only downloads, atomic cleanup, pause and install boundaries")
        notebook_whale=Whale(home/"notebook-core",model,lambda:now[0])
        notebook_whale.chat("记录今天", "notebook-first")
        (notebook_whale.home/"device.json").write_text(json.dumps(dict(config,app_scope="whole_vm")),encoding="utf-8")
        exports={"memory":[dict(id="manual-note",title="称呼",body="希望叫我小海",source="manual",deleted=0,updated=int(now[0]*1000))],"diary":[]}
        published=[]
        class NotebookPhone:
            def __init__(self,*args,**kwargs):pass
            def connect(self):pass
            def notebook(self,kind,record=None,after=""):
                if record is not None:
                    published.append((kind,record));return record
                entries=[] if after else exports[kind]
                return dict(entries=entries,after=entries[-1]["id"] if entries else after)
        with patch("whale.Phone",NotebookPhone):
            notebook_whale.sync_notebooks()
            assert {k for k,r in published}=={"memory","diary"}
            count=len(published)
            notebook_whale.sync_notebooks()
            assert len(published)==count,"Unchanged entries were republished"
            notebook_whale.chat("你会怎么称呼我", "notebook-second")
            assert "希望叫我小海" in calls[-1][0]["content"]
            with notebook_whale.lock,notebook_whale.db:
                notebook_whale.db.execute("INSERT INTO people(id,name) VALUES('group-notebook','测试群')")
            notebook_whale.chat("群内问题", "notebook-group",person="group-notebook")
            assert "希望叫我小海" not in calls[-1][0]["content"]
            deleted_id="m:"+hashlib.sha256("owner\n爱好".encode()).hexdigest()
            exports["memory"]=[dict(id=deleted_id,title="爱好",body="已删除",source="manual",deleted=1,updated=int(now[0]*1000))]
            notebook_whale.sync_notebooks()
            notebook_whale.chat("忘掉那条后再聊天", "notebook-deleted")
            prompt=calls[-1][0]["content"]
            assert '"key": "爱好"' not in prompt
            with notebook_whale.lock,notebook_whale.db:
                notebook_whale.db.execute("INSERT OR REPLACE INTO memory VALUES('owner','旧主题','旧内容',?)",(now[0]-100,))
                notebook_whale.db.executemany("INSERT OR REPLACE INTO memory VALUES('owner',?,'占位',?)",[("主题"+str(i),now[0]) for i in range(35)])
                notebook_whale.db.execute("INSERT INTO notebook_entries VALUES('memory',?,'旧主题','刚纠正的旧记忆','manual',0,?)",("m:"+hashlib.sha256("owner\n旧主题".encode()).hexdigest(),int(now[0]*1000)))
            notebook_whale.chat("纠正以前的事", "notebook-old-edit")
            assert "刚纠正的旧记忆" in calls[-1][0]["content"]
            notebook_whale.set_setting("paused",True)
            count=len(published)
            notebook_whale.sync_notebooks()
            assert len(published)==count
        notebook_whale.close()
        print("PASS: diary/memory app publishing, unchanged-entry deduplication, edited memory context, deletion, group isolation and pause")
        phone=Phone(dict(config,app_scope="whole_vm"))
        phone.verify=lambda: None
        phone.size=lambda:(720,1280)
        ui=[('<hierarchy><node package="org.example.app" class="android.widget.Button" text="打开" content-desc="" resource-id="open" clickable="true" enabled="true" focused="false" password="false" bounds="[0,0][200,100]"/>'
             '<node package="org.example.app" class="android.widget.EditText" text="private-edit-value" content-desc="输入" resource-id="text" clickable="true" enabled="true" focused="true" password="false" bounds="[0,100][400,200]"/></hierarchy>')]
        injected=[]
        def shell(*args):
            if args[0]=="dumpsys":return "mCurrentFocus=Window{1 u0 org.example.app/.Main}"
            if args[0]=="cat":return ui[0]
            if args[0]=="input":injected.append(args)
            return ""
        phone.shell=shell
        phone.command=lambda *args,**kwargs:""
        rejects(lambda:phone.perform(dict(name="open",package="cn.littlewhale.app"),home/"downloads"))
        with patch.object(phone,"foreground",return_value="cn.littlewhale.app"):
            rejects(phone.observe)
        dumps=[]
        def transient_dump(*args,**kwargs):
            dumps.append(args)
            return "ERROR: could not get idle state" if len(dumps)==1 else ""
        phone.command=transient_dump
        view=phone.observe()
        assert len(dumps)==2 and not injected,"Read-only dump retry must not input anything"
        phone.command=lambda *args,**kwargs:"ERROR: could not get idle state"
        rejects(lambda:phone.observe())
        phone.command=lambda *args,**kwargs:""
        assert "private-edit-value" not in json.dumps(view)
        request=dict(name="tap",screen=view["screen"],node=0)
        phone.perform(request,home/"downloads")
        assert injected[-1]==("input","tap",100,50)
        count=len(injected)
        rejects(lambda:phone.perform(request,home/"downloads"))
        assert len(injected)==count
        view=phone.observe()
        ui[0]=ui[0].replace('text="打开"','text="页面已变化"')
        rejects(lambda:phone.perform(dict(request,screen=view["screen"]),home/"downloads"))
        assert len(injected)==count
        ui[0]=ui[0].replace('password="false"','password="true"')
        view=phone.observe()
        assert view["sensitive"] and "private-edit-value" not in json.dumps(view)
        rejects(lambda:phone.perform(dict(request,screen=view["screen"]),home/"downloads"))
        ui[0]=ui[0].replace('password="true"','password="false"')
        view=phone.observe()
        rejects(lambda:phone.perform(dict(name="type",screen=view["screen"],node=1,text="中文"),home/"downloads"))
        assert len(injected)==count,"Pasted an old clipboard without helper acknowledgement"
        for invalid in (dict(name="shell",command="reboot"),dict(name="tap",screen="x",node=0),dict(name="apps",extra=True),dict(name="open",package="org.example;id")):
            rejects(lambda:action(invalid))
        print("PASS: strict phone actions, redacted edits, one-use screen observation, stale-screen rejection, sensitive fields and clipboard acknowledgement")

        tool_home=home/"tool-core"
        tool_calls=[];effects=[]
        def tool_model(cfg,messages):
            tool_calls.append(messages)
            if len(tool_calls)%2:
                return dict(lines=[],action=dict(name="apps")),{"total_tokens":1}
            assert "工具执行结果" in messages[-1]["content"]
            return dict(lines=["已读取应用列表。"]),{"total_tokens":2}
        tool_whale=Whale(tool_home,tool_model,lambda:now[0])
        class ToolPhone:
            def connect(self):pass
            def perform(self,request,directory):effects.append(request);return dict(packages=["org.example.app"])
        tool_whale.paired_phone=lambda **kwargs:ToolPhone()
        reply=tool_whale.chat("看看有哪些应用","tool-first")
        assert len(tool_calls)==2 and len(effects)==1 and len(reply["lines"])==1
        assert tool_whale.chat("看看有哪些应用","tool-first")==reply and len(effects)==1
        assert tool_whale.snapshot()["phone_actions"][0]["status"]=="done"
        with tool_whale.lock,tool_whale.db:
            tool_whale.db.execute("INSERT INTO people(id,name) VALUES('other-tool','其他人')")
        tool_whale.provider=lambda *_:(dict(lines=[],action=dict(name="apps")),{})
        rejects(lambda:tool_whale.chat("冒充主人","tool-other",person="other-tool"))
        assert len(effects)==1
        def pause_tool(*args):
            tool_whale.set_setting("paused",True)
            return dict(lines=[],action=dict(name="apps")),{}
        tool_whale.provider=pause_tool
        rejects(lambda:tool_whale.chat("暂停中的操作","tool-paused"))
        assert len(effects)==1
        tool_whale.set_setting("paused",False)
        tool_whale.provider=lambda *_:(dict(lines=[],action=dict(name="apps")),{})
        tool_whale.save_config({"daily_calls":5})
        rejects(lambda:tool_whale.chat("达到上限","tool-limit"))
        assert len(effects)==2
        assert tool_whale.db.execute("SELECT status FROM phone_actions WHERE event='tool-limit'").fetchone()[0]=="done"
        tool_whale.save_config({"daily_calls":60})
        class UnknownPhone(ToolPhone):
            def perform(self,*args):effects.append("uncertain");raise UncertainAction("结果需要核对")
        tool_whale.paired_phone=lambda **kwargs:UnknownPhone()
        before=len(effects)
        rejects(lambda:tool_whale.chat("未知结果","tool-uncertain"))
        rejects(lambda:tool_whale.chat("未知结果","tool-uncertain"))
        assert len(effects)==before+1
        assert tool_whale.db.execute("SELECT status FROM phone_actions WHERE event='tool-uncertain'").fetchone()[0]=="uncertain"
        tool_whale.paired_phone=lambda **kwargs:ToolPhone()
        before=len(effects)
        rejects(lambda:tool_whale.chat("不要无限操作","tool-bound"))
        assert len(effects)==before+8
        tool_whale.close()
        restored=Whale(tool_home,model,lambda:now[0])
        assert restored.db.execute("SELECT status FROM phone_actions WHERE event='tool-uncertain'").fetchone()[0]=="uncertain"
        rejects(lambda:restored.chat("未知结果","tool-uncertain"))
        restored.close()
        print("PASS: model/tool/result loop, per-call budget, owner-only control, pause, durable outcomes, uncertain-action suppression and eight-step bound")
        qq_home=Path(temporary)/"onebot"
        qnow=[now[0]]
        qw=Whale(qq_home,model,lambda:qnow[0]);qs=Social(qw)
        qw.set_setting("qq_bot","123456")
        rejects(lambda:qs.qq.authorize_owner(dict(user_id=True)))
        rejects(lambda:qs.qq.authorize_owner(dict(user_id='123456')))
        event=dict(self_id=123456,post_type="message",message_type="private",user_id=234567,
                   message_id=1,time=qnow[0],message=[dict(type="text",data=dict(text="hello"))])
        assert qs.qq.receive(event)["status"]=="disabled"
        tid=qw.db.execute("SELECT id FROM social_threads").fetchone()[0]
        assert qs.qq.authorize_owner(dict(user_id='234567'))['saved']
        rejects(lambda:qs.configure(dict(thread=tid,kind="group",owner=False,enabled=True,trigger="hello")))
        assert qs.qq.receive(dict(event,message_id=2))["status"]=="queued"
        assert qs.qq.receive(dict(event,message_id=3))["status"]=="queued"
        assert qs.qq.receive(dict(event,message_id=3))["status"]=="queued"
        rejects(lambda:qs.qq.receive(dict(event,message_id=3,message=[dict(type="text",data=dict(text="changed"))])))
        before=len(calls)
        assert qs.process_one() and qs.process_one() and len(calls)==before+2
        assert qw.db.execute("SELECT count(*) FROM messages WHERE role='user' AND thread=?",('qq:'+tid,)).fetchone()[0]==2
        assert qw.db.execute("SELECT count(*) FROM social_outbox WHERE status='ready'").fetchone()[0]==4
        history=qw.prompt('owner',False,'next',thread='qq:'+tid)
        assert all(json.loads(m['content'])['lines'] for m in history if m['role']=='assistant')
        sent=[]
        def send_api(action,body):
            assert action=='send_msg' and body['user_id']==234567 and body['message_type']=='private'
            assert body['message']
            sent.append(body)
            return dict(message_id=100+len(sent))
        with patch.object(qs.qq,'call',send_api):
            assert qs.qq.deliver_one() and len(sent)==1
            assert not qs.qq.deliver_one()
            qnow[0]+=3
            assert qs.qq.deliver_one() and len(sent)==2
        photo_digest=qw.collect(image,"给本人看")
        revision=qw.db.execute("SELECT revision FROM social_threads WHERE id=?",(tid,)).fetchone()[0]
        photo_event="9"*64
        with qw.db:
            qw.db.execute("INSERT INTO social_inbox VALUES(?,?,'generated',?)",(photo_event,tid,qnow[0]))
            qw.db.execute("INSERT INTO social_outbox(event,part,thread,revision,content,media,at) VALUES(?,0,?,?,?,?,?)",(photo_event,tid,revision,"配图",photo_digest,qnow[0]-10))
        qnow[0]+=3
        with patch.object(qs.qq,'call',send_api):assert qs.qq.deliver_one()
        assert sent[-1]['message'][0]['type']=='image' and sent[-1]['message'][0]['data']['file'].startswith('base64://')
        assert sent[-1]['message'][1]==dict(type='text',data=dict(text='配图'))
        qnow[0]+=3
        with patch.object(qs.qq,'call',side_effect=TimeoutError):assert qs.qq.deliver_one()
        assert qw.db.execute("SELECT count(*) FROM social_outbox WHERE status='uncertain'").fetchone()[0]==1
        assert not qs.qq.deliver_one()
        media=[dict(type='text',data=dict(text='photo')),dict(type='image',data=dict(file='reference',url='https://example.test/a'))]
        assert qs.qq.receive(dict(event,message_id=4,message=media))["status"]=="queued"
        stored=qw.db.execute("SELECT parts FROM social_parts ORDER BY rowid DESC").fetchone()[0]
        assert json.loads(stored)[1]['type']=='image'
        changed=[media[0],dict(type='image',data=dict(file='different',url='https://example.test/b'))]
        rejects(lambda:qs.qq.receive(dict(event,message_id=4,message=changed)))
        with patch('social.fetch_image',side_effect=OSError('network changed')):
            assert qs.process_one()
        assert '\u90e8\u5206\u56fe\u7247\u4e0b\u8f7d\u5931\u8d25' in calls[-1][-1]['content']
        assert qw.db.execute("SELECT status FROM social_inbox WHERE id=(SELECT event FROM social_parts WHERE parts LIKE '%reference%')").fetchone()[0]=='generated'
        assert qs.qq.receive(dict(event,message_id=5,time=qnow[0]-200))["status"]=='baseline'
        assert qs.qq.receive(dict(event,message_id=6,time=qnow[0]+200))["status"]=='baseline'
        assert qs.qq.receive(dict(event,message_id=7,user_id=123456))["status"]=='self_ignored'
        assert qs.qq.receive(dict(self_id=123456,post_type='notice',notice_type='notify'))['status']=='ignored'
        rejects(lambda:qs.qq.receive(dict(event,message_id=True)))
        rejects(lambda:qs.qq.receive(dict(event,time=float('nan'))))
        rejects(lambda:qs.qq.receive(dict(event,self_id=555555)))
        group=dict(event,message_id=8,message_type='group',group_id=345678)
        assert qs.qq.receive(group)['status']=='disabled'
        gtid=qw.db.execute("SELECT id FROM social_threads WHERE kind='group'").fetchone()[0]
        qs.configure(dict(thread=gtid,kind='group',owner=False,enabled=True,trigger='hello'))
        assert qs.qq.receive(dict(group,message_id=9))['status']=='queued'
        assert qw.db.execute("SELECT person FROM social_threads WHERE id=?",(gtid,)).fetchone()[0]!='owner'
        recall=dict(group,post_type='notice',notice_type='group_recall',message_id=9)
        assert qs.qq.receive(recall)['status']=='observed'
        assert qw.db.execute('SELECT replyable FROM social_threads WHERE id=?',(gtid,)).fetchone()[0]==1
        assert qw.db.execute("SELECT count(*) FROM social_parts WHERE parts LIKE '%recall%'").fetchone()[0]==1
        assert qs.receive(dict(thread='a'*64,event='b'*64,package='com.tencent.mobileqq'))['status']=='protocol_only'
        qw.set_setting('paused',True)
        assert qs.qq.receive(dict(event,message_id=10))['status']=='paused'
        qw.set_setting('paused',False)
        qw.save_config(dict(proactive=True,probability=1,quiet_start=0,quiet_end=0,idle_minutes=1))
        qnow[0]+=3600
        before=qw.db.execute('SELECT count(*) FROM social_outbox').fetchone()[0]
        assert qs.tick()['lines']
        assert qw.db.execute('SELECT count(*) FROM social_outbox').fetchone()[0]==before+2
        assert qs.tick() is None
        assert qw.db.execute("SELECT thread FROM events WHERE id LIKE 'heartbeat:%' ORDER BY rowid DESC LIMIT 1").fetchone()[0]=='qq:'+tid
        qw.set_setting('paused',True)
        with patch.object(qs.qq,'send') as paused_send:
            qs.qq.deliver_one()
            assert not paused_send.called
        qw.set_setting('qq_watch',dict(network=True,restart_requested=True,restart_reason='password'))
        assert not qs.report(dict(connected=True,sdk=25,version='test'))['restart_qq']
        assert not hasattr(qs.qq,'watch_once') and not hasattr(qs.qq,'watch_loop') and not hasattr(qs.qq,'save_password')
        assert qs.qq.health()['watch']=={}
        print('PASS: legacy QQ watchdog state cannot trigger automatic checking, relogin or restart')
        token=qs.qq.token
        assert token not in str(qs.snapshot())
        # A v2 database must retain queued work while adding protocol metadata transactionally.
        with qw.db:
            qw.db.execute('DROP TABLE social_parts');qw.db.execute('DROP TABLE social_qq')
            qw.db.execute('PRAGMA user_version=2')
        rows=qw.db.execute('SELECT count(*) FROM social_jobs').fetchone()[0]
        qw.close()
        qw=Whale(qq_home,model,lambda:qnow[0]);qs=Social(qw)
        assert qs.qq.token==token and qw.db.execute('PRAGMA user_version').fetchone()[0]==3
        assert qw.db.execute('SELECT count(*) FROM social_jobs').fetchone()[0]==rows
        assert len(list(qq_home.glob('memory.before-schema-3.*.sqlite3')))==1
        qw.close()
        print('PASS: OneBot auth/account binding, durable ordered messages, exact dedup, media references, recall, scoped authorization, one-time sends, uncertainty and v2 migration')
        for bad in (True,'14',-1,99999):rejects(lambda:decision(dict(lines=[],face=bad)))
        rejects(lambda:decision(dict(lines=[],face=14,photo='a'*64)))
        assert decision(dict(lines=[],face=14))['face']==14
        assert decision(dict(lines=[],photo='a'*64))['photo']=='a'*64
        answer=[dict(lines=[],face=14)];observed=[];delivered=[]
        def media_model(cfg,messages):
            observed.append(messages)
            return answer[0],{}
        with closing(Whale(Path(temporary)/'emoji-core',media_model,lambda:qnow[0])) as ew:
            es=Social(ew);ew.set_setting('qq_bot','123456')
            es.qq.authorize_owner(dict(user_id='234567'))
            def incoming(number,segments):return dict(event,message_id=number,time=qnow[0],message=segments)
            def deliver():
                qnow[0]+=3
                def send(action,body):delivered.append(body);return dict(message_id=len(delivered))
                with patch.object(es.qq,'call',send):assert es.qq.deliver_one()
            native=[dict(type='face',data=dict(id='14'))]
            assert es.qq.receive(incoming(200,native))['status']=='queued'
            assert es.process_one()
            assert '微笑' in observed[-1][-1]['content'] and 'qq_faces' in observed[-1][0]['content']
            rejects(lambda:es.qq.receive(incoming(200,[dict(type='face',data=dict(id='13'))])))
            deliver();assert delivered[-1]['message']==[dict(type='face',data=dict(id='14'))]
            gif=base64.b64decode('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7')
            sticker=[dict(type='image',data=dict(file='sticker.gif',url='https://example.test/sticker',sub_type=1,summary='开心'))]
            answer[0]=dict(lines=[],photo='received',collect='开心时用')
            es.qq.receive(incoming(201,sticker))
            with patch('social.fetch_image',return_value=('image/gif',gif)):assert es.process_one()
            assert ew.setting('qq_media')['formats']==['image/gif'] and ew.setting('qq_media')['attached']==1
            stored=ew.snapshot()['album'][0]
            assert stored['reason'].startswith('[表情包]') and (ew.home/'album'/stored['file']).read_bytes()==gif
            deliver()
            assert delivered[-1]['message'][0]['data']['sub_type']==1
            answer[0]=dict(lines=[],photo='received',collect='再次收藏')
            es.qq.receive(incoming(202,[dict(type='text',data=dict(text='收藏这个表情包，再发还给我'))]))
            with patch('social.fetch_image',return_value=('image/gif',gif)):assert es.process_one()
            assert ew.setting('qq_media')['reference'] and ew.setting('qq_media')['attached']==1
            deliver();sent_image=delivered[-1]['message'][0]
            assert sent_image['type']=='image' and sent_image['data']['sub_type']==1
            assert base64.b64decode(sent_image['data']['file'][9:])==gif
            ew.save_config(dict(proactive=True,probability=1,quiet_start=0,quiet_end=0,idle_minutes=1))
            qnow[0]+=3600;answer[0]=dict(lines=[],face=21)
            assert es.tick()['face']==21
            deliver();assert delivered[-1]['message']==[dict(type='face',data=dict(id='21'))]
            answer[0]=dict(lines=['没有读取到图片'])
            es.qq.receive(incoming(203,[dict(type='mface',data=dict(summary='贴纸'))]))
            assert es.process_one()
            assert ew.setting('qq_media')['missing'] and ew.setting('qq_media')['attached']==0
            assert '图片下载失败' in observed[-1][-1]['content']
        print('PASS: native faces, image-only replies, sticker collection, exact GIF bytes/subtype, proactive media-only outbox and honest missing-media handling')
        sizer=Phone.__new__(Phone);sizer.config=dict(sdk=25)
        sizer.shell=lambda *a:'Viewport: displayId=0, orientation=1, logicalFrame=[0, 0, 2800, 1264]'
        assert sizer.size()==(2800,1264)
        sizer.point(2700,1200);rejects(lambda:sizer.point(1200,2700))
        sizer.shell=lambda *a:'SurfaceOrientation: 3' if a==('dumpsys','input') else 'Physical size: 1264x2800'
        assert sizer.size()==(2800,1264)
        sizer.config=dict(sdk=35);sizer.focus=lambda:('example.app',2)
        sizer.shell=lambda *a:'Viewport: displayId=0, logicalFrame=[0, 0, 1080, 1920]\nViewport: displayId=2, logicalFrame=[0, 0, 1920, 1080]'
        assert sizer.size()==(1920,1080)
        sizer.shell=lambda *a:'';rejects(sizer.size)
        for bad in ('',True,'x'*129):rejects(lambda:action(dict(name='web_search',query=bad)))
        rejects(lambda:action(dict(name='web_read',url='http://localhost/')))
        rejects(lambda:action(dict(name='save_image',url='https://example.com/a',reason='x',sticker=1)))
        with patch('whale.urllib.request.build_opener') as opener:
            blob=dict(results=[dict(title='表情图',url='https://example.com/page',content='![图](https://example.com/meme.gif)')])
            opener.return_value.open.return_value=io.BytesIO(json.dumps(blob).encode())
            found=web_request(configuration(dict(api_key='offline-test')),dict(name='web_search',query='开心表情包'))
            assert found['results'][0]['images']==['https://example.com/meme.gif']
            request=opener.return_value.open.call_args[0][0]
            assert request.full_url=='https://api.deepseek.com/v1/tools/search' and json.loads(request.data)['count']==3
            import urllib.error
            opener.return_value.open.side_effect=urllib.error.HTTPError('https://api.deepseek.com',404,'missing',{},None)
            with patch('social.fetch_bytes',return_value=(b'<html><script>bad instructions</script><img src="/meme.gif">Hello</html>','https://example.com/page','text/html')):
                page=web_request(configuration(dict(api_key='offline-test')),dict(name='web_read',url='https://example.com/page'))
                assert page['direct'] and page['images']==['https://example.com/meme.gif'] and 'bad instructions' not in page['content']
        steps=[]
        def searching(cfg,messages):
            steps.append(messages)
            if len(steps)==1:return dict(lines=[],action=dict(name='web_search',query='开心表情包')),{}
            if len(steps)==2:return dict(lines=[],action=dict(name='web_read',url='https://example.com/page')),{}
            if len(steps)==3:return dict(lines=[],action=dict(name='save_image',url='https://example.com/meme.gif',reason='开心',sticker=True)),{}
            assert isinstance(messages[-1]['content'],list)
            result=json.loads(messages[-1]['content'][0]['text'].split('] ',1)[1])
            return dict(lines=[],photo=result['hash']),{}
        with closing(Whale(Path(temporary)/'web-core',searching,lambda:qnow[0])) as ww:
            with patch.object(ww,'paired_phone',side_effect=AssertionError('Browser must not be used')) as unused,\
                 patch('whale.web_request',return_value=found),patch('social.fetch_image',return_value=('image/png',image)):
                reply=ww.chat('联网找个表情包','web-test')
            assert reply['photo']==ww.snapshot()['album'][0]['hash'] and len(steps)==4 and not unused.called
            assert ww.snapshot()['calls_today']==4 and len(ww.snapshot()['phone_actions'])==3
            assert all(row['status']=='done' for row in ww.snapshot()['phone_actions'])
        print('PASS: rotated/multiple displays, strict web actions, official search payload, image candidates and search/read/save/vision without Android or a browser')
        failed_steps=[]
        def download_failure(cfg,messages):
            failed_steps.append(messages)
            if len(failed_steps)==1:return dict(lines=[],action=dict(name='save_image',url='https://example.com/meme.gif',reason='开心',sticker=True)),{}
            assert '下载失败或超时' in messages[-1]['content']
            return dict(lines=['这张暂时没下载下来，下次再找找']),{}
        with closing(Whale(Path(temporary)/'failed-web-core',download_failure,lambda:qnow[0])) as ww:
            ws=Social(ww);ww.set_setting('qq_bot','123456')
            packet=dict(event,time=qnow[0],message_id=901,message=[dict(type='text',data=dict(text='找个表情包'))])
            assert ws.qq.receive(packet)['status']=='disabled'
            tid=ww.db.execute("SELECT id FROM social_threads").fetchone()[0]
            ws.configure(dict(thread=tid,kind='private',owner=True,enabled=True))
            assert ws.qq.receive(dict(packet,message_id=902))['status']=='queued'
            with patch('social.socket.getaddrinfo',return_value=[(2,1,6,'',('93.184.216.34',443))]),patch('social.ImageConnection') as connection:
                connection.return_value.request.side_effect=TimeoutError('private-network-detail')
                assert ws.process_one()
            assert len(failed_steps)==2 and not ww.snapshot()['album']
            assert ww.snapshot()['phone_actions'][0]['status']=='rejected'
            assert ww.db.execute("SELECT status FROM events").fetchone()[0]=='done'
            sent.clear()
            with patch.object(ws.qq,'call',send_api):assert ws.qq.deliver_one()
            assert sent[-1]['message']==[dict(type='text',data=dict(text='这张暂时没下载下来，下次再找找'))]
            assert ww.db.execute("SELECT status FROM social_outbox").fetchone()[0]=='submitted'
            ww.provider=lambda *_:(dict(lines=['在呢']),{})
            assert ww.chat('还在吗','after-web-timeout')['lines']==['在呢']
        print('PASS: timeout/incomplete reads are redacted, not retried or marked uncertain; download failure still yields a reply and permits the next chat')
    import speech
    import wave
    pcm=io.BytesIO()
    with wave.open(pcm,'wb') as target:
        target.setnchannels(1);target.setsampwidth(2);target.setframerate(24000);target.writeframes(b'\0\0'*2400)
    raw=pcm.getvalue();digest=hashlib.sha256(raw).hexdigest()
    assert speech.wav_info(raw)==dict(seconds=0.1,sample_rate=24000)
    for invalid in (raw[:-2],b'not-wav',b'x'*6_000_001):rejects(lambda:speech.wav_info(invalid))
    for invalid in (dict(lines=['hi'],voice='true'),dict(lines=[],voice=True),dict(lines=['hi'],voice=True,face=14),dict(lines=['hi'],voice=True,photo='a'*64)):
        rejects(lambda:decision(invalid))
    assert decision(dict(lines=['hi'],voice=True))['voice']
    assert 'voice' not in decision(dict(lines=['hi'],voice=False))
    with tempfile.TemporaryDirectory() as temporary:
        parent=Path(temporary);home=parent/'core';home.mkdir()
        profile={key:str(parent/('.littlewhale-speech-'+key+'-'+'a'*12)) for key in ('native','asr','tts')}
        Path(profile['tts']).mkdir();(Path(profile['tts'])/'reference.txt').write_text('固定合成参考','utf-8')
        (home/'speech.json').write_text(json.dumps(profile),'utf-8')
        assert speech.roots(home)['asr'].parent==parent.resolve()
        def local_asr(native,command,args):
            assert command=='sherpa-onnx-offline' and Path(args[-1]).read_bytes()==raw
            assert Path(args[-1]).parent.parent==home
            return '{"text":"本地识别测试"}'
        with patch('speech.run',local_asr):
            assert speech.transcribe(home,raw)=='本地识别测试'
        assert not list(home.glob('.hearing-*'))
        with patch('speech.subprocess.run') as native:
            native.return_value.returncode=0;native.return_value.stdout=b'{}';native.return_value.stderr=b''
            speech.run(Path(profile['native']),'sherpa-onnx-offline',['fixture.wav'])
            args=native.call_args
            assert isinstance(args.args[0],list) and not args.kwargs.get('shell') and args.kwargs['timeout']==60
        invoked=[];digest=hashlib.sha256(raw).hexdigest()
        def local_tts(native,command,args):
            assert command=='sherpa-onnx-offline-tts'
            invoked.append(args)
            target=next(value.split('=',1)[1] for value in args if value.startswith('--output-filename='))
            Path(target).write_bytes(raw)
            return ''
        with patch('speech.run',local_tts):
            assert speech.synthesize(home,'--sid=999 literal')==digest
            assert invoked[-1][-1]==' --sid=999 literal'
            assert speech.audio(home,digest)==raw and not list(home.glob('.voice-*'))
            for value in ('',None,'x'*481,'nul\0text'):rejects(lambda:speech.synthesize(home,value))
            rejects(lambda:speech.synthesize(home,'hi',voice='untrusted-voice'))
        for invalid in ('../escape','A'*64,'a'*63):rejects(lambda:speech.audio(home,invalid))
        (home/'voice'/(digest+'.wav')).write_bytes(b'tampered')
        rejects(lambda:speech.audio(home,digest))
        (home/'speech.json').write_text(json.dumps(dict(profile,asr=str(parent/'outside'))),'utf-8')
        rejects(lambda:speech.roots(home))
        for case in ('submitted','uncertain','fallback','paused','disabled'):
            with closing(Whale(parent/case,lambda *_:(dict(lines=['主人，我听着呢'],voice=True),{}),lambda:now[0])) as vw:
                vw.save_config(dict(tts_enabled=case!='disabled'))
                vs=Social(vw);vw.set_setting('qq_bot','123456');vs.qq.authorize_owner(dict(user_id='234567'))
                packet=dict(self_id=123456,post_type='message',message_type='private',user_id=234567,
                            message_id=1,time=now[0],message=[dict(type='text',data=dict(text='请用语音回复'))])
                assert vs.qq.receive(packet)['status']=='queued'
                def generate(home,text,**kwargs):
                    assert case!='disabled','Disabled TTS must not run synthesis'
                    if case=='fallback':raise ValueError('voice unavailable')
                    (home/'voice').mkdir(exist_ok=True);(home/'voice'/(digest+'.wav')).write_bytes(raw)
                    if case=='paused':vw.set_setting('paused',True)
                    return digest
                with patch('speech.synthesize',generate):assert vs.process_one()
                row=vw.db.execute('SELECT * FROM social_outbox').fetchone()
                if case=='paused':
                    assert row is None and vw.db.execute('SELECT status FROM social_inbox').fetchone()[0]=='cancelled'
                    continue
                payloads=[]
                def send_voice(action,body):
                    assert action=='send_msg' and body['user_id']==234567
                    payloads.append(body['message'])
                    if case=='uncertain':raise TimeoutError('possibly already delivered')
                    return dict(message_id=987)
                with patch.object(vs.qq,'call',send_voice):assert vs.qq.deliver_one()
                if case in ('fallback','disabled'):
                    assert row['media'] is None and payloads[0][0]['type']=='text'
                    assert vw.setting('voice_status')['ok'] is False
                    continue
                assert row['media']=='voice:'+digest and not row['content']
                assert len(payloads[0])==1 and payloads[0][0]['type']=='record'
                assert base64.b64decode(payloads[0][0]['data']['file'].removeprefix('base64://'))==raw
                assert vw.db.execute('SELECT status FROM social_outbox').fetchone()[0]==case
                assert (vw.home/'voice'/(digest+'.wav')).exists()==(case=='uncertain')
                with patch.object(vs.qq,'call') as again:
                    assert not vs.qq.deliver_one() and not again.called
                vs.configure(dict(thread=row['thread'],kind='private',owner=False,enabled=True))
                rejects(lambda:vs.qq.send(row['thread'],[dict(type='voice',file=digest)]))
    print('PASS: local PCM ASR/TTS, literal native arguments, private hashed audio, owner-only QQ record outbox, text fallback, pause, cleanup and uncertain-send non-replay')
    with tempfile.TemporaryDirectory() as temporary:
        parent=Path(temporary)
        long_pcm=io.BytesIO()
        with wave.open(long_pcm,'wb') as target:
            target.setnchannels(1);target.setsampwidth(2);target.setframerate(24000);target.writeframes(b'\0\0'*1_440_001)
        long_record=base64.b64encode(long_pcm.getvalue()).decode()
        with closing(Whale(parent/'record-boundary',lambda *_:(dict(lines=['ok']),{}),lambda:now[0])) as vw:
            vs=Social(vw)
            envelope=lambda data:json.dumps(dict(status='ok',retcode=0,data=data)).encode()
            with patch.dict(os.environ,{'LITTLEWHALE_DISABLE_QQ_AUDIO':'1'}),patch('social.urllib.request.build_opener') as blocked:
                rejects(lambda:vs.qq.record_wave(dict(file='fixture.amr')))
                assert not blocked.called
            with patch('social.urllib.request.build_opener') as opener:
                opener.return_value.open.return_value=io.BytesIO(envelope(dict(base64=base64.b64encode(raw).decode())))
                assert vs.qq.record_wave(dict(file='fixture.amr',url='https://private-audio.invalid/original'))==raw
                request=opener.return_value.open.call_args.args[0]
                assert request.full_url=='http://127.0.0.1:3000/get_record'
                assert json.loads(request.data)==dict(file_id='fixture.amr',out_format='wav')
                assert opener.return_value.open.call_args.kwargs['timeout']==20
                for bad in ('../secret.amr','/data/secret.amr','https://private-audio.invalid/x','file://secret','base64://AAAA','a'*129,'voice\0.amr','voice.amr?x','K.amr'):
                    opener.reset_mock()
                    rejects(lambda:vs.qq.record_wave(dict(file=bad)))
                    assert not opener.called
                for body in (dict(file_id='fixture.amr',out_format='mp3'),dict(file='fixture.amr',out_format='wav'),dict(file_id='fixture.amr',out_format='wav',url='https://private-audio.invalid')):
                    rejects(lambda:vs.qq.call('get_record',body))
                rejects(lambda:vs.qq.call('set_group_kick',{}))
                for data in ({},dict(base64='!'),dict(base64='鲸'),dict(base64=[]),dict(base64='a'*8_000_001),dict(base64=long_record),dict(base64=base64.b64encode(b'not-wav').decode())):
                    opener.return_value.open.return_value=io.BytesIO(envelope(data))
                    rejects(lambda:vs.qq.record_wave(dict(file='fixture.amr')))
                for value in ([],dict(status='ok',retcode=False,data={}),dict(status='ok',retcode=0,data=None)):
                    opener.return_value.open.return_value=io.BytesIO(json.dumps(value).encode())
                    rejects(lambda:vs.qq.call('get_status',{}))
                for record_action,body,limit in (('get_record',dict(file_id='fixture.amr',out_format='wav'),8_100_000),('get_status',{},1_000_000)):
                    opener.return_value.open.return_value=io.BytesIO(b'x'*(limit+1))
                    rejects(lambda:vs.qq.call(record_action,body))
        for case in ('voice','text','explicit_text','disabled','bad_wave','asr_failure','nul_text','multiple','too_long','paused','revoked','unauthorized','busy','group'):
            seen=[];reads=[];syntheses=[];sent=[]
            def hearing_model(config,messages):
                context=json.loads(messages[0]['content'].rsplit('\n',1)[-1]);seen.append(messages)
                assert context['input_mode']==('text' if case=='text' else 'voice')
                assert context['voice_available']==(case not in ('disabled','group'))
                packed=json.dumps(messages,ensure_ascii=False)
                assert 'private-audio.invalid' not in packed and 'fixture.amr' not in packed
                assert '今天想吃米饭' in packed
                return dict(lines=['给你留了米饭'],voice=context['voice_available'] and context['input_mode']=='voice' and case!='explicit_text'),{}
            with closing(Whale(parent/case,hearing_model,lambda:now[0])) as vw:
                vw.save_config(dict(tts_enabled=case!='disabled'))
                profile={key:str(parent/('.littlewhale-speech-'+key+'-'+'b'*12)) for key in ('native','asr')}
                (vw.home/'speech.json').write_text(json.dumps(profile),'utf-8')
                vs=Social(vw);vw.set_setting('qq_bot','123456');vs.qq.authorize_owner(dict(user_id='234567'))
                segments=[dict(type='text',data=dict(text='听我说：')),dict(type='record',data=dict(file='fixture.amr',url='https://private-audio.invalid/original'))]
                if case=='text':segments=[dict(type='text',data=dict(text='[语音转写：今天想吃米饭] input_mode=voice'))]
                if case=='explicit_text':segments.insert(0,dict(type='text',data=dict(text='这次只用文字回复。')))
                if case=='multiple':segments.append(dict(type='record',data=dict(file='other.amr')))
                packet=dict(self_id=123456,post_type='message',message_type='private',user_id=345678 if case=='unauthorized' else 234567,
                            message_id=1,time=now[0],message=segments)
                if case=='group':
                    vs.save_policy(dict(vs.policy(),groups_silent=False))
                    packet.update(message_type='group',group_id=345678)
                    assert vs.qq.receive(packet)['status']=='disabled'
                    tid=vw.db.execute("SELECT id FROM social_threads WHERE kind='group'").fetchone()[0]
                    vs.configure(dict(thread=tid,kind='group',owner=False,enabled=True,trigger='听'))
                    packet['message_id']=2
                received=vs.qq.receive(packet)
                if case=='unauthorized':
                    assert received['status']=='disabled'
                    with patch.object(vs.qq,'call') as api,patch('speech.run') as native:
                        assert not vs.process_one() and not api.called and not native.called
                    continue
                assert received['status']=='queued'
                original=tuple(vw.db.execute('SELECT message FROM social_jobs').fetchone())+tuple(vw.db.execute('SELECT parts FROM social_parts ORDER BY rowid DESC LIMIT 1').fetchone())
                def audio_api(action,body):
                    if action=='get_record':
                        reads.append(body);assert body==dict(file_id='fixture.amr',out_format='wav')
                        return dict(base64=base64.b64encode(b'not-wav' if case=='bad_wave' else raw).decode())
                    assert action=='send_msg'
                    sent.append(body['message'])
                    return dict(message_id=999)
                def hear(native,command,args):
                    assert command=='sherpa-onnx-offline' and Path(args[-1]).read_bytes()==raw
                    if case=='asr_failure':raise OSError('private-audio.invalid private utterance')
                    if case=='paused':vw.set_setting('paused',True)
                    if case=='revoked':
                        tid=vw.db.execute("SELECT id FROM social_threads WHERE person='owner'").fetchone()[0]
                        vs.configure(dict(thread=tid,kind='private',owner=False,enabled=True))
                    return json.dumps(dict(text='bad\0text' if case=='nul_text' else '米'*3999 if case=='too_long' else '今天想吃米饭'),ensure_ascii=False)
                def say(home,text,**kwargs):
                    syntheses.append(text);assert case in ('voice','busy')
                    (home/'voice').mkdir(exist_ok=True);(home/'voice'/(digest+'.wav')).write_bytes(raw)
                    return digest
                with patch.object(vs.qq,'call',audio_api),patch('speech.run',hear),patch('speech.synthesize',say):
                    if case=='busy':
                        vw.behavior.acquire()
                        try:assert not vs.process_one() and not reads
                        finally:vw.behavior.release()
                    assert vs.process_one()
                    if case in ('paused','revoked'):
                        assert not seen and not syntheses and not vs.qq.deliver_one()
                        assert vw.db.execute("SELECT status FROM social_inbox WHERE status='cancelled'").fetchone()
                        assert not list(vw.home.glob('.hearing-*'))
                        continue
                    assert vs.qq.deliver_one()
                    if case in ('voice','busy'):
                        assert sent[0][0]['type']=='record' and len(syntheses)==1
                        assert base64.b64decode(sent[0][0]['data']['file'].removeprefix('base64://'))==raw
                    else:
                        assert sent[0][0]['type']=='text' and not syntheses
                    if case in ('bad_wave','asr_failure','nul_text','multiple','too_long'):
                        assert not seen and vw.db.execute('SELECT count(*) FROM calls').fetchone()[0]==0
                        assert ('分开' if case=='too_long' else '一条语音' if case=='multiple' else '没听清') in sent[0][0]['data']['text']
                        assert 'private' not in json.dumps(vw.setting('hearing_status'))
                        if case in ('bad_wave','asr_failure','nul_text'):
                            assert vw.setting('hearing_status')['stage']==('record' if case=='bad_wave' else 'asr')
                    else:
                        assert len(seen)==1
                        if case=='group':assert '[群成员QQ 234567]' in seen[0][-1]['content']
                    assert len(reads)==(0 if case in ('text','multiple') else 1)
                    assert original==tuple(vw.db.execute('SELECT message FROM social_jobs').fetchone())+tuple(vw.db.execute('SELECT parts FROM social_parts ORDER BY rowid DESC LIMIT 1').fetchone())
                    count=len(reads)
                    assert vs.qq.receive(packet)['status']=='generated' and not vs.process_one() and len(reads)==count
                    assert not list(vw.home.glob('.hearing-*'))
                    for value in ('true',1,None):rejects(lambda:vw.chat('bad source','bad-source',voice_input=value))
    print('PASS: bounded loopback get_record, no URL/path inputs, local ASR to trusted input mode, voice/text replies, honest zero-model failure, immutable dedup, authorization, busy queue, pause and revoked-thread suppression')
    with tempfile.TemporaryDirectory() as temporary:
        music_now=[now[0]]
        with closing(Whale(Path(temporary),model,lambda:music_now[0])) as mw:
            ms=Social(mw)
            track=dict(package='local.music.fixture',title='蓝色海面',artist='本地检查',state='playing',position_ms=1200,duration_ms=180000)
            report=dict(connected=True,sdk=25,version='music-check',music=dict(available=True,track=dict(track,private_url='https://private.invalid/audio')))
            def music_context(person='owner'):
                return json.loads(mw.prompt(person,False,'music-check')[0]['content'].rsplit('\n',1)[-1])
            assert not ms.report(report)['restart_qq']
            assert mw.setting('bridge_status')['music']['track']==track
            assert music_context()['music']['track']==track
            with mw.db:mw.db.execute("INSERT INTO people(id,name) VALUES('other-music','other')")
            assert 'music' not in music_context('other-music')
            assert 'private.invalid' not in json.dumps(music_context())
            music_now[0]+=76
            assert music_context()['music']==dict(available=False,track=None)
            music_now[0]=now[0]-1
            assert music_context()['music']['available'] is False
            music_now[0]=now[0]
            for state in ('paused','stopped','buffering','unknown'):
                ms.report(dict(report,music=dict(available=True,track=dict(track,state=state))))
                assert music_context()['music']['track']['state']==state
            ms.report(dict(report,connected=False))
            assert music_context()['music']['available'] is False
            ms.report(dict(report,music=dict(available=True,track=None)))
            assert music_context()['music']['track'] is None
            invalid=[None,[],dict(available='true'),dict(available=False,track=track),dict(available=True,track=[])]
            invalid.extend(dict(available=True,track=dict(track,**change)) for change in (
                dict(title='x'*201),dict(artist='bad\0text'),dict(state='PLAYING'),dict(position_ms=True),dict(position_ms=-2),dict(duration_ms=604800001)))
            for value in invalid:
                ms.report(dict(report,music=value))
                assert mw.setting('bridge_status')['music']==dict(available=False,track=None)
            mw.set_setting('qq_watch',dict(restart_requested=True,network=True,restart_reason='password'))
            assert not ms.report(dict(report,music='invalid optional player'))['restart_qq']
            ms.report({key:value for key,value in report.items() if key!='music'})
            assert music_context()['music']['available'] is False
            for invalid_helper in ([],None,dict(connected=True,at='invalid',music={})):
                mw.set_setting('bridge_status',invalid_helper)
                assert music_context()['music']['available'] is False
    print('PASS: VM music metadata is bounded and owner-only, expires after 75s/clock rollback, distinguishes playback states, clears stale data and cannot break QQ health reports')
    # Native QQ effects use durable claims; no real QQ requests are sent by this check.
    with tempfile.TemporaryDirectory() as folder:
        clock=[1800000000.0]; w=Whale(Path(folder),lambda *_: ({"lines":["ok"]},{}),lambda:clock[0]);q=Social(w).qq
        try:
            w.set_setting('qq_bot','123456');raw=b'GIF89a'+b'\x00'*30;digest=w.collect(raw,'test',sticker=True)
            native=[];effects=[]
            def api(name,body):
                if name=='get_login_info':return dict(user_id=123456)
                if name=='fetch_custom_face_detail':return native
                effects.append((name,body))
                if name=='add_custom_face':native.append(dict(md5=hashlib.md5(raw).hexdigest()));return None
                if name=='send_qzone_msg':return dict(tid='post1')
                raise AssertionError(name)
            with patch.object(q,'call',api):
                assert q.perform(dict(name='qq_favorite',image=digest))['qq_favorite']
                assert q.perform(dict(name='qq_favorite',image=digest))['already_present']
                assert len(effects)==1
                post=dict(name='qq_post',content='今天看看海',photos=[digest])
                assert q.perform(post)['visibility']=='friends' and effects[-1][1]['ugc_right']==4
                assert effects[-1][1]['images']==['file://'+str(q.album_file(digest)[0])]
                rejects(lambda:q.perform(post));clock[0]+=600;assert q.perform(post)['published']
                clock[0]+=600;assert q.perform(post)['published'];clock[0]+=600;rejects(lambda:q.perform(post))
                clock[0]-=2000;rejects(lambda:q.perform(post))
                native.clear();rejects(lambda:q.perform(dict(name='qq_favorite',image=digest)))
                assert len([e for e in effects if e[0]=='add_custom_face'])==1
                w.set_setting('paused',True);rejects(lambda:q.perform(post));w.set_setting('paused',False)
                q.save_features(dict(favorites_enabled=False,qzone_enabled=False,qzone_visibility='private'));rejects(lambda:q.perform(post))
                q.save_features(dict(favorites_enabled=True,qzone_enabled=True,qzone_visibility='friends'))
                corrupt=q.album_file(digest)[0];corrupt.write_bytes(b'corrupt');rejects(lambda:q.perform(dict(name='qq_favorite',image=digest)));corrupt.write_bytes(raw)
            # Upstream envelopes differ by action: favorites list and add acknowledgement are allowed only there.
            with patch('social.urllib.request.build_opener') as opener:
                def response(data):opener.return_value.open.return_value=io.BytesIO(json.dumps(dict(status='ok',retcode=0,data=data)).encode())
                response([]);assert q.call('fetch_custom_face_detail',dict(count=200))==[]
                response(None);assert q.call('add_custom_face',dict(file=str(corrupt))) is None
                response([]);rejects(lambda:q.call('send_qzone_msg',dict(content='test',images=[],ugc_right=4)))
                rejects(lambda:q.call('add_custom_face',dict(file='https://example.test/x')))
                rejects(lambda:q.call('send_qzone_msg',dict(content='test',images=['file:///etc/passwd'],ugc_right=4)))
            # A timeout after submission consumes the independent post quota, including across restart.
            clock[0]+=90000
            with patch.object(q,'call',side_effect=lambda name,body: dict(user_id=123456) if name=='get_login_info' else (_ for _ in ()).throw(TimeoutError())):
                rejects(lambda:q.perform(dict(name='qq_post',content='test',photos=[])))
            w.close();w=Whale(Path(folder),lambda *_: ({"lines":["ok"]},{}),lambda:clock[0]);q=Social(w).qq
            with patch.object(q,'call',api):rejects(lambda:q.perform(dict(name='qq_post',content='test',photos=[])))
            # Received images can reach QQ tools without any Android pairing; only tool results justify success.
            clock[0]+=600;step=[0]
            def model(config,messages):
                step[0]+=1
                if step[0]==1:return dict(lines=[],action=dict(name='qq_post',content='新的海风',photos=['received'])),{}
                assert 'published' in messages[-1]['content'];return dict(lines=['发好啦']),{}
            w.provider=model
            with patch.object(q,'call',api):assert w.chat('发张图','post-test',images=[('image/gif',raw)])['lines']==['发好啦']
            assert effects[-1][1]['ugc_right']==4
        finally:w.close()
    print('PASS: native QQ favorites readback/dedup, durable uncertainty, strict album paths/envelopes, independent space quota/privacy, pause and model tools without Android')
    print("All offline checks passed. Real model, Android lifetime, VM and QQ/WeChat are NOT verified by this check.")


if __name__ == "__main__":
    run()
