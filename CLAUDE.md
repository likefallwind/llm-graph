# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

面向教学的 AI 领域知识图谱。Python 3.13 + SQLite，无第三方框架（只有 requests + pyyaml）。项目语言是中文（代码注释、prompt、文档、CLI 输出）。

目标不是节点数，而是：覆盖 AI 各主要子领域、跨子领域语义一致、每条知识可溯源到语料、质量可被固定基准测量、扩展不会悄悄劣化已有知识（`agent.md`）。

## 一、先读这个：仓库正在核心重构中

`develop` 分支在重建知识表示层。**同一个 `data/kg.db` 里并存两套核心**：

| | 新核心（唯一在写的） | 旧核心（已冻结，只读） |
|---|---|---|
| 表 | `sources / source_snapshots / entities / aliases / claims / evidence / entailment_reviews / observations / decisions / runs / merge_events / targeting_probes / coverage_topics / reading_tasks` | `nodes / edges / corpus / doc_sections / review_log` |
| schema | `kg/schema.sql`（+ `kg/schema.py` 装载与迁移） | `db.SCHEMA`（+ `db._migrate`） |
| 入口 | `kg pipeline` | `ingest / verify / review / viz / export / check / stats / calibrate` |
| 发布 | **全部停在 Shadow，不自动发布** | proposed → approved 工作流 |

**新工作全部落在新核心，旧核心不加任何算法能力。** 两者不再有代码依赖，`tests/test_core_isolation.py` 强制这一点。桥只有 `kg/legacy_migration.py`（Phase 7，代码就绪但尚未跑过）。

碰到新旧语义冲突时按新逻辑改，不要往回兼容——旧核心的表述（facet、`related_to`、`误区:` 前缀、全局证据强度）都已经不成立。

### 文档权威顺序

1. **代码和数据库** — 唯一的事实来源
2. `development-plan.md` — 现行计划：目标、10 条不变式、目标架构、阶段状态、下一步迭代（含状态快照）
3. `agent.md` — agent 工作守则（下面第二节是它的提炼）
4. `design/ontology.md`、`design/evidence-policy.md` — 实体/关系语义、证据强度、独立性、自动裁决策略（人读版）
5. `config/relation-registry.yaml`（当前 v4）— 关系、实体类型、**证据类型词表与强弱**的机器权威，`kg/ontology.py` 加载并强制执行；`config/ai-coverage-taxonomy.yaml` — 覆盖主题树
6. `plan.md`、`algorithm.md` — **只描述旧核心**，且早于重构。改旧核心算法时同步 `algorithm.md`；改新核心不必动它们
7. 本文件 — 曾经在重构期间停更了两周多。**发现它和代码不一致时，以代码为准，并顺手改它**

### ⚠️ 陈旧状态陷阱

重构中的库里，**记录会带着产生它们时的算法版本，不是当前版本**。做任何"系统现在是什么状态"的判断前先核对版本与时间戳：

- `decisions.policy_version` 与 `runs.algorithm_version` / `prompt_version` 会混代
- `claims` 里可能存在按当前 `config/relation-registry.yaml` 已不该产生的关系（`lifecycle: experimental`）——那是注册表收窄之前抽的
- `decisions` 是追加表，一条 claim 会有多代裁决；只看最新一条，并确认它是否在当前策略下产生
- 结论是"某某规则有漏洞"之前，先确认存在活的代码路径能触发它，而不是只看到一条历史记录

## 二、工作守则（提炼自 `agent.md`）

- **断言算法行为前，先看当前代码、库状态和评测输出。** 不要从文档或记忆推断。
- **区分数据质量问题和架构问题。** 一条坏边是前者；一类系统性失效才是后者。
- **优先处理系统性失效模式**，不是逐个修坏节点。
- **不为了展示活动而扩图。**
- **不要用清审核队列、调 prompt、加语料来替代架构工作**（`agent.md` 明确点名了这三件事）。
- **改关系语义必须成套改**：schema、抽取、验证、审核、导出、文档、benchmark 一起动，缺一不算改完。
- **用户没明确要求时，不改数据、不跑自动裁决。** 只读检查请复制一份库到临时目录再跑——`db.connect()` 会写入（跑迁移、同步关系注册表和覆盖分类）。
- **提下一步时，说清它如何改善全域覆盖或可测量的质量。**
- 改动保持窄、有证据支撑。

## 三、运行命令

无构建、无 linter。测试是 unittest（venv 里没装 pytest）：

