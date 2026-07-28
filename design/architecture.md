# 新核心架构

按数据流组织，讲**代码怎么分层、数据怎么流**。每一步具体算什么——判据、公式、
阈值、复杂度、全部可调参数——见 `design/algorithm.md`。

设计意图与未完成的部分见 `development-plan.md`；关系与证据语义见
`design/ontology.md`、`design/evidence-policy.md`；机器权威是
`config/relation-registry.yaml`。

和代码不一致时以代码为准，并顺手改本文件。

## 一、总纲

一条不变式决定了全部分层：**LLM 的输出是 observation，不是知识**。

LLM 可以规划阅读、抽取、消歧、判关系、复核蕴含、翻译、排审核优先级。但每个
Entity 和 Claim 都必须绑定到带版本的来源快照和机械可定位的 evidence。同一模型
的多次调用是审计证据，不是新增独立信源。

所以每个模块本质上都在回答同一个问题：**这一步允许 LLM 决定什么，不允许决定
什么**。读代码时按这个角度看，分层就清楚了。

## 二、模块分层

**采集与工具**（新旧核心共用，它们是工具不是知识表示）

- `wiki.py` — Wikipedia/Wikidata API 客户端，≥1s 节流 + 429 退避
- `corpus.py` — 维基页面快照，增量，抓过的版本不重抓
- `docs.py` — 教材通道，声明式源配置，英文源经 `llm.TRANSLATE_MODEL` 译成中文入库
- `htmltext.py` / `quality.py`
- `llm.py` — MiniMax M3（`CHAT_MODEL`）+ embo-01 + `pmap` 并发

**新核心**

- `schema.sql` + `schema.py` — 表定义与增量迁移
- `models.py` — 只读数据类
- `store.py` — **唯一写入口**，所有 INSERT/UPDATE 都在这里
- `ontology.py` — 加载并强制执行关系注册表
- `coverage.py` — 覆盖主题树
- `local_corpus.py` — 本地语料段落枚举与快照登记，`doc_sections` / `corpus`
  缺表也不崩
- `observations.py` — 分块抽取 + 机械校验
- `entity_resolution.py` — 实体消歧
- `alias_evidence.py` — 零 LLM 别名声明扫描
- `claims.py` — Observation 落成 Entity/Claim/Evidence，并登记物化留痕
- `validators.py` — 蕴含复核 + 门槛评估
- `decision.py` — 写 Shadow Decision
- `targeting.py` — 定向补证
- `pipeline.py` — 编排
- `review_queues.py` — 类型冲突队列
- `legacy_migration.py` — 通往旧核心的唯一桥

**旧核心**（已冻结，只读）：`db.py`、`ingest.py`、`dedup.py`、`verify.py`、
`expand.py`、`mine.py`、`wikidata.py`、`guards.py`、`calibrate.py`、`export.py`、
`viz.py`、`seed.py`

`db.connect()` 会同时装两套 schema。隔离由 `tests/test_core_isolation.py` 强制：
AST 扫新核心每个模块，import 了旧核心、或 SQL 里出现 `nodes / edges / review_log`
就失败。

## 三、入口 A：读新文本 → 产生新 claim

`pipeline.read_text` (`kg/pipeline.py:34`) 是唯一入口。`read_doc_section` /
`read_wiki_page` / `read_file` 都只是包装它，差别只在怎么拿到文本、怎么定
`independence_group` 和 `authority_profile`。

### 1. 登记

`store.upsert_source` + `store.add_source_snapshot`（按 content_hash 去重）+
`store.create_run`。

run 记下 `pipeline.ALGORITHM_VERSION`（当前 `grounded-pipeline-8`）、
prompt 版本（`grounded-extract-7`）、注册表版本。

### 2. 抽取

`observations.extract` (`kg/observations.py:192`)。

`split_text` 按自然段分块，**12000 是目标不是硬边界**：一段跨过上限时在这一段的
后面断开（取长的那一侧），永远不在段落中间切。在前面断会留下一个刚好卡在上限的
短块，而抽取质量对上下文完整度敏感，对块长不敏感。

每块一次 LLM 调用，走 `llm.pmap` 并发，然后跨块去重合并。

并发前由主线程生成 active canonical + verified alias 的只读 identity 快照。名称比较
只额外忽略空白；同一引用键指向多个实体时不进入快照。并行线程只读普通字典，不触碰
sqlite。

**限额是每块的，不是整章的**（`--max-entities` / `--max-claims`）。长章节的后半段
不能因为分块被丢掉。

