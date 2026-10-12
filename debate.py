# -*- coding: utf-8 -*-
"""辩论流程。

    Phase 0  判据       —— 辩之前先把"什么算好"定死,全场共用,中途不许改
    Phase 1  独立提案   —— 背对背,互相看不见(最关键的一条)
    Phase 2  交叉盘问   —— 只盘真分歧;匿名;强制 pros + cons
    Phase 3  核证       —— 先代码判同源,再让验证器真去打开来源
    Phase 4  裁决       —— 按证据质量(不按票数);分三堆,保留异议
"""

import config
import evidence as ev
import evidence_source
import llm
import tools


class DebateError(RuntimeError):
    pass


# 权重的闸门:升上去也要能降下来,否则先开口的滚雪球
WEIGHT_START = 1.0
WEIGHT_CAP = 2.0
WEIGHT_DECAY = 0.9          # 每轮乘一次


def _w(v):
    return round(min(v, WEIGHT_CAP), 2)


# ============================================================
# Phase 0 · 判据
# ============================================================
def phase0_criteria(goal, hub):
    """判据必须在开辩之前定好,且全场共用 —— 否则辩论会变成
    "重新定义什么算好"的文字游戏。"""
    hub.push({"who": "phase", "n": 0, "title": "定判据",
              "note": "先定'什么算好',全场共用,中途不许改"})

    system = (
        "你是这场辩论的出题人。你的唯一任务是:在辩论开始**之前**,"
        "把'什么算好'定死 —— 列出这次要比的维度。\n"
        "不要给结论,不要站队。只输出 JSON。"
    )
    user = (
        f"议题:{goal}\n\n"
        "输出 JSON:\n"
        "{\n"
        '  "criteria": [{"name": "维度名", "desc": "这个维度上怎么算更好"}],\n'
        '  "background": "辩论前需要知道的背景与约束",\n'
        '  "search_terms": ["2-3 个检索词"]\n'
        "}\n"
        "维度控制在 3-4 个。\n"
        "**search_terms 必须用英文** —— 证据库是按英文原标题建索引的,"
        "中文关键词搜不出东西。"
    )
    data = llm.chat_json(config.JUDGE["provider"], system, user,
                         temperature=0.3, max_tokens=2500) \
        or {"criteria": [], "background": ""}
    hub.push({"who": "criteria", "data": data})
    return data


# ============================================================
# Phase 0.5 · 捞证据池
#
# 这一步是为了堵住实测出来的那个洞:辩手凭记忆给的出处十有八九打不开,
# 核证层空转。接上本地 AIHOT 之后,辩手第一次能拿到**真能打开的地址**。
# ============================================================
def phase0_5_pool(goal, crit, hub):
    if not config.AIHOT_BASE:
        return []
    hub.push({"who": "phase", "n": 0.5, "title": "捞证据池",
              "note": "从本地 AIHOT 检索真文章给辩手当弹药"})

    # 用判据阶段产出的**英文**检索词 —— 证据库按英文原标题建索引,中文搜不到。
    terms = [str(t).strip() for t in (crit.get("search_terms") or []) if str(t).strip()][:3]
    if not terms:
        terms = [goal]

    seen, items, errors = set(), [], []
    for t in terms:
        try:
            for it in evidence_source.search(t, limit=config.EVIDENCE_POOL_SIZE):
                if it["url"] in seen:
                    continue
                seen.add(it["url"])
                items.append(it)
        except Exception as e:
            errors.append(str(e))

    if not items:
        why = errors[0] if errors else "搜不到相关文章(可能是题不匹配库里语料)"
        hub.push({"who": "warn",
                  "text": f"证据池是空的:{why} —— 这一场辩手只能靠记忆,证据会偏虚"})
        return []

    hub.push({"who": "pool", "items": items, "terms": terms})
    return items


