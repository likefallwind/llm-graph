"""校准统计（零 LLM）：review_log × review_signals -> 自动放行阈值的标注数据汇总。

三张表：
- 人工裁决 precision：按（通道 × 条目类型/边类型 × 佐证组合桶）分组，
  precision = approve / (approve + reject)。高精度组合可放宽自动裁决，低的收紧。
- 自动放行抽检推翻率：audit_reject / (audit_confirm + audit_reject)，按通道分组。
- burn-in 影子裁决一致率：verify --apply --dry-run 记下的 auto_would
  vs 此后人工首次裁决，按通道分组。一致率够高的通道才放开真 --apply。

已知取舍：review_signals 存的是最新信号，可能晚于人工裁决更新，
precision 是「当前信号组合 vs 历史裁决」的近似——校准阈值够用；
严格快照需要在裁决时冻结信号副本，暂不做。
"""
import json
from collections import defaultdict

from . import db


def channel_of(source: str) -> str:
    source = source or ""
    if source.startswith("doc:"):
        return "doc:" + source.split(":")[1]
    if source.startswith("wiki:"):
        return "ingest/expand"
    if source.startswith("mine:"):
        return ":".join(source.split(":")[:2])
    if source.startswith("wikidata"):
        return "wikidata"
    if source.startswith("seed"):
        return "seed"
    return source or "(空)"


def _verdict_key(verdict) -> str:
    """归并 LLM 结论：应为facet→X -> 应为facet；支持(方向存疑) -> 支持(方向?)。"""
    if not verdict:
        return ""
    v = verdict.split("→")[0]
    if "(" in v:
        v = v.split("(")[0] + "(方向?)"
    return v


def bucket_of(item_type: str, sig) -> str:
    """佐证组合桶：把 review_signals 压成可分组的短标签。"""
    if not sig:
        return "无信号"
    s = sig["signals"]
    parts = []
    if item_type == "edge":
        if s.get("link_src_dst") and s.get("link_dst_src"):
            parts.append("互链")
        elif s.get("link_src_dst") or s.get("link_dst_src"):
            parts.append("单向链")
        if s.get("refd", 0) > 0:
            parts.append("refd↑")
        elif s.get("refd", 0) < 0:
            parts.append("refd↓")
        if s.get("toc", 0) > 0:
            parts.append("toc↑")
        elif s.get("toc", 0) < 0:
            parts.append("toc↓")
        if s.get("wikidata"):
            parts.append("wikidata")
    else:
        if s.get("has_page"):
            parts.append("有页")
        if s.get("exact_title"):
            parts.append("精确命中")
    v = _verdict_key(sig.get("llm_verdict"))
    if v:
        parts.append(f"LLM:{v}")
    return "+".join(parts) or "无信号"


def _first_decisions(conn, decided_by="human"):
    """每个条目的首次裁决（同一条目多次裁决只取最早那次，代表当时的判断）。"""
    seen, rows = set(), []
    for r in conn.execute(
            "SELECT * FROM review_log WHERE decided_by=? ORDER BY created_at", (decided_by,)):
        key = (r["item_type"], r["item_id"])
        if key in seen:
            continue
        seen.add(key)
        rows.append(dict(r))
    return rows


def human_precision(conn) -> list[dict]:
    edge_types = {r["id"]: r["type"] for r in conn.execute("SELECT id, type FROM edges")}
    groups = defaultdict(lambda: {"approve": 0, "reject": 0, "other": 0})
    for r in _first_decisions(conn, "human"):
        if r["action"].startswith("audit_"):
            continue
        sig = db.get_signals(conn, r["item_type"], r["item_id"])
        kind = edge_types.get(r["item_id"], "?") if r["item_type"] == "edge" else "node"
        key = (channel_of(r["source"]), kind, bucket_of(r["item_type"], sig))
        if r["action"] == "approve":
            groups[key]["approve"] += 1
        elif r["action"] == "reject":
            groups[key]["reject"] += 1
        else:  # merge/flip/retype/demote：提议部分正确，单列不进分母
            groups[key]["other"] += 1
    out = []
    for (channel, kind, bucket), c in groups.items():
        n = c["approve"] + c["reject"]
        out.append({"channel": channel, "kind": kind, "bucket": bucket,
                    "approve": c["approve"], "reject": c["reject"], "other": c["other"],
                    "n": n, "precision": (c["approve"] / n) if n else None})
    out.sort(key=lambda d: -(d["n"] + d["other"]))
    return out


