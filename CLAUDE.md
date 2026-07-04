# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

自进化 AI 领域教学知识图谱 MVP。Python 3.13 + SQLite，无第三方框架（只有 requests + pyyaml）。文档：设计决策在 `plan.md`，完整算法逻辑在 `algorithm.md`（改动算法时同步更新它）。项目语言是中文（代码注释、prompt、文档、CLI 输出）。

## 运行命令

无构建、无测试套件、无 linter。验证改动的方式是跑对应 CLI 子命令（必要时加 `--dry-run`）。

```bash
.venv/bin/python -m kg <子命令>        # venv 在项目根目录；需要 MINIMAX_API_KEY 环境变量
```

子命令（入口 `kg/cli.py`，日常循环顺序见 README / algorithm.md §10）：

- `seed seeds/ai_core.yaml` — 导入种子图（首次）
- `corpus crawl` / `corpus grow --limit N` / `corpus stats` — 抓维基页面进本地语料库（增量，抓过的版本不重抓）
- `mine aliases` / `mine categories` / `mine wikidata` — 零 LLM 结构挖掘（wikidata：QID 关系→候选边 + 同 QID 疑似同概念仲裁）
- `ingest --batch N`（缺口驱动自动选锚点）/ `ingest <节点名>` / 加 `--dry-run` — 主提取通道
- `expand` — 假设生成器
- `verify --limit N` — 复核 proposed 条目：结构佐证（零 LLM，`--no-llm` 只跑这部分）+ LLM 判断题复核；`--apply` 对双重一致的边自动裁决
- `review` — 人工审核队列（交互式；节点 a/r/m/d，边 a/r/f/t；`--audit N` 抽检自动放行）
- `check` — 守卫；`viz` — 生成 out/graph.html；`stats` / `export <节点名>` / `embed`

数据库 `data/kg.db`，可用 `KG_DB` 环境变量指向测试库以免污染真实数据。M3 一次提取要 1~3 分钟（reasoning 模型），跑涉及 LLM 的命令时用长 timeout 或后台运行。

## 架构

核心原则（plan.md 决策 #8）：**LLM 是抽取器，不是知识源**。图谱内容必须落地到本地语料库（维基页面快照），LLM 只做两件事：从语料正文中有据提取（每个概念/边都带 evidence 原文摘录），以及提议"往哪看"（expand 的假设只有名字，必须经语料验证，找不到来源页就丢弃）。改动任何 prompt 或流水线时不得破坏这一点——不要让 LLM 记忆内容直接进图谱。

数据流水线（模块即层次）：

```
wiki.py（API 客户端，≥1s 节流 + 429 退避；含 Wikidata QID/claims 批量接口）
  → corpus.py（本地语料库：页面快照@revision_id、node_page 映射、内链统计、抓取时标题相似度 ≥0.75 验证 + 消歧义页拒收）
  → mine.py（零 LLM：重定向→别名，分类→part_of 候选边）+ wikidata.py（QID 映射、P279/P361/P737→候选边、同 QID 仲裁）
  → ingest.py（M3 有据提取，锚点模式/主题模式两种 prompt；evidence 子串校验，伪证据丢弃）
  → dedup.py（四级去重：精确名→维基重定向→embedding≥0.90→LLM 裁决）
  → db.py 写入 status='proposed'
  → verify.py 复核（结构佐证：互链/RefD/Wikidata；LLM 判断题复核；--apply 双重一致自动裁决边）
  → cli.py review 人工裁决（全部裁决落 review_log）→ guards.py（先修环/孤儿/facet 重名）
```

`expand.py` 是旁路：M3 提缺口假设名 → corpus 验证 → 转 ingest 主题模式。`llm.py` 封装 MiniMax M3 + embo-01。

## 关键不变式

- **状态工作流**：所有 LLM 产物入库为 `proposed`，只有 seed/approved（`db.visible_statuses()`）参与图算法和教学导出；生效两条路——人工 review，或 `verify --apply` 双重一致自动裁决（仅边，须 LLM 复核「支持」+ 结构佐证同时成立；节点一律人工）。两条路都必须写 `review_log`（校准数据），自动放行要用 `review --audit` 抽检。
- **evidence 可校验**：ingest 产物的 evidence 必须是来源正文子串（`ingest.evidence_in_text`），校验不过整条丢弃；改 prompt 或流水线不得绕过此校验。
- **来源可溯**：每个提取产物的 source 格式为 `wiki:<lang>:<title>@<revision_id>`（结构通道为 `mine:category:<lang>` / `wikidata:<属性>`）；`ingest_log` 按（锚点, 来源版本）防重复提取。
- **边语义**（`db.EDGE_TYPES` 4 种）：`related_to` 受限于 `db.RELATED_KINDS` 三种情形（同题替代/演化启发/教学对比），LLM 必须给 kind 字段，无效即丢弃，kind 存为 rationale 前缀；`prerequisite_of` 是唯一允许推断的边，推断边 rationale 加 `[推断]` 前缀且 confidence 封顶 `ingest.INFERRED_MAX_CONFIDENCE`（0.6）。
- **概念粒度**：算法内部步骤/参数/公式细节不立节点，放 facets（≤12 字名词短语）。
- **M3 调用不设 max_tokens**（用户明确要求，reasoning 思考可以很长）；截断时 `llm.chat` 抛 RuntimeError，调用方（如 expand）逐项 try/except 不让单条失败杀掉整轮。
- 修改 `db.SCHEMA` 只对新库生效（`CREATE TABLE IF NOT EXISTS`），已有 `data/kg.db` 需要手写迁移。
