# -*- coding: utf-8 -*-
"""证据层:分级 / 独立性校验 / 权重合并 / 取原文。

辩论版的全部价值压在这几件事上 ——
没有它们,多 agent 辩论就只是"几个 AI 吵架,谁文笔好谁赢"。
"""

import os
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import config

# ============================================================
# 证据分级
# ============================================================
L0, L1, L2, L3 = "L0", "L1", "L2", "L3"

LEVEL_NAME = {L0: "断言", L1: "引用", L2: "可核验", L3: "已核验"}

# 权重:判官排序用。注意 L3 是 L2 的升级,不是另一种证据。
LEVEL_WEIGHT = {L0: 0.0, L1: 0.2, L2: 0.6, L3: 1.0}

LEVEL_DESC = {
    L0: "只有判断,没有出处",
    L1: "说某处提过,但给不出可核查的位置",
    L2: "给了具体出处(文件+行号 / URL),别人能照着去查",
    L3: "验证器真去打开了来源,对上了",
}


def weight_of(level):
    return LEVEL_WEIGHT.get(level, 0.0)


def normalize_level(x):
    """模型有时候会写 l2 / "L2" / "级别2" —— 统一一下。"""
    s = str(x or "").strip().upper()
    m = re.search(r"L?([0-3])", s)
    return "L" + m.group(1) if m else L0


# ============================================================
# 第一层:出处规范化 —— 纯代码,零成本,不会出错
# ============================================================
_TRACKING = ("utm_", "spm", "share_", "ref", "from", "source", "fbclid")

# 出处里可能混着标点和中文,URL 要按字符类边界抠
_URL_RE = re.compile(r"https?://[^\s,，、;；\"'<>()\[\]{}]+")
_PATH_RE = re.compile(r"^[A-Za-z0-9_./\\-]+\.[A-Za-z0-9]{1,6}(?:#L?\d+(?:-\d+)?)?$")


def extract_address(src):
    """从出处里把「机器能打开的那部分」抠出来。

    实测踩到的:模型给出处经常是**文献名和 URL 混在一起** ——
        CNNIC《第53次中国互联网络发展状况统计报告》,https://www.cnnic.net.cn/xxx
    如果只认「以 http 开头」,这条就被误判成打不开,白白降级。
    所以要搜,不能只看开头。

    抠不出来就返回空串 —— 那说明这是「去哪找」的人话,不是地址。
    """
    s = (src or "").strip().strip("`").strip().strip('"').strip("'")
    if not s:
        return ""

    m = _URL_RE.search(s)
    if m:
        return m.group(0).rstrip("。.,;；")

    if re.match(r"^[A-Za-z]:[\\/]", s):            # Windows 绝对路径
        return s.split()[0]
    if s.startswith(("/", "./", "../", "\\\\")):
        return s.split()[0]
    if _PATH_RE.match(s):                          # 纯文件名,如 server.py#L40
        return s
    return ""


def source_identity(src):
    """一条证据的「出处身份」。

    同一文件的不同行 → 同一个身份。它们仍然是同源,只是位置不同。
    抠不出地址的(比如「无外部出处,属我的判断」)按原文本归一 —— 它们会归到一组,
    这是对的:都没出处,本来就谈不上独立。
    """
    s = extract_address(src) or (src or "").strip().strip("`").strip()
    if not s:
        return ""

    if s.lower().startswith(("http://", "https://")):
        p = urlsplit(s)
        q = sorted((k, v) for k, v in parse_qsl(p.query)
                   if not k.lower().startswith(_TRACKING))
        return urlunsplit((p.scheme.lower(), p.netloc.lower(),
                           p.path.rstrip("/"), urlencode(q), ""))

    # 本地路径:先剥行号后缀(#L40 / :40 / #L40-42),再统一斜杠和大小写
    s = re.sub(r"(#L?\d+(?:-\d+)?|:\d+(?:-\d+)?)$", "", s)
    return re.sub(r"\s+", "", s.replace("\\", "/").lower())


def host_of(src):
    """出处的域名。用来做「这个站已经连续失败两次了,别再一条条等超时」。"""
    addr = extract_address(src)
    if not addr or not addr.lower().startswith(("http://", "https://")):
        return ""
    return urlsplit(addr).netloc.lower()


def looks_openable(src):
    """这个出处,机器能不能**直接打开**?

    实测出来的坑:模型给了 L2,但 source 写的是人话指路 ——
        "国家卫健委官网或 www.gov.cn 政策文件库,全文检索'不得对首诊患者…'"
    验证器照着这个打不开任何东西,整个核证层就空转了。

    所以:**能抠出地址的才算 L2**。抠不出来就是「去哪找」,不是地址。
    """
    return bool(extract_address(src))


