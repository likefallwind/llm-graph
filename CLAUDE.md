# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

面向教学的 AI 领域知识图谱。Python 3.13 + SQLite，无第三方框架（只有 requests + pyyaml）。项目语言是中文（代码注释、prompt、文档、CLI 输出）。

目标不是节点数，而是：覆盖 AI 各主要子领域、跨子领域语义一致、每条知识可溯源到语料、质量可被固定基准测量、扩展不会悄悄劣化已有知识（`agent.md`）。

## 一、两套核心并存

`develop` 分支在重建知识表示层。同一个 `data/kg.db` 里有两套核心：

| | 新核心（唯一在写的） | 旧核心（已冻结，只读） |
|---|---|---|
| schema | `kg/schema.sql`（+ `kg/schema.py`） | `db.SCHEMA`（+ `db._migrate`） |
| 入口 | `kg pipeline` | `ingest / verify / review / viz / export / check / stats / calibrate` |
| 发布 | 全部停在 Shadow，不自动发布 | proposed → approved 工作流 |

**新工作全部落在新核心，旧核心不加任何算法能力。** 两者不再有代码依赖，
`tests/test_core_isolation.py` 强制这一点。桥只有 `kg/legacy_migration.py`
（Phase 7，代码就绪但尚未跑过）。

碰到新旧语义冲突时按新逻辑改，不要往回兼容——旧核心的表述（facet、`related_to`、
`误区:` 前缀、全局证据强度）都已经不成立。

### 文档权威顺序

1. **代码和数据库** — 唯一的事实来源
2. `design/architecture.md` — 新核心怎么分层、数据怎么流：模块职责、两条入口、
   硬闸与可调策略的边界
3. `design/algorithm.md` — 新核心每一步**具体算什么**：判据、公式、阈值、复杂度、
   全部可调参数、已知算法缺口
4. `development-plan.md` — 目标、10 条不变式、未完成的设计、阶段状态、下一步迭代
5. `agent.md` — agent 工作守则（下面第二节是它的提炼）
6. `design/ontology.md`、`design/evidence-policy.md` — 实体/关系语义、证据强度、
   独立性（人读版）
7. `config/relation-registry.yaml`（当前 v5）— 关系、实体主类型判据、证据类型
   词表与强弱的**机器权威**；`config/ai-coverage-taxonomy.yaml` — 覆盖主题树
   （实体类型改造的全过程与未决项见 `design/entity-type-v5.md`，实施完并入
   `design/ontology.md` 后删除）
8. 本文件 — 发现它和代码不一致时，以代码为准，并顺手改它

2 和 3 分工：架构讲**代码怎么组织**，算法讲**每一步算什么**。改了算法两边都要动。
`TODO.md` 里对根目录 `algorithm.md` 的章节引用指向重写前的旧核心版本，已失效。

### ⚠️ 陈旧状态陷阱

重构中的库里，**记录会带着产生它们时的算法版本，不是当前版本**。做任何"系统现在
是什么状态"的判断前先核对版本与时间戳：

- `decisions.policy_version` 与 `runs.algorithm_version` / `prompt_version` 会混代
- `claims` 里可能存在按当前注册表已不该产生的关系——那是注册表收窄之前抽的
- `decisions` 是追加表，一条 claim 会有多代裁决；只看最新一条，并确认它是否在
  当前策略下产生
- 结论是"某某规则有漏洞"之前，先确认存在活的代码路径能触发它，而不是只看到一条
  历史记录

## 二、工作守则（提炼自 `agent.md`）

- **断言算法行为前，先看当前代码、库状态和评测输出。** 不要从文档或记忆推断。
- **区分数据质量问题和架构问题。** 一条坏边是前者；一类系统性失效才是后者。
- **优先处理系统性失效模式**，不是逐个修坏节点。
- **不为了展示活动而扩图。**
- **不要用清审核队列、调 prompt、加语料来替代架构工作**（`agent.md` 明确点名了
  这三件事）。
- **改关系语义必须成套改**：schema、抽取、验证、审核、导出、文档、benchmark
  一起动，缺一不算改完。
- **用户没明确要求时，不改数据、不跑自动裁决。** 只读检查请复制一份库到临时目录
  再跑——`db.connect()` 会写入（跑迁移、同步关系注册表和覆盖分类）。
- **提下一步时，说清它如何改善全域覆盖或可测量的质量。**
- 改动保持窄、有证据支撑。

## 三、运行命令

无构建、无 linter。测试是 unittest（venv 里没装 pytest）：

```bash
.venv/bin/python -m unittest discover -s tests -t .
```

venv 在项目根目录；调 LLM 的命令需要 `MINIMAX_API_KEY`。数据库默认 `data/kg.db`，
用 `KG_DB` 指向副本以免污染真实数据。M3 是 reasoning 模型，一次抽取 1~3 分钟，
跑涉及 LLM 的命令用长 timeout 或后台运行。

```bash
.venv/bin/python -m kg <子命令>
```

**新流水线 `kg pipeline <action>`**（`cli.cmd_pipeline`）：

