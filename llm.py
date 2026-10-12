# -*- coding: utf-8 -*-
"""模型调用 —— 按供应商分发。

辩论要求不同座位走**不同家族**,所以这里不能只有一个全局 MODEL。
"""

import json
import re

import requests

import config


class LLMError(RuntimeError):
    pass


def key_ok(key):
    k = (key or "").strip()
    return len(k) > 10 and "填" not in k


def available():
    """哪些供应商配好了 key。"""
    return [name for name, p in config.PROVIDERS.items() if key_ok(p.get("key"))]


def chat(provider, system, user, temperature=0.7, max_tokens=4000,
         need_json=True, timeout=300, _tries=0):
    """按供应商调模型。每个座位可以走不同的家。

    关于 max_tokens:它是**上限,不是配额** —— 不按上限收费,只按实际生成的收。
    所以宁可设大。设小了会踩这个坑:

        重推理模型(deepseek-flash 这类)会把额度**全花在 reasoning 上**,
        正文一个字都剩不下,返回空字符串,finish_reason="length"。
        实测 deepseek-flash 给 1600 → content 为空;给 4000 → 正常出结果。

    所以这里带两层自愈:
      1. 正文空了 / 需要 JSON 但被截断了 → 加预算重来
      2. 网络超时 → 重来一次(大模型偶尔会卡很久,不能让它拖垮整场)
    """
    p = config.PROVIDERS.get(provider)
    if not p:
        raise LLMError(f"没有配置供应商 {provider}")
    if not key_ok(p.get("key")):
        raise LLMError(f"供应商 {provider} 还没填 key")

    try:
        r = requests.post(
            p["base"].rstrip("/") + "/chat/completions",
            headers={
                "Authorization": "Bearer " + p["key"].strip(),
                "Content-Type": "application/json",
            },
            json={
                "model": p["model"],
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": temperature,
                "max_tokens": max_tokens,
                # 供应商专属参数从 config 里透传(比如调思考档位压时间)
                **(p.get("extra") or {}),
            },
            timeout=timeout,
        )
    except requests.exceptions.RequestException as e:
        if _tries < 1:
            return chat(provider, system, user, temperature, max_tokens,
                        need_json, timeout, _tries + 1)
        raise LLMError(f"{provider}({p['model']}) 请求失败:{e}")

    if r.status_code != 200:
        raise LLMError(f"{provider} 返回 {r.status_code}: {r.text[:200]}")

    data = r.json()
    choice = data["choices"][0]
    text = (choice["message"].get("content") or "").strip()
    truncated = choice.get("finish_reason") == "length"

    # 空正文,或者要 JSON 却被截断(半截 JSON 等于没有)—— 都加预算重来
    if (not text or (need_json and truncated)) and _tries < 2:
        return chat(provider, system, user, temperature, max_tokens * 3,
                    need_json, timeout, _tries + 1)

    if not text:
        used = (data.get("usage") or {}).get("completion_tokens_details", {}) or {}
        raise LLMError(
            f"{provider}({p['model']}) 连续 {_tries + 1} 次返回空内容"
            f"(thinking 吃掉了 {used.get('reasoning_tokens', 0)} token)。"
            f"换个模型,或把 max_tokens 再调大。"
        )
    return text


def repair_json(provider, raw, max_tokens=4000):
    """把一段不合法 JSON 丢回模型让它整理。整理不出来返回 None。"""
    raw2 = chat(provider, "你是一个 JSON 格式化器。只输出合法 JSON,不要任何别的话。",
                "下面这段不是合法 JSON。请**只**把它整理成合法 JSON,"
                "内容一个字都不要改,不要加解释、不要加代码块标记:\n\n" + raw[:6000],
                temperature=0, max_tokens=max_tokens, need_json=True)
    return parse_json(raw2)