def _pool_text(items):
    if not items:
        return ("(证据池是空的 —— 你只能靠自己的判断。那就老实标 L0/L1,"
                "**别编一个看起来像真的地址**。)")
    lines = ["检索到的真文章(**这些地址验证器能打开,优先用它们**):"]
    for i, it in enumerate(items, 1):
        lines.append(f"  [{i}] {it['title']}")
        if it.get("summary"):
            lines.append(f"      {it['summary'][:140]}")
        lines.append(f"      来源:{it.get('source','')} {it.get('published','')}")
        lines.append(f"      {it['url']}")
    return "\n".join(lines)


# ============================================================
# Phase 1 · 独立提案(背对背)
# ============================================================
PROPOSER_SYSTEM = """你是辩论的一位辩手。现在轮到你**独立**陈述立场。

最要紧的规则:**你看不到其他辩手说了什么,也不许猜。** 这不是限制,是这场辩论的全部意义 ——
如果辩手互相看得见,大家会朝第一个开口的人靠拢,辩论就退化成附和。

证据必须标级别,不许虚报:
- L0 断言:只有你的判断,没有出处
- L1 引用:你说某处提过,但给不出可核查的位置
- L2 可核验:给了**具体出处**(文件路径+行号 / URL),别人能照着去查

**关于 source 字段 —— 这条最容易被忽略:**

它必须是一个**机器能直接打开的地址**。

  ✅ 完整的 https:// 链接
  ✅ 本地文件路径(带行号更好,如 D:\\a\\b.py#L40)
  ❌ 「某某官网上搜索XX」「相关报道」「行业调查」—— 这是**给人看的指路说明**,
     验证器照着它打不开任何东西,这条证据会被**自动降级**

**你有一个工具:search_evidence(query)**

用它去**自己找证据**,不要凭记忆写地址。

- 检索词**用英文**效果最好(证据库按英文标题建索引,中文搜不到)
- **先搜,再写。** 不要一上来就凭记忆答题
- **最多搜 4 次。** 证据库很小,搜不到是常态 —— 别一路退到搜「the」「AI」
  这种废词死磕。搜不到就老实标 L0,那不算输
- 搜到了文章,引用时**必须用它给的那个地址**

你搜了什么、搜到没搜到,都会被记下来。

给不出直达地址,就老实标 L1。**别用一段话冒充地址。**

只输出 JSON,不要别的话。"""


def phase1_proposals(goal, crit, pool, hub):
    hub.push({"who": "phase", "n": 1, "title": "独立提案",
              "note": "背对背 —— 每个辩手都看不到别人说了什么"})

    crit_txt = _criteria_text(crit)
    pool_txt = _pool_text(pool)
    proposals = []

    for i, seat in enumerate(config.DEBATERS):
        label = chr(ord("A") + i)
        user = (
            f"议题:{goal}\n\n"
            f"{crit_txt}\n\n"
            f"{pool_txt}\n\n"
            f"你的角度:{seat['angle']}\n\n"
            "输出 JSON:\n"
            "{\n"
            '  "conclusion": "你的结论,一句话",\n'
            '  "evidence": [\n'
            '    {"level": "L2", "what": "证据说了什么", "source": "路径#L40 或 URL"}\n'
            "  ],\n"
            '  "checked": ["你真去看过/跑过的东西"],\n'
            '  "confidence": "high | medium | low",\n'
            '  "unknowns": ["你不知道、也查不到的东西"]\n'
            "}"
        )
        def on_tool(fn, args, result, _seat=seat, _label=label):
            hub.push({"who": "tool", "seat": _seat["name"], "label": _label,
                      "tool": fn, "args": args,
                      "empty": "没找到任何文章" in result})

        try:
            raw, calls = llm.chat_with_tools(
                seat["provider"], PROPOSER_SYSTEM, user,
                tools=tools.SCHEMA, run_tool=tools.run,
                temperature=0.6, max_tokens=4000, on_tool=on_tool,
            )
        except llm.LLMError as e:
            raise DebateError(f"辩手 {seat['name']} 出错:{e}")

        p = llm.parse_json(raw) or llm.repair_json(seat["provider"], raw)
        if not p:
            raise DebateError(f"辩手 {seat['name']} 两次都没吐出合法 JSON")

        p["label"] = label
        p["seat"] = seat["name"]
        p["provider"] = seat["provider"]
        p["weight"] = WEIGHT_START
        p["evidence"] = _clean_evidence(p.get("evidence"))
        proposals.append(p)

        hub.push({"who": "proposal", "label": label, "seat": seat["name"],
                  "angle": seat["angle"], "conclusion": p.get("conclusion", ""),
                  "evidence": p["evidence"], "checked": p.get("checked", []),
                  "confidence": p.get("confidence", "?"),
                  "unknowns": p.get("unknowns", [])})

    return proposals