提示词在 `kg/observations.py:13`。允许的实体类型、关系契约、证据类型可选值
全部由 `registry()` 生成，代码里没有第二份词表。

### 3. 机械校验

`observations.parse_payload` (`kg/observations.py:112`)。五道闸，全部零 LLM，
不合格的整条丢进 `rejected`：

1. `entity_type` 必须在注册表里
2. evidence 必须 `evidence_in_text` 逐字定位——只容忍空白差异和 `...`/`…` 分段
3. claim 至少一个端点必须出现在**本块有效 entities**；另一个可引用 identity 快照；
   两端都不在本块则视为偏离本批发现范围
4. relation 走 `validate_claim_endpoint_types(..., active_only=True)`，只放行
   `lifecycle: core` 并校验所有已知端点类型；qualifiers 按契约必填校验；
   `evidence_type` 必须在注册表词表里
5. `next_reading_targets` 必须在正文出现才登记

### 4. 落库

`claims.materialize` (`kg/claims.py:18`)。**先实体后 claim，顺序不能反。**

每个实体：先写一条 `observations` 行 → `entity_resolution.resolve` → 成功则写
entity evidence（类型 `entity_description`）+ proposed aliases → 标 observation
resolved。每写一行同时登记一条 `observation_materializations`（见第五节）——留痕
必须发生在写入那一刻，事后从 `evidence.metadata` 反推重建不出来。

每个 claim 的端点解析走 `_endpoint_id`，两级：

1. 本批 `resolved` 字典
2. 退回库里已有实体的**确定性精确匹配**（规范名 / 唯一 verified alias）。只走
   确定性快路，不做相似度、不调 LLM——这里认领的是已经消歧过的既有实体，不是
   重新做一次消歧

端点正在等疑似对齐裁决（`suspected_same_entity`）时跳过第 2 级：此时认领任何实体
都可能抢在裁决之前替它做决定。**`contextual_same_entity` 不进这个集合**——语境判定
已经认领了实体，端点照常解析。

两级都落不了地的，observation **保持 pending 不标 rejected**——rejected 是终态，
没有任何路径会重看它，而后续批次很可能建出这个实体。

`store.add_claim` (`kg/store.py:236`) 保证 canonical claim 由
`(subject_id, relation, object_id, qualifiers_hash)` 唯一确定；对称关系强制
`subject_id < object_id` 归一化。

### 5. 蕴含复核

`validators.verify_entailment_batch` (`kg/validators.py:124`)。严格三段：

```
串行读库拼 prompt → llm.pmap 并行调用 → 串行落库
```

并发只发生在 HTTP 上，sqlite 始终单线程。**传给 pmap 的 fn 不得触碰连接。**

`_prompt_context` (`kg/validators.py:68`) 按关系拼提示词：对称关系告诉模型不要
因为端点顺序判 contradicts；`part_of` 额外加语义硬闸和反事实检查，并要求输出
`composition_explicit`。

`_interpret` (`kg/validators.py:109`)：`part_of` 判 supports 但没返回
`composition_explicit=true` 的，降级为 insufficient。

判定写 `entailment_reviews`，**只追加不覆盖**，挂上产生它的 run。
`evidence.current_entailment_review_id` 指向最新一条。

### 6. 裁决

`decision.shadow_claim` → `validators.evaluate` → `store.decide(decided_by='shadow')`。

shadow 只往 `decisions` 追加，不改 claim 状态。`store.decide:330` 那个
`if decided_by != 'shadow'` 是**唯一**会改 entity/claim 状态的分支。

## 四、入口 B：定向补证 → 给已有 claim 找新来源

`kg/targeting.py`。方向和入口 A 相反：不产生新概念，只给已有 claim 补独立来源组。

存在的理由：读第二本教材通读一遍很少能佐证已有 claim——两本书讲同一个概念，
极少写出同一个三元组。

### 1. 选目标

`stuck_claims` — 最新裁决是 `needs_more_evidence` 的 claim。

### 2. 机械检索（零 LLM）

`find_passages`：

- `_entity_names` 取两端的身份名（canonical + verified alias，长度 ≥2）
- `_cooccurrences` 找两端相距 ≤ `WINDOW`(600) 的每一处共现。命中位置重叠的排除
  ——中文里一端常是另一端的子串（回归/线性回归），重叠说明只是同一处文字被两个
  名字各匹配一次
- **候选的粒度是「一次共现」，不是「一节」**：同一节里两处讲到同一对概念就是两个
  候选，按距离排序竞争名额