def chat_with_tools(provider, system, user, tools, run_tool,
                    max_rounds=8, max_tool_calls=4, temperature=0.6,
                    max_tokens=4000, on_tool=None):
    """带工具循环的对话 —— 让 AI **自己决定**去找什么。

    流程:模型说要调工具 → 我们真去调 → 结果回填 → 再问一轮,
    直到它不再调工具、直接给出答案。

    这一层是「固定管线」和「agent」的分界线:以前是我们在代码里写死
    "去 AIHOT 搜一次",现在是模型自己决定搜什么词、搜几次。

    两道代码闸门(不靠提示词自觉):
      · max_rounds      —— 来回最多几轮
      · max_tool_calls  —— 总共最多真执行几次工具。超了就直接回它
        "配额用完了",逼它基于已有信息收手。实测模型会一路退到搜
        「the」「AI」这种废词死磕,必须由代码拦住。

    返回 (文本, 调用记录)。
    """
    p = config.PROVIDERS.get(provider)
    if not p:
        raise LLMError(f"没有配置供应商 {provider}")
    if not key_ok(p.get("key")):
        raise LLMError(f"供应商 {provider} 还没填 key")

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    calls = []
    used = 0

    for _ in range(max_rounds):
        r = requests.post(
            p["base"].rstrip("/") + "/chat/completions",
            headers={
                "Authorization": "Bearer " + p["key"].strip(),
                "Content-Type": "application/json",
            },
            json={
                "model": p["model"],
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "tools": tools,
                **(p.get("extra") or {}),
            },
            timeout=300,
        )
        if r.status_code != 200:
            raise LLMError(f"{provider} 返回 {r.status_code}: {r.text[:200]}")

        msg = r.json()["choices"][0]["message"]
        tool_calls = msg.get("tool_calls") or []

        if not tool_calls:
            text = (msg.get("content") or "").strip()
            if not text:
                raise LLMError(
                    f"{provider}({p['model']}) 在工具循环里返回了空正文 —— "
                    f"多半是推理把额度吃光了。"
                )
            return text, calls

        # 把这一轮(含它的工具调用意图)原样记回对话,否则模型会"忘了自己叫过工具"
        messages.append({
            "role": "assistant",
            "content": msg.get("content") or "",
            "tool_calls": tool_calls,
        })

        for tc in tool_calls:
            fn = (tc.get("function") or {}).get("name") or ""
            if used >= max_tool_calls:
                result = (f"工具调用配额用完了(每轮辩论最多 {max_tool_calls} 次检索)。"
                          f"**不要再请求检索** —— 立刻基于你已经拿到的信息给出最终答案,"
                          f"手头没有证据的部分就老实标 L0。")
            else:
                try:
                    args = json.loads((tc.get("function") or {}).get("arguments") or "{}")
                except Exception:
                    args = {}
                result = run_tool(fn, args)
                used += 1
                calls.append({"tool": fn, "args": args})
                if on_tool:
                    on_tool(fn, args, result)

            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id") or fn,
                "content": result,
            })

    raise LLMError(f"{provider} 工具循环跑了 {max_rounds} 轮还没收手,掐掉")


def parse_json(text, fallback=None):
    """模型爱在 JSON 外面套 ``` —— 剥掉。剥不出来返回 fallback,不抛异常。"""
    s = (text or "").strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    try:
        return json.loads(s)
    except Exception:
        pass
    m = re.search(r"\{.*\}", s, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return fallback


def chat_json(provider, system, user, temperature=0.6, max_tokens=4000):
    """要 JSON 的调用。

    第一次没解析出来时,把模型**自己的原文**丢回去让它整理一遍 ——
    比重新问一遍便宜(不用重新推理),而且不丢它已经想好的内容。

    两次都失败返回 None,由调用方决定怎么办。
    """
    raw = chat(provider, system, user, temperature=temperature,
               max_tokens=max_tokens, need_json=True)
    data = parse_json(raw)
    if data is not None:
        return data

    raw2 = chat(
        provider,
        "你是一个 JSON 格式化器。只输出合法 JSON,不要任何别的话。",
        "下面这段不是合法 JSON。请**只**把它整理成合法 JSON,"
        "内容一个字都不要改,不要加解释、不要加代码块标记:\n\n" + raw[:6000],
        temperature=0, max_tokens=max_tokens, need_json=True,
    )
    return parse_json(raw2)


def preflight():
    """启动前体检。返回一串警告(空列表 = 没问题)。"""
    warns = []
    ok = available()
    if not ok:
        return ["一个供应商都没填 key —— 什么都跑不了。"]

    # 「是不是真异构」看**家族**,不看供应商。
    # 聚合平台上"一个 key 调好几家模型"仍然算异构 —— 供应商字段一样,家族不同。
    fams = [d.get("family") or d.get("provider") for d in config.DEBATERS]
    if len(fams) != len(set(fams)):
        warns.append(
            "两个辩手是同一个模型家族 —— 这不是辩论,是同一个模型的自问自答。"
            "同一个偏差会被互相确认,得到一个比单个模型更自信的错误。"
        )

    # 「key 填没填」看供应商。
    for d in config.DEBATERS:
        if d.get("provider") not in ok:
            warns.append(f"辩手 {d.get('name')} 所在的供应商 "
                         f"[{d.get('provider')}] 没填 key。")

    for role, seat in (("判官", config.JUDGE), ("验证器", config.VERIFIER)):
        fam = seat.get("family") or seat.get("provider")
        if fam in fams:
            warns.append(f"{role}和辩手同家族 [{fam}] —— 它会有共享偏差,"
                         f"会'确认'辩手的错误而不是查它。")
        if seat.get("provider") not in ok:
            warns.append(f"{role}所在的供应商 [{seat.get('provider')}] 没填 key。")
    return warns