# ============================================================
# Phase 2 · 交叉盘问
# ============================================================
CRITIC_SYSTEM = """你在参加一场辩论的第二轮。现在你能看到**别人**的提案了。

三条硬规则:

1. **只准对证据开火,不准对结论开火。**
   "你说的第 3 条我查不到出处,请给出可核查的位置" —— 合法。
   "我不同意你的结论" —— 废话,不接受。

2. **每条都必须先写 pros 再写 cons。**
   说不出对方哪里强,说明你没认真看。只喷不认账的一律作废。

3. **要改结论,必须给出新证据。**
   没有新证据的让步算"趋同"(被对方的气场带走),会被单独记一笔。
   新证据的 source 同样必须是**机器能直接打开的地址**(完整 URL 或文件路径),
   写「某某官网搜索XX」会被自动降级。

只输出 JSON,不要别的话。"""


def phase2_cross_critique(goal, crit, proposals, hub):
    hub.push({"who": "phase", "n": 2, "title": "交叉盘问",
              "note": "匿名;只对证据开火;强制 pros + cons"})

    all_new_evidence = []

    for rnd in range(1, config.MAX_ROUNDS + 1):
        revised_this_round = 0
        new_count = 0

        for p in proposals:
            others = [
                {"label": q["label"], "conclusion": q.get("conclusion", ""),
                 "evidence": q.get("evidence", [])}
                for q in proposals if q is not p
            ]
            # 匿名:只给 label,不给名字和供应商
            user = (
                f"议题:{goal}\n\n{_criteria_text(crit)}\n\n"
                f"你是提案 {p['label']} 的作者。你的结论:{p.get('conclusion','')}\n"
                f"你自己的证据:{_evidence_text(p.get('evidence'))}\n\n"
                f"其他提案(作者已隐去):\n{_others_text(others)}\n\n"
                "输出 JSON:\n"
                "{\n"
                '  "critiques": [\n'
                '    {"target": "B",\n'
                '     "pros": ["它哪里对、哪里比我的强"],\n'
                '     "cons": ["风险、边界情况、隐藏成本、错误假设"]}\n'
                "  ],\n"
                '  "revised": false,\n'
                '  "revised_reason": "如果改了结论,这里写你被哪条证据说服了",\n'
                '  "new_evidence": [\n'
                '    {"level":"L2","what":"...","source":"...","kills":"它推翻了谁的哪条推理"}\n'
                "  ]\n"
                "}"
            )
            data = llm.chat_json(p["provider"], CRITIC_SYSTEM, user,
                                 temperature=0.6, max_tokens=4000)
            if not data:
                hub.push({"who": "system",
                          "text": f"提案 {p['label']} 这轮两次都没吐出合法 JSON,跳过"})
                continue

            new_ev = _clean_evidence(data.get("new_evidence"))
            if data.get("revised"):
                revised_this_round += 1
            new_count += len(new_ev)
            all_new_evidence.extend(new_ev)

            hub.push({"who": "critique", "round": rnd, "label": p["label"],
                      "critiques": data.get("critiques", []),
                      "revised": bool(data.get("revised")),
                      "revised_reason": data.get("revised_reason", ""),
                      "new_evidence": new_ev})

        # ---- 收敛判据(比硬砍轮数更早触发)----
        if config.STOP_IF_NO_NEW_EVIDENCE and new_count == 0:
            hub.push({"who": "system",
                      "text": f"第 {rnd} 轮没有任何新证据 —— 再辩也没意义,停。"})
            break
        hub.push({"who": "system",
                  "text": f"第 {rnd} 轮:{new_count} 条新证据,{revised_this_round} 人改了立场"})

        # 权重衰减 —— 防先开口的滚雪球
        for p in proposals:
            p["weight"] = _w(p["weight"] * WEIGHT_DECAY)

    return all_new_evidence