- 已经支持该 claim 的独立来源组跳过
- `targeting_probes` 里探测过的跳过

### 3. 取窗口

`_neighborhood`：命中点前后各 `CONTEXT`(300) 字，再向外对齐到边界（优先段首，
其次句首，`SNAP_LIMIT`(400) 封顶）。

两个约束同时成立：

- **不能给整节**。距离窗口必须限定模型读哪里，不只是筛选哪些节参与。一节动辄
  上万字，全给模型它会引用和命中位置无关的句子
- **不能只给一句**。判断两个概念的关系需要上下文，孤立一句看不出是定义、举例
  还是并列

实测窗口 365–1263 字，中位 653，相当于命中处所在的一两段完整文字。

`Passage.text` 是送模型的窗口，`Passage.section_text` 是整节原文。**快照登记的是
整节**——窗口只约束模型看哪里，不改变 provenance 的单位。

### 4. 中性抽取

提示词 (`kg/targeting.py:23`) 点名两个端点但**不说待验证的关系**，否则是诱导性
提问。模型可以返回相反方向或 `none`。

`_extract_one` 校验：关系不是 none、端点按 `normalized_name` 比对且恰好是这两个、
evidence 逐字可定位、关系/qualifiers/证据类型过注册表。

之后接回同一套 `claims` / `validators` / `decision`，没有捷径。

## 五、实体消歧

**「名称归一化」是三个问题，不是一个**，三者门槛不同、落地物不同，不能共用一套
判据：

| 问题 | 判据 | 落地物 |
|---|---|---|
| 这次 mention 指向哪个实体 | 本次语境的证据与定义 | `contextual_same_entity` |
| 这个名字能否成为**全局**别名 | 跨独立来源组的稳定性 | `aliases.status='verified'` |
| 两个已有实体是否该**物理合并** | 人工确认 | `merge_events` |

此前它们共用 verified alias 一套门槛，最弱的那个被最强的那个卡住：真别名没转正之
前，整条 Claim 都落不了地。

`entity_resolution.resolve` (`kg/entity_resolution.py`)。确定性优先：

1. canonical `normalized_name` 精确命中 → 确定，结束
2. 唯一 verified alias 精确命中 → 确定，结束
3. `_candidate_rows` 用 `SequenceMatcher` 召回 ≤5 个候选，排完序再补每个候选的
   ≤3 条原文摘录与 ≤4 条已有关系。**相似度只召回，永远不证明同一性**
4. LLM 分类，返回 `existing|new|ambiguous` + `match_type` + `confidence`
5. existing 且 conf ≥ `LLM_SUSPECT_CONFIDENCE`(0.80)：
   `_validated_direct_match_type` 是**充分条件**——命中
   `translation_alias`/`name_variant` 且 conf ≥ `LLM_AUTO_LINK_CONFIDENCE`(0.95)
   就自动 verified。**命中不了不代表不同名**
6. 非快路、conf ≥ 0.95 且 `_needs_separation_review` 为真 → **反向复核**
   （第二次 LLM，只能否决）。判「该分开」且 conf ≥ 0.60 → `ambiguous`，
   且不记对齐证据
7. `store.add_alignment_evidence` 累计：`score = 1 - Π(1 - conf_group)`，每个独立
   来源组只取最高分。需 ≥2 组、score ≥ 0.95、且无竞争候选才升级为 verified
8. 累计未达标但 conf ≥ 0.95 → **语境链接** `contextual_same_entity`：实体认领、
   Claim 落图，别名留 proposed 继续累计
9. 累计未达标且 conf < 0.95 → `suspected_same_entity`，端点进 pending
10. new 且 conf ≥ `LLM_NEW_ENTITY_CONFIDENCE`(0.95) → 建 proposed 实体
11. new 低于 0.95 → `below_confidence`；模型拿不准 → `ambiguous`；违反输出契约
    → `invalid_response`。三者 observation 都保持 pending

所有自动落地采用统一的 0.95 门槛。0.80 只决定一个 existing 建议是否值得进入疑似
证据累计。置信度不足和真实歧义都不表示 observation 无效，因此不能进入 rejected。

**名称同一性分三档**（`tests/test_name_identity.py` 是这三档的可执行定义）：

```
第一档 纯形式变化      大小写／全半角／空白／标点／修饰性「的」   → 确定性判同名
第二档 类别后缀包含    一个名字 = 另一个 + 类别词，且不改主类型   → 可走快路
第三档 同词根异中心词  机器学习方法 / 机器学习算法               → 只召回，必须语境判
```

