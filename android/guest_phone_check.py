"""Check the paired phone VM without fixtures, new notes, messages or model charges."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from phone import Phone
from whale import Whale


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path.home()/".littlewhale/device.json")
    parser.add_argument("--adb", default="adb")
    parser.add_argument("--port", type=int, default=5039)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    phone = Phone(json.loads(args.config.read_text("utf-8-sig")), args.adb, args.port)
    status = phone.connect()
    assert status["sdk"] == 25, "This check is for the paired Android 7 VM"
    # Only counts are recorded; existing diary and memory contents stay private.
    counts = {kind: len(phone.notebook(kind).get("entries", [])) for kind in ("diary", "memory")}
    calls = []

    def model(config, messages):
        step = len(calls)
        calls.append(step)
        previous = json.loads(messages[-1]["content"].split("] ", 1)[1]) if step else None
        if step == 0:
            request = dict(name="open", package="cn.littlewhale.memory")
        elif step in (1, 3, 5):
            request = dict(name="observe")
        elif step == 2:
            field = next(n for n in previous["nodes"] if n["editable"] and n["description"] == "搜索记录")
            request = dict(name="tap", screen=previous["screen"], node=field["node"])
        elif step == 4:
            field = next(n for n in previous["nodes"] if n["editable"] and n["focused"])
            request = dict(name="type", screen=previous["screen"], node=field["node"], text="海")
        else:
            assert previous["package"] == "cn.littlewhale.memory"
            return dict(lines=["虚拟机中文搜索测试通过。"]), {"total_tokens": 1}
        return dict(lines=[], action=request), {"total_tokens": 1}

    with tempfile.TemporaryDirectory() as folder:
        whale = Whale(Path(folder)/"check", model)
        whale.paired_phone = lambda **kwargs: phone
        phone.permitted = lambda: not whale.setting("paused", False)
        try:
            event = "guest-ui-check-" + str(time.time_ns())
            reply = whale.chat("在记忆应用的搜索框输入海", event)
            rows = whale.db.execute("SELECT name,status,result FROM phone_actions").fetchall()
            failed = [(r["name"], r["status"], r["result"]) for r in rows if r["status"] != "done"]
            assert not failed, failed
            view = phone.observe(private=True)
            field = next(n for n in view["nodes"] if n["editable"] and n["description"] == "搜索记录")
            assert "海" in field["value"]
            assert len(calls) == 7 and len(rows) == 6 and all(r["status"] == "done" for r in rows)
            assert whale.chat("在记忆应用的搜索框输入海", event) == reply and len(calls) == 7
            phone.perform(dict(name="open", package="cn.littlewhale.diary"), Path(folder)/"downloads")
            assert phone.observe()["package"] == "cn.littlewhale.diary"
            phone.perform(dict(name="home"), Path(folder)/"downloads")
            report = dict(target="paired phone VM", sdk=status["sdk"], size=status["size"],
                          external_model_calls=0, model_stub_calls=len(calls), actions=len(rows),
                          chinese_input_verified=True, diary_open_verified=True,
                          duplicate_did_not_replay=True, notebook_first_page_counts=counts,
                          actual_deepseek_verified=False, checked_at=time.time())
            args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print("PASS: paired phone VM, real app opening, screen observation, Chinese input and no duplicate replay")
        except Exception:
            print("Phone check stopped at:", [(r["name"], r["status"]) for r in whale.db.execute("SELECT name,status FROM phone_actions ORDER BY step")])
            raise
        finally:
            whale.close()


if __name__ == "__main__":
    main()