```bash
.venv/bin/python -m unittest discover -s tests -t .
```

venv 在项目根目录；调 LLM 的命令需要 `MINIMAX_API_KEY`。数据库默认 `data/kg.db`，用 `KG_DB` 指向副本以免污染真实数据。M3 是 reasoning 模型，一次抽取 1~3 分钟，跑涉及 LLM 的命令用长 timeout 或后台运行。

```bash
.venv/bin/python -m kg <子命令>
```

**新流水线 `kg pipeline <action>`**（`cli.cmd_pipeline`）：

| action | 说明 |
|---|---|
| `status` | 新核心计数、claims/aliases/observations 状态分布、消歧与队列状态、最近 shadow 决策 |
| `migrate` / `--apply` | 旧 nodes/edges 幂等迁移为 proposed Entity/Claim + Evidence；无法安全判断的进 `migration_issues` |
| `batch --topic ID --docs N --wiki-pages N` | 选本算法版本未处理过的教材节与已映射维基页各跑一批 |
| `doc --book B --sec S --topic T` | 指定教材章节 |
| `wiki --lang zh --title 人工智能 --topic T` | 指定本地 `corpus` 页面（须已抓过） |
| `read --file F --source SLUG --topic T` | 任意 UTF-8 本地文本；另有 `--source-type --independence-group --authority --version --language`；`--observations` 可导入已有结构化 Observation（仍走 evidence 机械校验） |
| `align-aliases` / `review-alignments` / `replay-pending` / `review-type-conflicts` | 四条复核队列，顺序即此；`--alignment-limit N` |
| `reshadow` | 按当前策略重判全部 claim，只写 Shadow decision。默认零 LLM；`--force-entailment` 重判全部证据的蕴含，`--only-stale` 只重判判定版本落后于当前 validator/prompt 的那些 |
| `survey` / `target` | 定向取证：`survey` 列出卡在证据门槛下的 claim 及本地语料里的候选段落（零 LLM）；`target` 对这些段落跑抽取，只找已有 claim 的新独立来源。`--limit N` `--passages N` |
| `identity` | 报告哪些 evidence 的摘录里没出现端点的身份名（规范名或 verified alias）——数据质量体检，不改库 |
| `duplicates` / `alias-declarations` | 前者按名字相似度列疑似重复实体；后者零 LLM 扫语料里的别名声明句式（括号注释、又称），累计进对齐候选 |
| `merge --source-entity A --target-entity B --reason R` / `revert-merge --merge-event N` | 人工确认后的实体合并与撤销 |

通用：`--max-entities` / `--max-claims` 是**每个文本块**的上限；`--no-verify-llm` 跳过 LLM 蕴含复核（Claim 会停在 `needs_more_evidence`，适合无网络管线测试）。

**语料采集（新旧共用）**：

- `docs add sources/<book>.yaml` / `docs fetch <book>` / `docs translate <book>` / `docs stats` — 教材通道（声明式源配置；英文源用 minimax-m2.7 译成中文入库；抓取与翻译均增量；PDF 源需 poppler pdftotext）
- `corpus crawl` / `corpus grow --limit N` / `corpus stats` — 维基页面快照（增量，抓过的版本不重抓）

**旧流水线**（迁移期对照，勿扩展）：`seed / ingest / expand / mine / verify / review / rollback / calibrate / check / viz / export / stats / embed`。

`./evolve.sh` 是日常入口（`--fast` 不调 LLM，只看迁移预览与 status；`--fetch` 先补教材章节；`--migrate` 执行迁移）。

## 四、架构

**核心原则：LLM 的输出是 observation，不是知识。** LLM 可以规划阅读、抽取、消歧、判关系、复核蕴含、翻译、排审核优先级，但每个 Entity 和 Claim 都必须绑定到带版本的来源快照和**机械可定位**的 evidence。同一模型的多次调用是审计证据，不是新增独立信源。改任何 prompt 或流水线都不得绕过这一点。