def same_source(e1, e2):
    """第一层判同源。零成本、不会出错、不受人格影响。"""
    a = source_identity(e1.get("source"))
    b = source_identity(e2.get("source"))
    return bool(a) and a == b


def group_evidence(items):
    """把证据按「同一个源头」分组。

    同组 = 相关证据 —— 它们的权重只能合并,不能累加。
    """
    groups, index = [], {}
    for e in items:
        key = source_identity(e.get("source"))
        if key and key in index:
            groups[index[key]].append(e)
        else:
            if key:
                index[key] = len(groups)
            groups.append([e])
    return groups


def weighted_score(groups):
    """同源合并取 max,不同源才累加。

    理由:同一来源被引两次,不是两个证据,是一个证据被数了两遍。
    相关观测不能当独立样本 —— 累加等于给同一个证据记两次功。
    """
    return round(sum(max(weight_of(e.get("level")) for e in g) for g in groups), 3)


def independence_report(items):
    """给前端看的独立性报告 —— 哪几条挤在同一组、被合并了。"""
    out = []
    for i, g in enumerate(group_evidence(items), 1):
        out.append({
            "group": i,
            "source": source_identity(g[0].get("source")) or "(没给出处)",
            "count": len(g),
            "merged": len(g) > 1,
            "weight": max(weight_of(e.get("level")) for e in g),
        })
    return out


# ============================================================
# 验证器的手:按出处把原文取回来
#
# 没有这个函数,验证器也只能看着两段文本猜 —— 那它和辩手一样是 L0。
# ============================================================
def split_ref(source):
    """把 "D:\\x\\a.py#L40" 拆成 ("D:\\x\\a.py", 40, 40)。没行号返回 (路径, None, None)。"""
    s = (source or "").strip().strip("`").strip()
    m = re.match(r"^(.*?)(?:#L?|:)(\d+)(?:-(\d+))?$", s)
    if m:
        return m.group(1).strip(), int(m.group(2)), int(m.group(3) or m.group(2))
    return s, None, None


def fetch_source(source, timeout=20, max_chars=6000):
    """真去把来源打开。

    返回 {"ok", "kind", "text", "note"}
      ok=False 表示够不着 —— 那这条就该标「无法核查」,不许编。
    """
    import requests

    raw = extract_address(source)
    if not raw:
        return {"ok": False, "kind": "none", "text": "",
                "note": "抠不出可打开的地址 —— 出处写的是「去哪找」而不是地址"}

    # ---- 网页 ----
    if raw.lower().startswith(("http://", "https://")):
        # 先看本地库里有没有存好正文 —— AIHOT 抓过的都在,不用出门。
        # 这一步把「403 / 超时 / 得挂梯子」整串问题一次性绕开。
        try:
            import aihot_db
            body = aihot_db.body_for_url(raw)
            if body:
                return {"ok": True, "kind": "local",
                        "text": body[:max_chars],
                        "note": "取自本地 AIHOT 库(没出门)"}
        except Exception:
            pass      # 本地库连不上就正常出门抓,不因为库挂了就失败

        # 必须**显式**走代理:梯子开着也没用 —— Python 的 requests
        # 不读 Windows 系统代理,它得被告知。
        proxies = None
        if config.EVIDENCE_PROXY:
            proxies = {"http": config.EVIDENCE_PROXY,
                       "https": config.EVIDENCE_PROXY}
        try:
            r = requests.get(
                raw, timeout=timeout, proxies=proxies,
                headers={
                    # 有些站按 UA 挡爬虫,伪装成浏览器通过率高不少
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                                  "Chrome/120.0.0.0 Safari/537.36",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                },
            )
            if r.status_code != 200:
                return {"ok": False, "kind": "http", "text": "",
                        "note": f"HTTP {r.status_code}"}
            return {"ok": True, "kind": "http", "text": r.text[:max_chars], "note": ""}
        except Exception as e:
            return {"ok": False, "kind": "http", "text": "", "note": f"抓取失败:{e}"}

    # ---- 本地文件 ----
    path, lo, hi = split_ref(raw)
    if not os.path.isfile(path):
        return {"ok": False, "kind": "file", "text": "", "note": f"文件不存在:{path}"}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except Exception as e:
        return {"ok": False, "kind": "file", "text": "", "note": f"读不了:{e}"}

    if lo is None:
        return {"ok": True, "kind": "file",
                "text": "\n".join(lines[:200])[:max_chars], "note": "整个文件前 200 行"}

    lo = max(1, lo - 2)
    hi = min(len(lines), hi + 2)
    body = "\n".join(f"{i}| {lines[i - 1]}" for i in range(lo, hi + 1))
    return {"ok": True, "kind": "file", "text": body[:max_chars], "note": f"第 {lo}-{hi} 行"}
