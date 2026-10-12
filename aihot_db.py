# -*- coding: utf-8 -*-
"""直连 AIHOT 的库 —— 正文检索 + 取正文。

为什么不用它自带的 HTTP API?实测对比:

    API  q=open source                    ->  0 条
    API  q=agent                          ->  3 条
    库   body_text ILIKE '%open source%'  -> 73 篇
    库   body_text ILIKE '%agent%'        -> 334 篇

API 只认英文标题、短语基本失效;而**正文就躺在库里**,直接搜命中率高几个数量级。

额外的好处:正文在本地 —— 验证器不用出门抓网页了。
"403 / 超时 / 得挂梯子"那一串问题,从根上绕开。

连接信息从 AIHOT 自己的 .env 读,密码不落进我们的代码。
"""

import os
import re

import config

_cache = {}

# 英文虚词 —— 它们在几乎每篇里都有,留着只会稀释命中
_STOP = {
    "the", "and", "for", "with", "that", "this", "from", "are", "was", "were",
    "has", "have", "had", "can", "could", "not", "but", "you", "your", "our",
    "its", "vs", "versus", "than", "into", "over", "about", "how", "why",
    "what", "when", "which", "who", "will", "would", "should", "there",
}


def _tokens(query):
    """把检索词拆成词。

    为什么必须拆 —— 实测踩的坑:ILIKE 要求**连续精确子串**,而模型给的
    往往是一整句话:

        ILIKE '%open source%'                                   -> 命中
        ILIKE '%open source LLM catch up closed source models%' -> 永远 0 条

    所以拆成一个个词、每个都要出现(AND),这才是穷人版全文检索。
    """
    raw = re.split(r"[^A-Za-z0-9一-鿿]+", (query or "").lower())
    out = []
    for t in raw:
        if not t:
            continue
        is_cn = bool(re.search(r"[一-鿿]", t))
        if not is_cn and (len(t) < 3 or t in _STOP):
            continue
        out.append(t)
    return out[:6]


def _read_env(path):
    vals = {}
    if not os.path.isfile(path):
        return vals
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip().strip('"').strip("'")
    return vals


def _dsn():
    if "dsn" in _cache:
        return _cache["dsn"]
    pwd = _read_env(config.AIHOT_ENV).get("POSTGRES_PASSWORD")
    dsn = None
    if pwd:
        dsn = (f"host={config.AIHOT_DB_HOST} port={config.AIHOT_DB_PORT} "
               f"dbname=aihot user=aihot password={pwd}")
    _cache["dsn"] = dsn
    return dsn


def _conn():
    import psycopg2
    return psycopg2.connect(_dsn(), connect_timeout=6)


def available():
    if not _dsn():
        return False
    try:
        c = _conn()
        c.close()
        return True
    except Exception:
        return False


def search(query, limit=6):
    """全文检索。先用 PostgreSQL 的 tsquery(带词干化 + 相关度排序),
    搜不到再退回按词 ILIKE。

    为什么不用单纯的 ILIKE —— 实测出来的:纯 AND 匹配没有相关度概念,
    一篇 4 万字的通讯汇总只要"碰巧包含"那几个词就会被搜出来排在最前。
    ts_rank 才是给"哪篇更相关"打分的那个东西。

    ILIKE 那条留着兜中文:tsquery 的 english 词典处理不了中文词。
    """
    q = (query or "").strip()
    if not q:
        return []

    rows = _search_ts(q, limit)
    if not rows:
        rows = _search_like(q, limit)

    out = []
    for r in rows:
        body = r[4] or ""
        out.append({
            "id": r[0],
            "title": r[1] or "",
            "url": r[2] or "",
            "summary": (r[3] or "")[:400],
            "source": r[6],
            "published": r[5].strftime("%Y-%m-%d") if r[5] else "",
            "has_body": bool(body),
            "body_len": len(body),
        })
    return out


def _search_ts(q, limit):
    sql = """
        select a.id, a.title, a.url, a.excerpt, a.body_text, a.published_at,
               coalesce(s.name, '') as source_name
          from articles a
          left join sources s on s.id = a.source_id
         where to_tsvector('english',
                           coalesce(a.body_text, '') || ' ' || coalesce(a.title, ''))
               @@ plainto_tsquery('english', %s)
         order by ts_rank(to_tsvector('english',
                           coalesce(a.body_text, '') || ' ' || coalesce(a.title, '')),
                          plainto_tsquery('english', %s)) desc,
                  a.published_at desc nulls last
         limit %s
    """
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (q, q, limit))
            return cur.fetchall()
    finally:
        conn.close()


def _search_like(q, limit):
    toks = _tokens(q)
    if not toks:
        return []
    body_cond = " and ".join(["a.body_text ilike %s"] * len(toks))
    title_cond = " and ".join(["a.title ilike %s"] * len(toks))
    sql = f"""
        select a.id, a.title, a.url, a.excerpt, a.body_text, a.published_at,
               coalesce(s.name, '') as source_name
          from articles a
          left join sources s on s.id = a.source_id
         where ({body_cond}) or ({title_cond})
         order by a.published_at desc nulls last
         limit %s
    """
    params = ["%" + t + "%" for t in toks] * 2 + [limit]
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        conn.close()


def body_for_url(url):
    """按地址取库里存的正文。取不到返回空串。

    这是验证器的另一条腿:AIHOT 已经把正文抓好存下来了,
    验证器直接读 —— 不用出门,也就没有 403 / 超时 / 梯子那些事。
    """
    if not url:
        return ""
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "select body_text from articles where url = %s "
                "and body_text is not null order by published_at desc nulls last limit 1",
                (url.strip(),),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    return (row[0] or "") if row else ""