| action | 说明 |
|---|---|
| `status` | 新核心计数、状态分布、消歧与队列状态、最近 shadow 决策 |
| `batch --topic ID --docs N --wiki-pages N` | 选本算法版本未处理过的教材节与维基页各跑一批 |
| `doc --book B --sec S --topic T` | 指定教材章节 |
| `wiki --lang zh --title 人工智能 --topic T` | 指定本地 `corpus` 页面（须已抓过） |
| `read --file F --source SLUG --topic T` | 任意 UTF-8 本地文本；另有 `--source-type --independence-group --authority --version --language`；`--observations` 可导入已有结构化 Observation |
| `align-aliases` / `review-alignments` / `replay-pending` / `review-type-conflicts` | 四条复核队列，顺序即此；`--alignment-limit N` |
| `reshadow` | 按当前策略重判全部 claim，只写 Shadow decision。默认零 LLM；`--force-entailment` 重判全部证据，`--only-stale` 只重判版本落后的 |
| `survey` / `target` | 定向补证：`survey` 零 LLM 列出卡在门槛下的 claim 及候选段落；`target` 对这些段落抽取。`--limit N` `--passages N` |
| `identity` | 报告哪些 evidence 摘录里没出现端点身份名——只读体检 |
| `taxonomy-types` | 报告两端主类型不同的 `is_a`——只读体检，零 LLM |
| `duplicates` / `alias-declarations` | 前者列疑似重复实体；后者零 LLM 扫语料里的别名声明句式 |
| `merge --source-entity A --target-entity B --reason R` / `revert-merge --merge-event N` | 人工确认后的实体合并与撤销 |
| `retype --entity N --to T [--definition D] --reason R` / `revert-retype --revision N` / `revisions [--entity N]` | 人工修订主类型与定义，留痕可撤销 |
| `migrate` / `--apply` | 旧 nodes/edges 幂等迁移；无法安全判断的进 `migration_issues` |

通用：`--max-entities` / `--max-claims` 是**每个文本块**的上限；`--no-verify-llm`
跳过 LLM 蕴含复核（Claim 会停在 `needs_more_evidence`，适合无网络管线测试）。

**语料采集（新旧共用）**：

- `docs add sources/<book>.yaml` / `docs fetch <book>` / `docs translate <book>` /
  `docs stats` — 教材通道（英文源用 minimax-m2.7 译成中文入库；抓取与翻译均增量；
  PDF 源需 poppler pdftotext）
- `corpus crawl` / `corpus grow --limit N` / `corpus stats` — 维基页面快照（增量）

**旧流水线**（已冻结，勿扩展）：`seed / ingest / expand / mine / verify / review /
rollback / calibrate / check / viz / export / stats / embed`。

`./evolve.sh` 是日常入口（`--fast` 不调 LLM；`--fetch` 先补教材章节；`--migrate`
执行迁移）。

## 四、改代码时不能破坏的

完整数据流、判据顺序、模块职责见 `design/architecture.md`。这里只列约束。

- **LLM 的输出是 observation，不是知识。** 每个 Entity 和 Claim 都必须绑定到带
  版本的来源快照和机械可定位的 evidence。同一模型的多次调用是审计证据，不是新增
  独立信源。改任何 prompt 或流水线都不得绕过这一点。
- **Evidence 机械可定位。** `observations.evidence_in_text` 只容忍空白差异和
  `...`/`…` 分段，定位不上整条丢弃。
- **Claim 与 Evidence 分离。** canonical claim 由
  `(subject_id, relation, object_id, qualifiers_hash)` 唯一确定，可累计多条
  `support / oppose / uncertain`；后来的证据既不覆盖也不丢弃。Evidence 必须且
  只能绑定一个 entity 或 claim。
- **独立性只来自 `sources.independence_group`**，不是摘录条数、不是同一模型的
  多次判断。翻译、镜像、同一本书的不同章节都不算独立来源。
- **注册表是唯一事实来源。** 关系语义、实体主类型判据、证据类型词表与强弱全在
  `config/relation-registry.yaml`。代码读它，不复述它——第二份词表会被
  `tests/test_core_isolation.py` 抓到。注册表里也不放没有消费者的字段，死配置
  会让人以为某条规则在生效。
- **主类型是单值六类，判据从注册表注入提示词。** `resource / criterion / data /
  task / solution / concept`，按优先序判、命中即停；`concept` 是兜底不是备选。
  **判型的输入是定义，不是名字**——抽取时先写 `definition` 再据它判型，定义说不出
  内容的实体整条丢弃。主类型只表达规范主类别，不声称表达全部用途，其他面向是
  邻边的模式，不存成节点属性。写入后只能经 `pipeline retype` 修订，留痕可撤销。
- **证据计数是两层，别把它们混成一层。** 全局 `strength`
  （`is_assertive_evidence`）决定一段文字算不算断言，编排类（目录序/超链接/共现）
  支持和反对**两侧**都不计；关系白名单（`is_strong_evidence`）决定这类断言能不能
  **建立**该关系，**只作用于支持侧**。建立不了不等于反驳不了——一句定义可能建立
  不了 `part_of` 却足以反驳它。
