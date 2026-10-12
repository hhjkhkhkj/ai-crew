# -*- coding: utf-8 -*-
"""证据池:从本地 AIHOT 检索真实文章。

为什么需要它 —— 实测出来的:

    辩手凭记忆给的出处,验证器十有八九打不开。要么写成「去某某官网搜索XX」
    这种指路说明,要么干脆是记错的幻觉地址。结果整个核证层空转,
    裁决只能得出「全场无 L3」。

AIHOT 存的每一条都是**真抓过的文章**,而且 `links.original` 是能直接打开的
原始地址。接上它之后,辩手就从「我记得 WHO 有个指南」变成
「库里这篇真文章说……」—— 证据第一次变成可核验的。
"""

import requests

import config


def available():
    """证据池在线吗。不起 AIHOT 也能跑辩论,只是辩手会退回去靠记忆。"""
    if not config.AIHOT_BASE:
        return False
    try:
        r = requests.get(config.AIHOT_BASE.rstrip("/") + "/api/v1/items",
                         params={"limit": 1}, timeout=6)
        return r.status_code == 200
    except Exception:
        return False


def search(query, limit=None):
    """按关键词检索。

    返回 [{title, summary, url, source, published}]
    url 就是能直接打开的原始地址 —— 辩手引它,验证器抓它,链路才闭合。
    """
    limit = limit or config.EVIDENCE_POOL_SIZE
    r = requests.get(
        config.AIHOT_BASE.rstrip("/") + "/api/v1/items",
        params={"mode": "all", "q": query, "limit": limit},
        timeout=25,
    )
    if r.status_code != 200:
        raise RuntimeError(f"AIHOT 返回 {r.status_code}: {r.text[:120]}")

    out = []
    for it in (r.json().get("items") or []):
        links = it.get("links") or {}
        out.append({
            "title": it.get("title") or "",
            "summary": it.get("summary") or "",
            "url": links.get("original") or "",
            "source": (it.get("source") or {}).get("name") or "",
            "published": (it.get("publishedAt") or "")[:10],
        })
    return [x for x in out if x["url"]]
