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
- `docs add sources/<book>.yaml` / `docs fetch <book>` / `docs translate <book>` / `docs stats` — 教材语料通道（声明式源配置；英文源用 minimax-m2.7 翻译成中文入库，抓取/翻译均增量；PDF 源需要 poppler pdftotext）
- `mine aliases` / `mine categories` / `mine wikidata` — 零 LLM 结构挖掘（wikidata：QID 关系→候选边 + 同 QID 疑似同概念仲裁）
- `ingest --batch N --doc`（教材缺口驱动批量，high 级语料优先）/ `ingest --batch N`（维基批量兜底）/ `ingest <节点名>` / `ingest <节点名> --doc [BOOK]`（教材通道）/ 加 `--dry-run` — 主提取通道；长页分块提取（首块+按锚点密度选块，每页 ≤3 块）；evidence 校验失败先逐字重引一次再丢弃
- `expand` — 假设生成器
- `verify --limit N` — 复核 proposed 条目：结构佐证（零 LLM，`--no-llm` 只跑这部分；含教材目录序 toc 信号）+ LLM 判断题复核；`--apply` 双重一致自动裁决；`--apply --dry-run` 影子裁决（burn-in：只记 review_signals.auto_would，不改状态不写 review_log）
- `review` — 人工审核队列（交互式，按复核信号分层排序、冲突项在前；节点 a/r/m/d，边 a/r/f/t；节点批准后就地裁决其关联边；`--audit N` 抽检自动放行）
- `rollback [batch_id]` — 整批撤销一次 `verify --apply` 自动裁决（退回 proposed；不带参数列批次；后来的裁决优先不被覆盖）
- `calibrate` — 校准统计：人工裁决 precision（通道×类型×佐证组合）+ 自动放行抽检推翻率 + burn-in 影子裁决 vs 人工一致率（零 LLM）
- `check` — 守卫；`viz` — 生成 out/graph.html；`stats` / `export <节点名>`（定义/facets/误区/先修链/讲解资源 JSON）/ `embed`

数据库 `data/kg.db`，可用 `KG_DB` 环境变量指向测试库以免污染真实数据。M3 一次提取要 1~3 分钟（reasoning 模型），跑涉及 LLM 的命令时用长 timeout 或后台运行。

## 架构

核心原则（plan.md 决策 #8）：**LLM 是抽取器，不是知识源**。图谱内容必须落地到本地语料库（维基页面快照），LLM 只做两件事：从语料正文中有据提取（每个概念/边都带 evidence 原文摘录），以及提议"往哪看"（expand 的假设只有名字，必须经语料验证，找不到来源页就丢弃）。改动任何 prompt 或流水线时不得破坏这一点——不要让 LLM 记忆内容直接进图谱。

数据流水线（模块即层次）：

```
wiki.py（API 客户端，≥1s 节流 + 429 退避；含 Wikidata QID/claims 批量接口）
  → corpus.py（本地语料库：页面快照@revision_id、node_page 映射、内链统计、抓取时标题相似度 ≥0.75 验证 + 消歧义页拒收）
  + docs.py（教材/教案语料：sources/*.yaml 声明式源 → 章节快照+目录序；html 用 htmltext.py，pdf 用 pdftotext；英文源翻译即快照 content_hash 为版本）
  + quality.py（语料三级：high=种子/教材/教案（批量提取优先），mid=维基/结构挖掘/Wikidata，low=弱校对语料（不参与自动裁决）；yaml tier 字段可声明）
  → mine.py（零 LLM：重定向→别名，分类→part_of 候选边）+ wikidata.py（QID 映射、P279/P361/P737→候选边、同 QID 仲裁）
  → ingest.py（M3 有据提取，锚点模式/主题模式/教材模式；长页分块+选块；evidence 子串校验，伪证据丢弃）
  → dedup.py（四级去重：精确名→维基重定向→embedding≥0.90→LLM 裁决）
  → db.py 写入 status='proposed'（related_to 反向重复在 add_edge 拦截）
  → verify.py 复核（结构佐证：互链/RefD/toc 教材目录序/Wikidata；LLM 判断题复核；--apply 双重一致自动裁决）
  → cli.py review 人工裁决（全部裁决落 review_log）→ guards.py（先修/is_a/part_of 环、先修传递冗余、正反向同型边、孤儿、facet 重名）
  → calibrate.py（review_log × review_signals → 通道×类型×佐证组合 precision，指导自动裁决松紧）
```