- **一切裁决先 Shadow。** `store.decide` 是唯一会改 entity/claim 状态的地方，
  `decided_by='shadow'` 的分支不改状态。缺证据是 `needs_more_evidence`，不是
  自动拒绝。
- **蕴含判定只追加不覆盖**（`entailment_reviews`），实体合并可逆
  （`merge_events.payload`），模型队列结论只留痕不改主类型
  （`model_queue_reviews.auto_changed` 恒为 False）。
- **实体消歧快路是确定性的。** 字符串相似度只做候选召回，永远不证明同一性。
  `_validated_direct_match_type` 是充分条件，命中不了不代表不同名。
- **抽取限额是每个文本块的**，不做全章截断。
- **重复处理防护**：每个 `(source_snapshot, coverage_topic, algorithm_version)`
  只处理一次；改算法要动对应的版本号才会重跑。
- **schema 变更**：`kg/schema.sql` 的 `CREATE TABLE IF NOT EXISTS` 只对新库生效；
  已有库的字段变更写进 `kg/schema.py` 的迁移函数并登记 `schema_migrations`。
- **LLM 使用**：M3 调用不设 `max_tokens`（reasoning 可以很长），截断时
  `llm.chat` 抛 RuntimeError，调用方逐项 try/except，不让单条失败杀掉整轮。
  并行只走 `llm.pmap`，**传给 pmap 的 fn 不得触碰 sqlite 连接**。

## 五、质量门槛（决定何时能关掉 Shadow）

来自 `development-plan.md`，是自动发布的开闸条件，不是可以将就的目标：

- 已发布 claim 中无证据的：**0**
- 被接受的机械无效证据：**0**
- 自动实体合并 precision ≥ **99%**
- 每个启用的策略桶，自动批准 precision ≥ **98%**
- 高影响的分类学与先修 claim，在校准数据足够前保持更严策略或人工审核
- 不允许靠极小抽样样本就启用某条自动策略

`benchmarks/gold.jsonl` 目前只有 3 条人工判过的负例（目标 300），所以现在没有任何
策略具备开闸依据。修改门槛需要有记录的基准证据，不是为了方便。

判错的 claim 归档到 `data/archive/` 并导出成 gold 负例再删——错误比正确的例子更能
测出策略漏洞，不要直接删掉了事。

## 六、旧核心（已冻结，只在维护旧命令时需要）

**2026-07-26 起冻结**：现有读命令继续可用，不再加算法能力，新核心也不再依赖它。

新核心里没有 facet：任何值得命名的东西就是一个 entity，不值得命名的就不存。旧核心
的 `误区:` 前缀 facet 不迁移——误区在新核心是普通实体，靠 `often_confused_with`
关联，而那条关系目前是 `experimental`，要用得先按正常流程转正。

旧核心的算法不再有文档——原来的 `algorithm.md` 已改写成新核心的规格并移到
`design/`。要查旧逻辑直接读 `kg/ingest.py` / `kg/verify.py` / `kg/guards.py`。
当前仍成立的骨架：

- **状态工作流**：LLM 产物入库为 `proposed`，只有 seed/approved
  （`db.visible_statuses()`）参与图算法和教学导出；生效两条路——人工 `review`，或
  `verify --apply` 双重一致自动裁决。裁决写 `review_log`，自动裁决带 batch_id 可
  `kg rollback`，自动放行用 `review --audit` 抽检，`calibrate` 出通道×类型×佐证
  组合的 precision。
- **常量**：`db.EDGE_TYPES` 4 种（含 `related_to`），`db.RELATED_KINDS` 3 种，
  `db.NODE_TYPES` 现仅 `concept`；`prerequisite_of` 是唯一允许推断的边，推断边
  confidence 封顶 `ingest.INFERRED_MAX_CONFIDENCE`(0.6)；`db.MISCONCEPTION_PREFIX`
  (`误区:`) 是特殊 facet 前缀，`export` 时拆为 misconceptions 字段。
- **语料分级** `quality.py`：high（种子/教材/教案）> mid（维基/结构挖掘/Wikidata）
  > low（弱校对，一律不参与自动裁决）。
- **守卫** `guards.py`：`cycles` / `prereq_cycles` / `prereq_redundant` /
  `mutual_edges` / `bad_edge_endpoints` / `orphans` / `facet_shadows`。这些是必要
  检查，但**不构成语义质量的证据**（`agent.md`）。
- 旧核心的 evidence 校验是 `ingest.evidence_in_text`，来源格式
  `wiki:<lang>:<title>@<revision_id>` / `doc:<book>:<sec_id>@<content_hash>` /
  `mine:category:<lang>` / `wikidata:<属性>`。

⚠️ `legacy_migration.RELATED_MAP` 把旧 `related_to` 边映射到 `alternative_to` /
`derived_from` / `pedagogical_contrast_with`，这三个在注册表里都是 `experimental`，
抽取路径不放行。跑 Phase 7 迁移前必须先决定：要么先把这三条关系按正常流程转正，
要么迁移时直接丢弃这批边。不要为了迁移开后门绕过 `active_only`。