# ============================================================
# Phase 3 · 核证
# ============================================================
VERIFIER_SYSTEM = """你是一个独立的核验员。你的工作**不是**判断谁说得对,
是判断**这些声称站不站得住**。

你会拿到:若干条声称(它们都引用同一个来源),以及我按那个出处**真去取回来的原文**。

对每一条分别回三种结论之一:
- "verified"    —— 原文确实支持这条声称
- "refuted"    —— 原文和这条对不上(包括:原文里根本没有这回事)
- "unverifiable" —— 原文里找不到相关信息,判不了

**原文里没写就是没写,别替它圆。** 编一个结论是这份工作里最严重的失职。
只输出 JSON。"""


def _verify_group(items, got):
    """一个来源下挂着的所有声称,一次判完。返回 {条目 id: (结论, 理由)}。

    为什么要成批:实测一场辩论里同一个 arxiv 链接被引了 6 次 —— 逐个核会把
    同一个地址抓 6 遍、问 6 遍。成批之后是「一个来源 = 一次抓取 + 一次判定」。
    """
    lines = [f"[{i}] {e.get('what', '')}" for i, e in enumerate(items, 1)]
    user = (
        f"下面 {len(items)} 条声称,都引用同一个来源:\n"
        + "\n".join(lines) + "\n\n"
        f"我按出处真去取回来的原文:\n---\n{got['text']}\n---\n"
        f"(来源:{items[0].get('source', '')}  取回说明:{got['note']})\n\n"
        '输出 JSON:{"results":[{"i":1,"status":"verified|refuted|unverifiable",'
        '"why":"一句话理由"}]}'
    )
    v = llm.chat_json(config.VERIFIER["provider"], VERIFIER_SYSTEM, user,
                      temperature=0.1, max_tokens=2500)

    out = {}
    if isinstance(v, dict):
        for r in (v.get("results") or []):
            try:
                idx = int(r.get("i")) - 1
            except Exception:
                continue
            if 0 <= idx < len(items):
                st = r.get("status", "unverifiable")
                if st not in ("verified", "refuted", "unverifiable"):
                    st = "unverifiable"
                out[id(items[idx])] = (st, r.get("why", ""))
    return out


def _verify_one_source(key, items):
    """核一个来源 —— 下面可能挂着好几条声称。"""
    got = ev.fetch_source(items[0].get("source"), timeout=config.VERIFY_TIMEOUT)

    if not got["ok"]:
        return [(e, "unverifiable", got["note"]) for e in items], got["note"]

    verdicts = _verify_group(items, got)
    res = []
    for e in items:
        st, why = verdicts.get(id(e), ("unverifiable", "核验员没给出这条的结论"))
        res.append((e, st, why))
    return res, got["note"]