```
sources/*.yaml + Wikipedia
  → docs.py / corpus.py（章节与页面快照；英文源以翻译后的中文文本为快照）
  → pipeline.read_text（登记 source + source_snapshot + run，按 coverage topic 归属）
  → observations.py（分块抽取 → Entity/Claim/next_reading_targets；evidence 逐字校验 + 本体契约校验）
  → entity_resolution.py（规范名/verified alias 精确 → 候选召回 → LLM 分类 → 确定性安全闸 / 疑似对齐队列）
  → claims.py（聚合为 canonical claim，Evidence 单独入库；端点未消歧的进 pending 等重放）
  → validators.py（关系专用蕴含复核 + 无环/端点类型/证据门槛评估）
  → decision.py（只写 Shadow Decision）
  → cli pipeline 队列复核（alias / alignment / pending replay / type conflict）

第二条入口是定向取证，方向相反——不是读新文本产生新 claim，而是给已有 claim 补独立来源：

targeting.survey（找卡在门槛下的 claim → local_corpus 里按共现+邻近召回候选段落，零 LLM）
  → targeting.run（只对这些段落跑抽取，端点按规范名比对；探测过程记入 targeting_probes）
  → claims.py / validators.py / decision.py（与主流程同一套，不走捷径）
```

模块分层：

- **采集与工具（新旧共用）**：`wiki.py`（API 客户端，≥1s 节流 + 429 退避，含 Wikidata 接口）、`corpus.py`、`docs.py`、`htmltext.py`、`quality.py`、`llm.py`（MiniMax M3 + embo-01 + `pmap` 并发）
- **新核心**：`schema.sql` + `schema.py`、`models.py`、`store.py`（唯一写入口）、`ontology.py`、`coverage.py`、`observations.py`、`entity_resolution.py`、`claims.py`、`validators.py`、`decision.py`、`pipeline.py`、`review_queues.py`、`targeting.py`（定向取证）、`alias_evidence.py`（零 LLM 别名声明扫描）、`local_corpus.py`（本地语料段落读取，两套表都容忍缺失）、`legacy_migration.py`
- **旧核心**：`db.py`、`ingest.py`、`dedup.py`、`verify.py`、`expand.py`、`mine.py`、`wikidata.py`、`guards.py`、`calibrate.py`、`export.py`、`viz.py`、`seed.py`

`db.connect()` 会同时装两套 schema（`db.SCHEMA` + `db._migrate`，然后 `schema.ensure` 执行 `schema.sql`、补迁移、把关系注册表和覆盖分类同步进库）。

**新旧核心的隔离是测试强制的**：`tests/test_core_isolation.py` 用 AST 扫新核心的每个模块，import 了旧核心模块、或 SQL 里出现 `nodes / edges / review_log`，测试直接失败。最后一处耦合已经拆掉——`pipeline batch` 选维基页原先要 `JOIN node_page` 并要求 `nodes.status IN ('seed','approved')`，现在只看 `corpus`。

剩下的共用是有意的：`db.connect()` 同时装两套 schema，采集与工具层（`wiki / corpus / docs / htmltext / quality / llm`）两边都用——它们是工具，不是知识表示。`local_corpus.py` 读 `doc_sections` / `corpus` 时先查 `sqlite_master`，表不在也不崩，所以新核心不硬依赖旧表。

**尚未接上的**：`reading_tasks` 只入库无人消费，`batch` 按 `doc_sections.ord` 顺序选章节，不看覆盖优先级（Phase 6）。

## 五、关键不变式（新核心）