第三档的判据是**包含而不是词根相交**。相交只要求两个名字各剥各的、剥到中间碰头，
共同词根只说明它们谈同一个领域，不说明是同一个知识对象。

**反向复核**（`_verify_separation_with_llm`）对高风险名称对——同词根异中心词，或
一个名字是另一个加了非类别修饰成分（`softmax-交叉熵损失` / `交叉熵损失`）——换方向
问一次「请优先寻找不能合并的理由」。**只能否决不能支持**，和
`is_strong_evidence`、`knowledge_objection` 的不对称同构。两轮同一个模型，
**不构成第二个来源**，结论只写进消歧事件不进独立来源计数。

缩写、符号、语义别名、复合名不靠一次模型判断转成全局别名。

**第二条转正通道**：`alias_evidence.py` 零 LLM 扫语料里的显式别名声明（括号注释、
又称句式），每条 `DECLARATION_CONFIDENCE`(0.9)。单个来源组 0.90 < 0.95 不转正，
两个独立来源组 0.99 ≥ 0.95 转正。这是语料给的证据，不是模型判断。

**第三条观察通道**：`entity_resolution.mention_stability_report` 只读汇总
`entity_resolution_events`——一个表面名称在多次真实语境里落到了哪些实体、有没有
竞争目标、跨了几个独立来源组。全局别名该问的是这个，而不是累计分本身。

**物化留痕**：语境链接会当场落地，所以每一行写入都登记
`observation_materializations`（observation × 消歧事件 × 目标）。判错时
`store.revert_materialization` 把 Claim/Alias 置 rejected、observation 退回
pending 等重放；命中的既有实体不动（它有自己的来历），evidence 行不删（留着才能
复现依据）。入口是 `pipeline materializations` 和 `pipeline revert-resolution`。

**实体合并**是独立机制，不在自动流程里：`store.merge_entities` 由重复清扫报告
驱动、人工确认后调用。搬动的行 id 全部写进 `merge_events.payload`，
`store.revert_merge` 按它回滚。

## 六、evaluate 的判据顺序

`validators.evaluate` (`kg/validators.py:203`) 是整个系统的裁决核心。

**证据计数是两层过滤，管的是两件不同的事**：

- **全局 `strength`**（`registry().is_assertive_evidence`）— 这段文字是不是一句
  断言。`toc_order / hyperlink / cooccurrence` 是编排不是断言，既支持不了也反驳
  不了，**支持与反对两侧都不计**
- **关系白名单 `accepted_evidence_types`**（`registry().is_strong_evidence`）—
  这类断言能不能**建立**该关系。**只作用于支持侧**

第二层不能套到反对侧。「特征是预测所依据的自变量」这句定义建立不了
`特征 part_of 样本`，但它确实是对这条 claim 的有效反驳。建立不了 ≠ 反驳不了。

判据顺序，六步短路：

1. `validate_claim` 端点类型不合法 → 抛异常
2. `_would_cycle` 有环 → `human_review`
3. 有断言性 `contradicts` → `human_review`
4. 没有能建立该关系的 `supports` → `needs_more_evidence`
5. `high >= explicit_high_authority` 且 `independent >= independent_*_sources`
   → `high_impact_review` 为真就 `human_review`，否则 `auto_approve`
6. 否则 `needs_more_evidence`

被两层过滤掉的证据都写进 `reasons`，不算数不等于不可见。

`_required_independent` 用显式判空取门槛，不是 `or` 串联——配成 0 时 `or` 会静默
滑到下一个默认值，那是配置被忽略，不是配置生效。

三条 core 关系当前都是 `high_impact_review: true`，所以第 5 步永远走不到
`auto_approve`。

`Validation.evidence_reviews` 记下裁决依据的是「这些证据的**这一次**判定」，
写进 `decisions.evidence_review_snapshot`。重判之后旧裁决仍然复现得出来。

## 七、四条复核队列

顺序有依赖，必须按这个次序跑（`kg pipeline <action>`）：

1. `align-aliases` — proposed 别名复核。走不了确定性快路的转到
   `alias_evidence.record`
2. `review-alignments` — suspected 对齐候选
3. `replay-pending` — 重放 pending observation。此时前两步可能已经补上了端点
4. `review-type-conflicts` — `review_queues.py`。结论只写 `model_queue_reviews`，
   `auto_changed` 恒为 False，实体主类型变更走人工

