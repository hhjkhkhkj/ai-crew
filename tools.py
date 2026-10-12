# -*- coding: utf-8 -*-
"""辩手能用的工具 —— 让 AI 自己决定去哪找,而不是我们把路写死。

加新源只做一件事:在这里多写一个函数 + 往 SCHEMA 里加一条声明。
辩手那边一行都不用动。
"""

import evidence_source

# OpenAI 兼容的 tools 声明
SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "search_evidence",
            "description": (
                "在证据库里检索文章,返回标题、摘要,以及**可直接打开的原始地址**。"
                "这是你拿到「可核验出处」的唯一途径 —— 你凭记忆写出来的地址 "
                "验证器多半打不开。检索词**用英文效果最好**。"
                "找不到就换个说法再试,别只搜一次就放弃。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string",
                              "description": "检索词,建议用英文关键词"},
                },
                "required": ["query"],
            },
        },
    },
]


def _search(q, limit=6):
    """先走本地库(正文全文检索,命中率高),库连不上再退回 HTTP API。"""
    try:
        import aihot_db
        if aihot_db.available():
            return aihot_db.search(q, limit=limit), "本地库·正文检索"
    except Exception:
        pass
    return evidence_source.search(q, limit=limit), "HTTP API·标题匹配"


def run(name, args):
    """真去执行一次工具调用,把结果作为文本回填给模型。"""
    args = args or {}

    if name == "search_evidence":
        q = str(args.get("query") or "").strip()
        if not q:
            return "错误:query 是空的。"
        try:
            items, where = _search(q)
        except Exception as e:
            return f"检索失败:{e}"

        if not items:
            return (f"检索「{q}」没找到任何文章。"
                    f"换个更宽泛或更常见的英文词再试一次。")

        lines = [f"(检索途径:{where})"]
        for i, it in enumerate(items, 1):
            lines.append(f"[{i}] {it['title']}")
            if it.get("summary"):
                lines.append(f"    摘要:{it['summary'][:200]}")
            n = it.get("body_len") or 0
            # 把正文长度摆出来 —— 语料里有一批三四万字的「周报汇总」,
            # 它们什么都提一句、所以什么搜索都命中。让模型自己看着办。
            mark = ""
            if it.get("has_body"):
                mark = f"  ★正文已在本地({n}字)"
                if n > 25000:
                    mark += " —— 篇幅很大,可能是资讯汇总,引用前先确认它真在讲这个题"
            lines.append(f"    来源:{it.get('source','')} {it.get('published','')}{mark}")
            lines.append(f"    地址:{it['url']}")
        return "\n".join(lines)

    return f"没有这个工具:{name}"