- **Evidence 机械可定位**：`observations.evidence_in_text` 只容忍空白差异和 `...`/`…` 分段，定位不上整条丢弃；Claim 的 subject/object 必须同时出现在本批有效 entities 里；`next_reading_targets` 必须在正文出现才登记为 `reading_tasks`。
- **Claim 与 Evidence 分离**：canonical claim 由 `(subject_id, relation, object_id, qualifiers_hash)` 唯一确定，可累计多条 `support / oppose / uncertain` 证据；后来的证据既不覆盖也不丢弃。Evidence 必须且只能绑定一个 entity 或 claim。
- **独立性来自 `sources.independence_group`**，不是摘录条数、不是同一模型的多次判断；翻译、镜像、同一本书的不同章节都不算独立来源。
- **关系注册表是唯一事实来源**（`config/relation-registry.yaml`，当前 `version: 4`）。核心关系只有 `is_a / part_of / prerequisite_of`（`lifecycle: core`），抽取路径用 `validate_claim(..., active_only=True)` 只放行 core；其余 10 个是 `experimental`，登记但不参与抽取。注意 `active_only` 目前只在 `observations.parse_payload` 一处生效，`store.add_claim` 与 `validators.evaluate` 不查 lifecycle——改这些路径时留意。
- **证据类型词表也在注册表里**：顶层 `evidence_types` 定义全部取值和各自 `strength`。强类型 7 个（`explicit_definition / explicit_taxonomy / explicit_composition / explicit_function / explicit_prerequisite / explicit_comparison / explicit_derivation`），弱类型 3 个（`toc_order / hyperlink / cooccurrence`）。抽取提示词的可选值由 `registry().evidence_type_names()` 生成，代码里不得再写第二份词表（`tests/test_core_isolation.py` 会抓）。
- **强弱是按关系判的，不是全局的**：每个关系的 `accepted_evidence_types` 决定哪些类型能计入门槛，`validators.evaluate` 走 `registry().is_strong_evidence(relation, type)`。`explicit_function` 对 `used_for` 是强证据，对 `part_of` 明确排除——「A 用于 B」正是 part_of 定义要排掉的读法。不在白名单里的类型不是降级为弱，是完全不计。
- **注册表里不放没有消费者的字段**：`tests/test_core_isolation.py` 会遍历注册表字段，找不到读它的代码就失败。死配置比缺配置更危险，它让人以为某条规则在生效。
- **关系语义细节**：`prerequisite_of` 必填 qualifiers `kind`(conceptual|derivational|curricular) + `strength`(required|recommended)，`scope` 仅在语料明确限定时填；`part_of` 有额外语义硬闸——用于/依赖/参与/帮助构建/输入输出/属性/子类型都不是 part_of，蕴含复核必须返回 `composition_explicit=true` 才算支持。
- **一切裁决先 Shadow**：`decision.shadow_claim` 只往 `decisions` 写 `decided_by='shadow'`，不改 claim 状态。三个核心关系当前都是 `high_impact_review: true`，即使证据达标也判 `human_review`。`store.decide` 是唯一会改 entity/claim 状态的地方。缺证据是 `needs_more_evidence`，不是自动拒绝。
- **蕴含判定只追加不覆盖**：每次判定往 `entailment_reviews` 插一行并挂上产生它的 `runs` 记录，`evidence.current_entailment_review_id` 指向最新一条；`decisions.evidence_review_snapshot` 记下这次裁决是基于哪些判定做的，所以回读一条旧裁决不用假设当前判定就是它当时看到的。改 `validators.VALIDATOR_VERSION` 或 `ENTAILMENT_PROMPT_VERSION` 后，`pipeline reshadow --stale-only` 只重判版本落后的证据。历史遗留的判定补了 review 行但 `run_id` 为 NULL——那些的模型和 prompt 版本已不可考，不伪造版本号。
- **实体消歧快路是确定性的**：规范名精确命中 → 唯一 verified alias 精确命中；字符串相似度只做候选召回，永远不证明同一性。LLM 判同实体只有在 `translation_alias`/`name_variant` 且 confidence ≥ `LLM_AUTO_LINK_CONFIDENCE`(0.95) 时自动 verified；其余进 `entity_alignment_candidates` 累计，需跨 ≥2 个独立来源组、合成分 ≥0.95 且无竞争候选才升级。缩写、符号、语义别名、复合名不靠一次模型判断合并。
- **实体合并可逆**：`store.merge_entities` 把 alias / evidence / claim 搬到目标实体，源实体转 `merged` 且原规范名留作 verified alias；搬动的行 id 全部写进 `merge_events.payload`，`store.revert_merge` 按它回滚。合并后自指的 claim 和撞唯一键的重复 claim 标 rejected，撤销时一并恢复。已合并的实体不能再次作为合并源，同一个 merge_event 不能撤销两次。
- **模型队列结论只留痕不改主类型**：`review_queues.review_type_conflicts` 把判断写进 `model_queue_reviews`（`auto_changed` 恒为 False），实体主类型变更走人工。
- **抽取限额是每个文本块的**：`observations.split_text` 按段落切 12000 字符块，限额平摊到块、跨块去重合并，不做全章截断——长章节后半段不能因为分块被丢掉。
- **重复处理防护**：每个 `(source_snapshot, coverage_topic, algorithm_version)` 只处理一次（`pipeline_processed`）；改算法要动 `pipeline.ALGORITHM_VERSION` 才会重跑。抽取与复核都建 `runs` 记录算法版本、prompt 版本和配置。
- **schema 变更**：`kg/schema.sql` 的 `CREATE TABLE IF NOT EXISTS` 只对新库生效；已有库的字段变更写进 `schema._migrate_entity_resolution` 并登记 `schema_migrations`。旧核心的增量迁移仍在 `db._migrate`。
- **LLM 使用**：M3 调用不设 `max_tokens`（用户明确要求，reasoning 可以很长），截断时 `llm.chat` 抛 RuntimeError，调用方逐项 try/except，不让单条失败杀掉整轮；翻译用 `llm.TRANSLATE_MODEL`（minimax-m2.7，`KG_TRANSLATE_MODEL` 可覆盖），抽取与复核仍是 M3；并发上限 `llm.MAX_CONCURRENCY`=6（`KG_LLM_CONCURRENCY` 可覆盖），并行只走 `llm.pmap`，**传给 pmap 的 fn 不得触碰 sqlite 连接**。