配套的只读体检（零 LLM，不改数据，跑几次都一样）：`name-stability`（表面名称的
跨语境稳定性）、`duplicates`、`alias-declarations`、`identity`、`taxonomy-types`、
`endpoint-types`、`materializations`。它们不是队列，是决定该看哪条队列的依据。

## 八、版本号 = 重跑开关

五组互相独立的计数器。改哪个决定重跑什么：

- `pipeline.ALGORITHM_VERSION` — 改了才会重读已处理过的
  `(source_snapshot, coverage_topic)`。防重复靠 `pipeline_processed`
- `validators.VALIDATOR_VERSION` / `ENTAILMENT_PROMPT_VERSION` — 改了
  `reshadow --only-stale` 才认为旧判定过期（`stale_evidence_ids`）
- `decision.POLICY_VERSION` — 只给裁决记录打标签，不触发重跑
- `entity_resolution.RESOLVER_VERSION` / `ALIGNMENT_POLICY_VERSION`
- `targeting.ALGORITHM_VERSION` / `PROMPT_VERSION`

注册表 `version` 每次 `db.connect()` 同步进 `relation_definitions`。

历史遗留的蕴含判定补了 review 行但 `run_id` 为 NULL——那些的模型和 prompt 版本
已不可考，不伪造版本号。

## 九、硬闸 vs 可调策略

分模块优化时最重要的分界线。

**硬闸，不能为了提召回放宽**：

- evidence 逐字定位（`observations.evidence_in_text`）
- claim 至少一个端点来自本块；另一端只认唯一 canonical/verified identity
- 独立性只来自 `sources.independence_group`
- 消歧快路只认精确匹配与类别后缀**包含**（不是词根相交），相似度只召回
- 反向复核只能否决，不能支持；两轮同模型不构成第二个来源
- 语境链接必须留 `observation_materializations`，可撤销
- 一切裁决先 Shadow
- 注册表是唯一词表来源，代码不复述
- `llm.pmap` 的 fn 不碰 sqlite

**可调，改了要有基准证据**：

- `minimum_evidence`：当前 2 个独立组 + 1 条高权威
- `LLM_AUTO_LINK_CONFIDENCE` 0.95 / `LLM_NEW_ENTITY_CONFIDENCE` 0.95
- `SEPARATION_OBJECTION_CONFIDENCE` 0.60（反向复核只否决，故意低于正向）
- `CANDIDATE_EXCERPTS` 3 / `CANDIDATE_RELATIONS` 4
- `NAME_VARIANT_SUFFIXES` 收哪些类别词
- 累计对齐的 ≥2 组 + ≥0.95
- `targeting.WINDOW` 600 / `CONTEXT` 300 / `SNAP_LIMIT` 400
- `observations.split_text` 的 12000
- `DUPLICATE_SCAN_THRESHOLD` 0.6
- 哪些关系 `high_impact_review`
- `llm.MAX_CONCURRENCY`（默认 6，`KG_LLM_CONCURRENCY` 可覆盖）

## 十、已知结构性限制

按模块列，都是读代码看出来的，不是猜测。

**`observations.parse_payload` 的范围约束** — claim 至少一个端点必须来自本块有效
entities；另一端可通过只读 identity 快照引用唯一已有实体。两端都不在本块的 claim
仍会被排除，即使关系可能正确，因为当前发现入口把它视为偏离本批主题。这是一条明确
的范围政策，不是实体解析能力限制。

非本块端点无法唯一解析时不会被拒绝：claim observation 保持 pending，等后续实体
出现或 identity 歧义解除后由 `replay-pending` 捡回。

**`targeting.find_passages`** — 每次调用都把全部语料读进内存
（`local_corpus.passages`），复杂度 O(claims × corpus)。语料规模上去要建倒排。

**`targeting` 的召回方式** — 纯字符串共现 + 距离，没有任何语义召回。同一节里
离得远但相关的段落取不到，不同措辞的表述也取不到。`llm.py` 有 embo-01 但新核心
没用。

**`observations` 的抽取路径** — `pipeline identity` 报告里缺端点身份名的证据
绝大多数来自这条路，不是 targeting。提示词在 `kg/observations.py:13`。

**lifecycle 的执行面** — `active_only` 在 `observations.parse_payload`、
`claims.replay_pending`、`targeting._extract_one` 三条抽取路径上生效，但
`store.add_claim` 和 `validators.evaluate` 本身不查 lifecycle。新增写路径时要
自己带上这道闸。

**未接上的** — `reading_tasks` 只写不读；`pipeline.batch` 按 `doc_sections.ord`
顺序选章节，不看覆盖优先级（development-plan Phase 6）。