def phase3_verify(goal, proposals, new_evidence, hub):
    from concurrent.futures import ThreadPoolExecutor, as_completed

    hub.push({"who": "phase", "n": 3, "title": "核证",
              "note": "按来源去重,一个来源只抓一次、只判一次;多个来源并发"})

    # ---- 第一层:纯代码判同源,零成本 ----
    everything = []
    for p in proposals:
        everything.extend(p.get("evidence", []))
    everything.extend(new_evidence)

    groups = ev.group_evidence(everything)
    hub.push({"who": "independence",
              "report": ev.independence_report(everything),
              "score": ev.weighted_score(groups),
              "note": "同源的证据合并取 max,不累加 —— 同一来源被引两次,"
                      "不是两个证据,是一个证据被数了两遍"})

    # ---- 按来源归堆 ----
    # 这是这一版的关键改动:以前是「每条证据核一次」,同一个地址被引 6 次就
    # 抓 6 遍、问 6 遍。现在按出处身份归堆,一个来源 = 一次抓取 + 一次判定。
    buckets = {}
    for e in everything:
        if e.get("level") != ev.L2:
            continue
        k = ev.source_identity(e.get("source")) or e.get("source")
        buckets.setdefault(k, []).append(e)

    todo = list(buckets.items())[:config.VERIFY_MAX_SOURCES]
    if len(buckets) > config.VERIFY_MAX_SOURCES:
        hub.push({"who": "system",
                  "text": f"共 {len(buckets)} 个来源待核,这趟只核前 "
                          f"{config.VERIFY_MAX_SOURCES} 个(防跑飞)"})
    hub.push({"who": "system",
              "text": f"归堆后:{len(everything)} 条声称 → {len(todo)} 个来源,"
                      f"{config.VERIFY_WORKERS} 路并发核"})

    results = []
    with ThreadPoolExecutor(max_workers=config.VERIFY_WORKERS) as pool:
        futures = [pool.submit(_verify_one_source, k, items) for k, items in todo]
        for fut in as_completed(futures):
            try:
                verdicts, note = fut.result()
            except Exception as ex:
                hub.push({"who": "verify", "status": "unverifiable",
                          "note": f"核验线程出错:{ex}"})
                continue
            for e, status, why in verdicts:
                if status == "verified":
                    e["level"] = ev.L3          # 升格:这才算事实
                e["verify"] = {"status": status, "why": why, "note": note}
                hub.push({"who": "verify", "source": e.get("source"),
                          "what": e.get("what"), "status": status, "why": why,
                          "from": e.get("from")})
                results.append(e)

    return everything, results


# ============================================================
# Phase 4 · 裁决
# ============================================================
JUDGE_SYSTEM = """你是这场辩论的判官。

你的职责**不是**挑一个你个人认同的结论,是**按证据质量**判断哪个结论站得住。

排序只按这个,不看语气、不看篇幅、不看谁说得更确定:

    L3 已核验  >  L2 可核验  >  L1 引用  >  L0 断言

**不按票数。** 两个人都说了同一件事,但如果他们引的是同一份来源,那还是**一个**证据。

裁决必须**分三堆**,不许硬凑一个答案:
- 已证实:有 L3 撑着的
- 已推翻:被核验员打回的
- 无法核查:谁都验不了的(明说,别编)

另外必须**保留异议** —— 有人不同意、且没被说服,照实写下来,不要抹平。

只输出 JSON。"""


def phase4_verdict(goal, crit, proposals, all_evidence, hub):
    hub.push({"who": "phase", "n": 4, "title": "裁决",
              "note": "按证据质量排,不按票数;分三堆;保留异议"})

    # 按证据等级给每个提案算分
    ranked = []
    for p in proposals:
        g = ev.group_evidence(p.get("evidence", []))
        ranked.append({
            "label": p["label"], "seat": p["seat"],
            "conclusion": p.get("conclusion", ""),
            "score": ev.weighted_score(g),
            "weight": p.get("weight", WEIGHT_START),
            "l3": len([e for e in p.get("evidence", []) if e.get("level") == ev.L3]),
        })
    ranked.sort(key=lambda x: (x["score"], x["weight"]), reverse=True)

    user = (
        f"议题:{goal}\n\n{_criteria_text(crit)}\n\n"
        f"各提案(按证据分排好序):\n{_ranked_text(ranked)}\n\n"
        f"全部证据(含核验结果):\n{_evidence_detail(all_evidence)}\n\n"
        "输出 JSON:\n"
        "{\n"
        '  "verdict": "一句话结论",\n'
        '  "picked": "A 或 B 或 hybrid",\n'
        '  "why": "为什么这个结论胜出 —— 绑到证据上,不绑到措辞上",\n'
        '  "confirmed": ["有 L3 支撑的事实"],\n'
        '  "refuted": ["被核验员打回的"],\n'
        '  "unverifiable": ["查不了的 —— 明说,别编"],\n'
        '  "dissent": ["有人不同意且没被说服的"],\n'
        '  "confidence": "high | medium | low"\n'
        "}"
    )
    v = llm.chat_json(config.JUDGE["provider"], JUDGE_SYSTEM, user,
                      temperature=0.2, max_tokens=4000)
    if not v:
        raise DebateError("判官两次都没吐出合法 JSON")

    v["ranking"] = ranked
    hub.push({"who": "verdict", "data": v})
    return v