`expand.py` 是旁路：M3 提缺口假设名 → corpus 验证 → 转 ingest 主题模式。`llm.py` 封装 MiniMax M3 + embo-01。

## 关键不变式

- **状态工作流**：所有 LLM 产物入库为 `proposed`，只有 seed/approved（`db.visible_statuses()`）参与图算法和教学导出；生效两条路——人工 review，或 `verify --apply` 双重一致自动裁决（边：LLM 复核「支持」+ 结构佐证同时成立、且无环类型模拟加边不成环才批准，「不支持」+ 零佐证可拒绝；节点只自动批准：LLM 判「独立概念」+ 名字精确命中语料页 + 语料页与图谱邻居有重叠（UMAP 撞名补丁），拒绝/降级 facet 一律人工；low 级语料一律不自动裁决）。不用 LLM 自报数值置信度做分流（自报置信度校准差），置信度=独立信源是否一致。两条路都必须写 `review_log`（校准数据，自动裁决带 batch_id 可 `kg rollback` 整批撤销），自动放行要用 `review --audit` 抽检。
- **evidence 可校验**：ingest 产物的 evidence 必须是来源正文子串（`ingest.evidence_in_text`），校验不过整条丢弃；改 prompt 或流水线不得绕过此校验。英文教材源以**翻译后的中文文本**为快照（`doc_sections.text` + content_hash 版本号），evidence 对翻译文本校验——翻译一旦入库即冻结，重翻译产生新版本。
- **来源可溯**：每个提取产物的 source 格式为 `wiki:<lang>:<title>@<revision_id>` 或 `doc:<book>:<sec_id>@<content_hash>`（结构通道为 `mine:category:<lang>` / `wikidata:<属性>`）；`ingest_log` 按（锚点, 来源版本）防重复提取。
- **边语义**（`db.EDGE_TYPES` 4 种）：`related_to` 受限于 `db.RELATED_KINDS` 三种情形（同题替代/演化启发/教学对比），LLM 必须给 kind 字段，无效即丢弃，kind 存为 rationale 前缀；`prerequisite_of` 是唯一允许推断的边，推断边 rationale 加 `[推断]` 前缀且 confidence 封顶 `ingest.INFERRED_MAX_CONFIDENCE`（0.6）。
- **概念粒度**：算法内部步骤/参数/公式细节不立节点，放 facets（≤12 字名词短语）。误区（Misconception）是特殊 facet：`db.MISCONCEPTION_PREFIX`（`误区:`）前缀 + ≤40 字陈述，走 ingest 全部 evidence 约束提取，`export` 时拆出为 misconceptions 字段。
- **M3 调用不设 max_tokens**（用户明确要求，reasoning 思考可以很长）；截断时 `llm.chat` 抛 RuntimeError，调用方（如 expand）逐项 try/except 不让单条失败杀掉整轮。翻译用 `llm.TRANSLATE_MODEL`（minimax-m2.7，同 endpoint，`KG_TRANSLATE_MODEL` 可覆盖），提取与复核仍是 M3。**LLM 并发上限 6**（`llm.MAX_CONCURRENCY`，信号量全局兜底，`KG_LLM_CONCURRENCY` 可覆盖）；并行只通过 `llm.pmap`，传给它的 fn 不得触碰 sqlite 连接。
- **ingest 的 `--limit` 是每页概念产出预算**（不是调用次数）：平摊到各块、合并后截断，保证分块不放大"宁缺毋滥"总闸门。
- 修改 `db.SCHEMA` 只对新库生效（`CREATE TABLE IF NOT EXISTS`），已有 `data/kg.db` 的增量迁移写进 `db._migrate`（connect 时自动执行，如 review_log.batch_id）。