## 六、质量门槛（决定何时能关掉 Shadow）

来自 `development-plan.md` §6.3，是自动发布的开闸条件，不是可以将就的目标：

- 已发布 claim 中无证据的：**0**
- 被接受的机械无效证据：**0**
- 自动实体合并 precision ≥ **99%**
- 每个启用的策略桶，自动批准 precision ≥ **98%**
- 高影响的分类学与先修 claim，在校准数据足够前保持更严策略或人工审核
- 不允许靠极小抽样样本就启用某条自动策略

`benchmarks/gold.jsonl` 目前只有 3 条人工判过的负例（目标 300），所以现在没有任何策略具备开闸依据。修改门槛需要有记录的基准证据，不是为了方便。判错的 claim 归档到 `data/archive/` 并导出成 gold 负例再删——错误比正确的例子更能测出策略漏洞，不要直接删掉了事。

## 七、旧核心（已冻结，只在维护旧命令时需要）

**2026-07-26 起旧核心冻结**：现有读命令继续可用，但不再加任何算法能力，新核心也不再依赖它。发现新旧概念混用时按新逻辑改，不要往回兼容。

新核心里没有 facet：任何值得命名的东西就是一个 entity，不值得命名的就不存。旧核心的 `误区:` 前缀 facet 不迁移——误区在新核心是普通实体，靠 `often_confused_with` 关联，而那条关系目前是 `experimental`，要用得先按正常流程转正。

完整算法见 `algorithm.md`。当前仍成立的骨架：

- **状态工作流**：LLM 产物入库为 `proposed`，只有 seed/approved（`db.visible_statuses()`）参与图算法和教学导出；生效两条路——人工 `review`，或 `verify --apply` 双重一致自动裁决（`verify.apply_auto`）。裁决写 `review_log`，自动裁决带 batch_id 可 `kg rollback`（`verify.list_batches` / `rollback_batch`），自动放行用 `review --audit` 抽检，`calibrate` 出通道×类型×佐证组合的 precision。
- **常量**：`db.EDGE_TYPES` 4 种（is_a / part_of / prerequisite_of / related_to），`db.RELATED_KINDS` 3 种（同题替代/演化启发/教学对比），`db.NODE_TYPES` 现仅 `concept`，端点按 `db.EDGE_ENDPOINT_TYPES` 校验；`prerequisite_of` 是唯一允许推断的边，推断边 confidence 封顶 `ingest.INFERRED_MAX_CONFIDENCE`(0.6)；`db.MISCONCEPTION_PREFIX`(`误区:`) 是特殊 facet 前缀，`export` 时拆为 misconceptions 字段。
- **语料分级** `quality.py`：high（种子/教材/教案，`sources/*.yaml` 可用 tier 覆盖）> mid（维基/结构挖掘/Wikidata）> low（弱校对，一律不参与自动裁决）。
- **守卫** `guards.py`：`cycles` / `prereq_cycles` / `prereq_redundant` / `mutual_edges` / `bad_edge_endpoints` / `orphans` / `facet_shadows`。这些是必要检查，但**不构成语义质量的证据**（`agent.md`）。
- 旧核心的 evidence 校验是 `ingest.evidence_in_text`，来源格式 `wiki:<lang>:<title>@<revision_id>` / `doc:<book>:<sec_id>@<content_hash>` / `mine:category:<lang>` / `wikidata:<属性>`。

⚠️ `legacy_migration.RELATED_MAP` 把旧 `related_to` 边映射到 `alternative_to` / `derived_from` / `pedagogical_contrast_with`，这三个在注册表里都是 `experimental`，抽取路径不放行。跑 Phase 7 迁移前必须先决定：要么先把这三条关系按正常流程转正，要么迁移时直接丢弃这批边。不要为了迁移开后门绕过 `active_only`。