# ============================================================
# 主流程
# ============================================================
def run(goal, hub):
    warns = llm.preflight()
    for w in warns:
        hub.push({"who": "warn", "text": w})
    if any("都没填" in w or "没填 key" in w for w in warns):
        hub.push({"who": "error", "text": "有座位没配好,先去看 config.py"})
        return

    hub.push({"who": "system", "text": f"辩论开始:{goal}"})

    crit = phase0_criteria(goal, hub)
    pool = phase0_5_pool(goal, crit, hub) if config.PREFETCH_POOL else []
    proposals = phase1_proposals(goal, crit, pool, hub)
    new_ev = phase2_cross_critique(goal, crit, proposals, hub)
    all_ev, _ = phase3_verify(goal, proposals, new_ev, hub)
    phase4_verdict(goal, crit, proposals, all_ev, hub)

    hub.push({"who": "system", "text": "辩论结束"})


# ============================================================
# 拼文本的小工具
# ============================================================
def _criteria_text(crit):
    if not crit or not crit.get("criteria"):
        return "判据:(没定出来)"
    lines = ["判据(全场共用,不许改):"]
    for c in crit["criteria"]:
        lines.append(f"  · {c.get('name','')} —— {c.get('desc','')}")
    if crit.get("background"):
        lines.append(f"背景:{crit['background']}")
    return "\n".join(lines)


def _evidence_text(evs):
    if not evs:
        return "  (没有)"
    return "\n".join(
        f"  [{e.get('level','?')}] {e.get('what','')}  (出处:{e.get('source','')})"
        for e in evs
    )


def _others_text(others):
    out = []
    for o in others:
        out.append(f"提案 {o['label']} 的结论:{o['conclusion']}")
        out.append(_evidence_text(o.get("evidence")))
        out.append("")
    return "\n".join(out)


def _ranked_text(ranked):
    return "\n".join(
        f"  {r['label']}({r['seat']}) 证据分 {r['score']} 权重 {r['weight']} "
        f"L3 条数 {r['l3']} —— {r['conclusion']}"
        for r in ranked
    )


def _evidence_detail(items):
    out = []
    for e in items:
        v = e.get("verify") or {}
        st = v.get("status", "-")
        out.append(f"  [{e.get('level','?')}/{st}] {e.get('what','')} "
                   f"(出处:{e.get('source','')})")
    return "\n".join(out) or "  (没有)"


def _clean_evidence(items):
    """把模型吐出来的证据规整一遍:等级归一 + 挡掉虚报。

    两道**代码闸门**,不靠提示词自觉:
      1. 声称可核验、却没给出处        → 打回 L0(这是包装)
      2. 给了出处、但机器打不开        → 降为 L1(这是指路说明,不是地址)
    """
    out = []
    for e in (items or []):
        if not isinstance(e, dict):
            continue
        what = str(e.get("what") or "").strip()
        src = str(e.get("source") or "").strip()
        lvl = ev.normalize_level(e.get("level"))
        note = ""

        if lvl in (ev.L2, ev.L3):
            if not src:
                lvl, note = ev.L0, "声称可核验却没给出处 → 打回 L0"
            elif not ev.looks_openable(src):
                lvl, note = ev.L1, "出处机器打不开(写的是「去哪找」而不是地址)→ 降为 L1"

        if not what:
            continue
        item = {"level": lvl, "what": what, "source": src}
        if note:
            item["note"] = note
        if e.get("kills"):
            item["kills"] = str(e["kills"])
        item["from"] = str(e.get("from") or "")
        out.append(item)
    return out