def auto_overturn(conn) -> list[dict]:
    """自动放行的抽检结果：audit_reject 率高的通道该收紧自动裁决规则。"""
    audits = {}
    for r in conn.execute(
            "SELECT * FROM review_log WHERE action IN ('audit_confirm','audit_reject')"
            " ORDER BY created_at"):
        audits[(r["item_type"], r["item_id"])] = r["action"]
    stats = defaultdict(lambda: {"approved": 0, "confirm": 0, "reject": 0})
    for r in _first_decisions(conn, "auto"):
        if r["action"] != "approve":
            continue
        ch = channel_of(r["source"])
        stats[ch]["approved"] += 1
        audit = audits.get((r["item_type"], r["item_id"]))
        if audit == "audit_confirm":
            stats[ch]["confirm"] += 1
        elif audit == "audit_reject":
            stats[ch]["reject"] += 1
    out = []
    for ch, c in sorted(stats.items()):
        audited = c["confirm"] + c["reject"]
        out.append({"channel": ch, "approved": c["approved"], "audited": audited,
                    "overturn": (c["reject"] / audited) if audited else None})
    return out


def shadow_agreement(conn) -> list[dict]:
    """burn-in 校准实验的汇总：影子裁决（auto_would）与人工金标的一致情况。

    agree=人工同判；disagree=人工反判（approve↔reject）；
    modified=人工做了 merge/flip/retype/demote（提议只对了一部分，影子批准算不完全对）；
    pending=还没人工裁决。一致率 = agree / (agree+disagree+modified)。"""
    humans = {}
    for r in _first_decisions(conn, "human"):
        if not r["action"].startswith("audit_"):
            humans[(r["item_type"], r["item_id"])] = r["action"]
    src_of = {"node": {r["id"]: r["source"] for r in conn.execute("SELECT id, source FROM nodes")},
              "edge": {r["id"]: r["source"] for r in conn.execute("SELECT id, source FROM edges")}}
    groups = defaultdict(lambda: {"shadow": 0, "agree": 0, "disagree": 0,
                                  "modified": 0, "pending": 0})
    for r in conn.execute("SELECT * FROM review_signals WHERE signals LIKE '%auto_would%'"):
        would = json.loads(r["signals"]).get("auto_would")
        if would not in ("approve", "reject"):
            continue
        source = src_of[r["item_type"]].get(r["item_id"], "")
        g = groups[(channel_of(source), r["item_type"])]
        g["shadow"] += 1
        action = humans.get((r["item_type"], r["item_id"]))
        if action is None:
            g["pending"] += 1
        elif action == would:
            g["agree"] += 1
        elif action in ("approve", "reject"):
            g["disagree"] += 1
        else:
            g["modified"] += 1
    out = []
    for (channel, kind), g in sorted(groups.items()):
        judged = g["agree"] + g["disagree"] + g["modified"]
        out.append({"channel": channel, "kind": kind, **g,
                    "agreement": (g["agree"] / judged) if judged else None})
    return out


def report(conn) -> str:
    lines = ["=== 人工裁决 precision（通道 × 类型 × 佐证组合）===",
             "（信号为最新态，precision 是近似；n<3 样本不足仅供参考）"]
    rows = human_precision(conn)
    if not rows:
        lines.append("还没有人工裁决记录（先 kg review）")
    for r in rows:
        p = f"{r['precision']:.0%}" if r["precision"] is not None else " - "
        note = "（样本不足）" if r["n"] < 3 else ""
        other = f"，改动 {r['other']}" if r["other"] else ""
        lines.append(f"  {r['channel']:<16} {r['kind']:<16} {r['bucket']:<38}"
                     f" precision {p:>4}（{r['approve']}批/{r['reject']}拒{other}）{note}")
    lines.append("")
    lines.append("=== 自动放行抽检（kg review --audit）===")
    autos = auto_overturn(conn)
    if not autos:
        lines.append("还没有自动放行记录（kg verify --apply 产生）")
    for r in autos:
        ov = f"{r['overturn']:.0%}" if r["overturn"] is not None else "未抽检"
        lines.append(f"  {r['channel']:<16} 自动批准 {r['approved']}，已抽检 {r['audited']}，"
                     f"推翻率 {ov}")
    lines.append("")
    lines.append("=== burn-in 影子裁决 vs 人工金标（kg verify --apply --dry-run）===")
    shadows = shadow_agreement(conn)
    if not shadows:
        lines.append("还没有影子裁决记录（kg verify --apply --dry-run 产生）")
    for r in shadows:
        ag = f"{r['agreement']:.0%}" if r["agreement"] is not None else "待人工裁决"
        lines.append(f"  {r['channel']:<16} {r['kind']:<5} 影子裁决 {r['shadow']}，"
                     f"一致 {r['agree']}，反判 {r['disagree']}，改动 {r['modified']}，"
                     f"未裁 {r['pending']}，一致率 {ag}")
    return "\n".join(lines)
