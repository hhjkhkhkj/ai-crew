# -*- coding: utf-8 -*-
"""明辨 · 后端   启动:  py server.py

    MODE = "council"  → 会商:几个 agent 商量出个结论
    MODE = "debate"   → 辩论:判据 → 独立提案 → 交叉盘问 → 核证 → 裁决
"""

import json
import queue
import threading
import time
import traceback
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse

import config
import debate
import llm

BASE = Path(__file__).parent
app = FastAPI()


# ============================================================
# 状态:一份历史 + 一堆 SSE 订阅者
# ============================================================
class Hub:
    def __init__(self):
        self.lock = threading.Lock()
        self.history = []
        self.clients = []
        self.busy = False

    def push(self, msg):
        with self.lock:
            msg["id"] = len(self.history)
            msg["ts"] = time.strftime("%H:%M")
            self.history.append(msg)
            subs = list(self.clients)
        for q in subs:
            q.put(msg)

    def snapshot(self):
        with self.lock:
            return list(self.history)

    def subscribe(self):
        q = queue.Queue()
        with self.lock:
            self.clients.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.clients:
                self.clients.remove(q)

    def try_lock_busy(self):
        with self.lock:
            if self.busy:
                return False
            self.busy = True
            return True

    def release(self):
        with self.lock:
            self.busy = False


hub = Hub()


# ============================================================
# 会商模式
# ============================================================
def meeting(goal):
    try:
        hub.push({"who": "system", "text": "会议开始:" + goal})
        for rnd in range(1, config.MAX_ROUNDS + 1):
            for agent in config.AGENTS:
                hub.push({"who": "typing", "name": agent["name"]})
                sp = _council_prompt(agent, goal, hub.history, rnd)
                text = llm.chat(config.COUNCIL_PROVIDER, sp,
                                "轮到你发言了,说:", temperature=0.9,
                                max_tokens=2000, need_json=False)
                for prefix in (agent["name"] + ":", f"【{agent['name']}】"):
                    if text.startswith(prefix):
                        text = text[len(prefix):].strip()
                want_end = "[散会]" in text
                text = text.replace("[散会]", "").strip()
                hub.push({"who": "agent", "name": agent["name"],
                          "emoji": agent["emoji"], "color": agent["color"],
                          "text": text})
                if want_end:
                    hub.push({"who": "system", "text": "散会"})
                    return
        hub.push({"who": "system", "text": f"聊满 {config.MAX_ROUNDS} 轮,强制散会"})
    except Exception as e:
        traceback.print_exc()
        hub.push({"who": "error", "text": f"出错了:{e}"})
    finally:
        hub.release()


def _council_prompt(agent, goal, history, round_no):
    others = "、".join(a["name"] for a in config.AGENTS if a["name"] != agent["name"])
    lines = []
    for m in history:
        if m["who"] == "user":
            lines.append(f"老板:{m['text']}")
        elif m["who"] == "agent":
            lines.append(f"{m['name']}:{m['text']}")
    transcript = "\n".join(lines) or "(还没人说话)"
    return (
        f"你们是一个小团队,在一个群里开会商量事情。\n"
        f"你是「{agent['name']}」。{agent['persona']}\n"
        f"群里的其他人是:{others}。发目标的那个是老板。\n"
        f"老板这次给的目标:{goal}\n\n"
        f"规则:\n"
        f"1. 像真人同事在群里聊天:中文、口语、简短,一条不超过 "
        f"{config.MAX_WORDS} 字,别写小作文。\n"
        f"2. 只说自己的观点,不要复述别人说过的话。\n"
        f"3. 可以直接 @某人 点名。\n"
        f"4. 只输出你要发的这一条消息。不要加引号,不要加「{agent['name']}:」这种前缀。\n"
        f"5. 如果大家已经达成一致、没什么可补充的了,就在消息最后加上 [散会]。\n\n"
        f"现在是第 {round_no} 轮(最多 {config.MAX_ROUNDS} 轮)。\n"
        f"群聊记录:\n{transcript}"
    )


# ============================================================
# 辩论模式
# ============================================================
def run_debate(goal):
    try:
        debate.run(goal, hub)
    except Exception as e:
        traceback.print_exc()
        hub.push({"who": "error", "text": f"辩论中断:{e}"})
    finally:
        hub.release()


# ============================================================
# 路由
# ============================================================
@app.get("/")
def index():
    return HTMLResponse((BASE / "index.html").read_text(encoding="utf-8"))


@app.get("/agents")
def agents():
    if config.MODE == "debate":
        return [{"name": d["name"], "emoji": "⚖", "color": "#6ea8fe",
                 "persona": d["angle"]} for d in config.DEBATERS]
    return config.AGENTS


@app.get("/mode")
def mode():
    return {
        "mode": config.MODE,
        "providers": llm.available(),
        "judge": config.JUDGE.get("provider"),
        "verifier": config.VERIFIER.get("provider"),
        "warnings": llm.preflight(),
    }


@app.get("/stream")
def stream():
    q = hub.subscribe()

    def gen():
        try:
            for m in hub.snapshot():
                yield "data: " + json.dumps(m, ensure_ascii=False) + "\n\n"
            while True:
                try:
                    m = q.get(timeout=15)
                    yield "data: " + json.dumps(m, ensure_ascii=False) + "\n\n"
                except queue.Empty:
                    yield ": ping\n\n"
        finally:
            hub.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.post("/say")
def say(payload: dict):
    text = (payload.get("text") or "").strip()
    if not text:
        return {"ok": False, "msg": "空的"}
    if not llm.available():
        return {"ok": False, "msg": "还没填 API Key:打开 config.py 填上,重启一下"}
    if not hub.try_lock_busy():
        return {"ok": False, "msg": "上一场还没结束,等一下"}

    hub.push({"who": "user", "text": text})
    target = run_debate if config.MODE == "debate" else meeting
    threading.Thread(target=target, args=(text,), daemon=True).start()
    return {"ok": True}


@app.post("/reset")
def reset():
    if hub.busy:
        return {"ok": False, "msg": "还在跑,等它结束"}
    with hub.lock:
        hub.history.clear()
    hub.push({"who": "system", "text": "记录已清空"})
    return {"ok": True}


if __name__ == "__main__":
    print(f"模式: {config.MODE}")
    for w in llm.preflight():
        print("!! " + w)
    print(f"网页地址: http://127.0.0.1:{config.PORT}")
    uvicorn.run(app, host="127.0.0.1", port=config.PORT)
