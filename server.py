# -*- coding: utf-8 -*-
"""AI 群聊 · 后端   启动:  py server.py"""
import json
import queue
import threading
import time
import traceback
from pathlib import Path

import requests
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse

import config

BASE = Path(__file__).parent
app = FastAPI()


# ============================================================
# 群聊状态:一份历史 + 一堆 SSE 订阅者
# ============================================================
class Hub:
    def __init__(self):
        self.lock = threading.Lock()
        self.history = []      # 所有消息
        self.clients = []      # 每个打开网页的人一个队列
        self.busy = False      # 是否正在开会

    def push(self, msg):
        with self.lock:
            msg["id"] = len(self.history)          # 给每条消息编号,网页那边靠它去重
            msg["ts"] = time.strftime("%H:%M")
            self.history.append(msg)
            subs = list(self.clients)              # 复制一份,别在锁里调用别人
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
        """没人开会就占住,返回 True。"""
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
# 拼提示词 —— 每个 agent 都能看到到目前为止的整份聊天记录
# ============================================================
def build_system_prompt(agent, goal, history, round_no):
    others = "、".join(a["name"] for a in config.AGENTS if a["name"] != agent["name"])
    lines = []
    for m in history:
        if m["who"] == "user":
            lines.append(f"老板:{m['text']}")
        elif m["who"] == "agent":
            lines.append(f"{m['name']}:{m['text']}")
        elif m["who"] == "system":
            lines.append(f"(系统:{m['text']})")
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


def key_ready():
    """密钥填了没 —— 没填就别去调接口,不然只会报一堆看不懂的错。"""
    k = (config.API_KEY or "").strip()
    return len(k) > 20 and "填" not in k


def call_llm(system_prompt, goal):
    r = requests.post(
        config.API_BASE + "/chat/completions",
        headers={
            "Authorization": "Bearer " + config.API_KEY,
            "Content-Type": "application/json",
        },
        json={
            "model": config.MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": "轮到你发言了,说:"},
            ],
            "temperature": 0.9,
            "max_tokens": 400,
        },
        timeout=90,
    )
    if r.status_code != 200:
        raise RuntimeError(f"接口返回 {r.status_code}: {r.text[:200]}")
    return r.json()["choices"][0]["message"]["content"].strip()


# ============================================================
# 开会 —— 在后台线程里按轮次跑
# ============================================================
def meeting(goal):
    try:
        hub.push({"who": "system", "text": "会议开始:" + goal})
        for rnd in range(1, config.MAX_ROUNDS + 1):
            for agent in config.AGENTS:
                hub.push({"who": "typing", "name": agent["name"]})

                sp = build_system_prompt(agent, goal, hub.history, rnd)
                text = call_llm(sp, goal)

                # 去掉模型偶尔自己加的前缀
                for prefix in (agent["name"] + ":", f"【{agent['name']}】", f"[{agent['name']}]"):
                    if text.startswith(prefix):
                        text = text[len(prefix):].strip()

                want_end = "[散会]" in text
                text = text.replace("[散会]", "").strip()

                hub.push({
                    "who": "agent",
                    "name": agent["name"],
                    "emoji": agent["emoji"],
                    "color": agent["color"],
                    "text": text,
                })

                if want_end:
                    hub.push({"who": "system", "text": "散会"})
                    return

        hub.push({"who": "system", "text": f"聊满 {config.MAX_ROUNDS} 轮,强制散会"})
    except Exception as e:
        traceback.print_exc()
        hub.push({"who": "error", "text": f"出错了:{e}"})
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
    return config.AGENTS


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
    if not key_ready():
        return {"ok": False, "msg": "还没填 API Key:打开 config.py 第 5 行填上,重启一下"}
    if not hub.try_lock_busy():
        return {"ok": False, "msg": "还在开会,等这轮聊完"}
    hub.push({"who": "user", "text": text})
    threading.Thread(target=meeting, args=(text,), daemon=True).start()
    return {"ok": True}


@app.post("/reset")
def reset():
    if hub.busy:
        return {"ok": False, "msg": "还在开会,等这轮聊完"}
    with hub.lock:
        hub.history.clear()
    hub.push({"who": "system", "text": "记录已清空"})
    return {"ok": True}


if __name__ == "__main__":
    if not key_ready():
        print("!! 还没填 API Key —— 打开 config.py 第 5 行填上,然后重启")
    print(f"网页地址: http://127.0.0.1:{config.PORT}")
    uvicorn.run(app, host="127.0.0.1", port=config.PORT)
