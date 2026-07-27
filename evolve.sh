#!/usr/bin/env bash
# 语料驱动知识图谱日常循环。只走新 Pipeline，所有裁决保持 Shadow。
#
# 用法：./evolve.sh [选项]
#   --fast          不调用 LLM，只检查迁移预览与 Pipeline 状态
#   --topic ID      覆盖主题（默认 ai）
#   --docs N        本轮读取的未处理教材章节数（默认 1）
#   --wiki N        本轮读取的未处理 Wikipedia 页面数（默认 1）
#   --max-entities N 单份语料最多实体数（默认 20）
#   --max-claims N  单份语料最多 Claim 数（默认 30）
#   --fetch         先同步并抓取 sources/*.yaml 的缺失教材章节
#   --migrate       先执行旧 nodes/edges 的幂等迁移
#   --concurrency N MiniMax API 全局最大并发（默认 6）
#
# 正式知识不由此脚本自动发布。Pipeline 只写 proposed Entity/Claim、
# Evidence 和 Shadow Decision。

set -u
cd "$(dirname "$0")"
PY=.venv/bin/python

FAST=0
TOPIC=ai
DOCS=1
WIKI=1
MAX_ENTITIES=20
MAX_CLAIMS=30
FETCH=0
MIGRATE=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --fast) FAST=1 ;;
        --topic) TOPIC="$2"; shift ;;
        --docs) DOCS="$2"; shift ;;
        --wiki) WIKI="$2"; shift ;;
        --max-entities) MAX_ENTITIES="$2"; shift ;;
        --max-claims) MAX_CLAIMS="$2"; shift ;;
        --fetch) FETCH=1 ;;
        --migrate) MIGRATE=1 ;;
        --concurrency) export KG_LLM_CONCURRENCY="$2"; shift ;;
        *) echo "未知选项: $1（看脚本头部注释）" >&2; exit 2 ;;
    esac
    shift
done

if [[ $FAST -eq 0 && -z "${MINIMAX_API_KEY:-}" ]]; then
    echo "缺 MINIMAX_API_KEY（只检查状态请加 --fast）" >&2
    exit 1
fi

FAILED=()
step() {
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

if [[ $FETCH -eq 1 ]]; then
    for f in sources/*.yaml; do
        book=$(basename "$f" .yaml)
        step "docs add $book" "$PY" -m kg docs add "$f"
        step "docs fetch $book" "$PY" -m kg docs fetch "$book"
    done
fi

if [[ $MIGRATE -eq 1 ]]; then
    step "legacy migrate" "$PY" -m kg pipeline migrate --apply
else
    step "legacy migrate preview" "$PY" -m kg pipeline migrate
fi

if [[ $FAST -eq 0 ]]; then
    step "grounded pipeline batch" "$PY" -m kg pipeline batch \
        --topic "$TOPIC" --docs "$DOCS" --wiki-pages "$WIKI" \
        --max-entities "$MAX_ENTITIES" --max-claims "$MAX_CLAIMS"
fi

step "pipeline status" "$PY" -m kg pipeline status

echo
if [[ ${#FAILED[@]} -gt 0 ]]; then
    echo "有步骤失败: ${FAILED[*]}"
    exit 1
fi
echo "全部步骤完成。所有新决策均为 Shadow；查看上方 status 后再决定是否扩大批次。"
