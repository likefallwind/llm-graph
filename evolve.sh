#!/usr/bin/env bash
# 日常进化循环一键脚本（顺序见 algorithm.md §10）。
#
# 用法：./evolve.sh [选项]
#   --fast          只跑零 LLM 部分（语料/结构挖掘/结构佐证/守卫/看图），几分钟内完成
#   --batch N       ingest 批量锚点数（默认 3；0 跳过 ingest）
#   --verify N      LLM 复核条数上限（默认 20）
#   --translate N   每本英文教材预翻译 N 节（默认 0，即用到时 lazy 翻译）
#   --no-expand     跳过 expand 假设生成器
#   --apply         verify 后执行双重一致自动裁决（默认不裁决，只积累信号）
#   --concurrency N MiniMax API 全局最大并发（默认 6；verify 复核/翻译分块/ingest 分块并行）
#
# 交互式步骤（kg review / kg review --audit）不在此脚本内，跑完手动做。
# 用 KG_DB 环境变量可指向测试库。

set -u
cd "$(dirname "$0")"
PY=.venv/bin/python

BATCH=3 VERIFY=20 TRANSLATE=0 FAST=0 EXPAND=1 APPLY=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --fast) FAST=1 ;;
        --batch) BATCH="$2"; shift ;;
        --verify) VERIFY="$2"; shift ;;
        --translate) TRANSLATE="$2"; shift ;;
        --no-expand) EXPAND=0 ;;
        --apply) APPLY=1 ;;
        --concurrency) export KG_LLM_CONCURRENCY="$2"; shift ;;
        *) echo "未知选项: $1（看脚本头部注释）" >&2; exit 2 ;;
    esac
    shift
done

if [[ $FAST -eq 0 && -z "${MINIMAX_API_KEY:-}" ]]; then
    echo "缺 MINIMAX_API_KEY（只想跑零 LLM 部分请加 --fast）" >&2
    exit 1
fi

FAILED=()
step() {  # step <名字> <命令...>：报时、失败不中断
    local name="$1"; shift
    echo
    echo "=== $name ==="
    local t0=$SECONDS
    if "$@"; then
        echo "--- $name 完成（$((SECONDS - t0))s）"
    else
        echo "!!! $name 失败（继续后续步骤）" >&2
        FAILED+=("$name")
    fi
}

# 1. 语料：维基 + 教材（均增量，抓过的不重抓）
step "corpus crawl（维基页面）" $PY -m kg corpus crawl
for f in sources/*.yaml; do
    book=$(basename "$f" .yaml)
    step "docs add $book（登记/同步源配置）" $PY -m kg docs add "$f"
    step "docs fetch $book（抓缺失章节）" $PY -m kg docs fetch "$book"
done
if [[ $TRANSLATE -gt 0 ]]; then
    for book in cs231n cs229 sutton-barto; do
        step "docs translate $book（预翻译 $TRANSLATE 节）" \
            $PY -m kg docs translate "$book" --limit "$TRANSLATE"
    done
fi

# 2. 结构挖掘（零 LLM）
step "mine aliases（重定向→别名）" $PY -m kg mine aliases
step "mine categories（分类→候选边）" $PY -m kg mine categories
step "mine wikidata（QID 关系→候选边）" $PY -m kg mine wikidata

# 3. LLM 提取与复核
if [[ $FAST -eq 1 ]]; then
    step "verify --no-llm（结构佐证，含 toc）" $PY -m kg verify --no-llm
else
    if [[ $BATCH -gt 0 ]]; then
        step "ingest --batch $BATCH（缺口驱动提取，每锚点 1~3 分钟）" \
            $PY -m kg ingest --batch "$BATCH"
    fi
    if [[ $EXPAND -eq 1 ]]; then
        step "expand（假设生成器）" $PY -m kg expand
    fi
    VERIFY_ARGS=(--limit "$VERIFY")
    VERIFY_LABEL="verify（结构佐证 + LLM 复核）"
    if [[ $APPLY -eq 1 ]]; then
        VERIFY_ARGS+=(--apply)
        VERIFY_LABEL="verify（结构佐证 + LLM 复核 + 自动裁决）"
    fi
    step "$VERIFY_LABEL" $PY -m kg verify "${VERIFY_ARGS[@]}"
fi

# 4. 守卫、校准、看图、统计
step "check（一致性守卫）" $PY -m kg check
step "calibrate（裁决 precision 汇总）" $PY -m kg calibrate
step "viz（生成 out/graph.html）" $PY -m kg viz
step "stats" $PY -m kg stats

echo
if [[ ${#FAILED[@]} -gt 0 ]]; then
    echo "⚠ 有步骤失败: ${FAILED[*]}"
else
    echo "✓ 全部步骤完成"
fi
echo "下一步（交互式，手动跑）："
echo "  $PY -m kg review            # 人工裁决队列（冲突项在前）"
echo "  $PY -m kg review --audit 5  # 抽检自动放行（跑过 --apply 后定期做）"
[[ ${#FAILED[@]} -gt 0 ]] && exit 1 || exit 0
