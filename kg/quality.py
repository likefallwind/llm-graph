"""语料质量分级：按来源通道给每条提取产物定级，新语料源按级别声明式接入。

三级（tier）：
- high：人工校验或教学性语料——种子骨架、教材/教案/讲义（docs 通道；
  sources/<book>.yaml 可用 tier 字段覆盖，缺省 high）。教学性关系（先修、
  教学对比）密度最高，是提取的首选来源。
- mid ：社区校对的百科与结构数据——wiki 提取、mine 结构挖掘、Wikidata。
  覆盖面广，做兜底提取与结构佐证。
- low ：弱校对语料（博客、论坛等，暂未接入）。保留级别：low 来源的条目
  一律不参与 verify --apply 自动裁决，只能人工审。

用途：ingest 批量选点先教材后维基（日常循环顺序）；verify 自动裁决按级别
把关；calibrate 的通道 precision 天然细于 tier，攒够数据后可按 tier 汇总。
以后接入新语料只需：加抓取通道 + 在这里给 source 前缀定级（或 yaml 声明）。
"""
import functools

TIERS = ("high", "mid", "low")

_MID_PREFIXES = ("wiki:", "mine:", "wikidata")


@functools.lru_cache(maxsize=None)
def _book_tier(book: str) -> str:
    from . import docs  # 延迟导入避免环
    try:
        tier = docs.load_book(book).get("tier", "high")
    except (FileNotFoundError, ValueError):
        return "high"  # 配置缺失时按教材的默认级别处理
    return tier if tier in TIERS else "high"


def source_tier(source: str) -> str:
    """source 字符串 -> 质量级别。未知来源按 low 保守处理。"""
    source = source or ""
    if source.startswith("seed"):
        return "high"
    if source.startswith("doc:"):
        return _book_tier(source[4:].split(":", 1)[0])
    if source.startswith(_MID_PREFIXES):
        return "mid"
    return "low"


def auto_adjudicable(source: str) -> bool:
    """low 级来源不参与自动裁决，一律留人工。"""
    return source_tier(source) in ("high", "mid")
