# 算法规格

新核心每一步具体算什么：判据、公式、阈值、复杂度。

模块怎么分层、数据怎么流见 `design/architecture.md`；关系与证据语义见
`design/ontology.md`、`design/evidence-policy.md`；机器权威是
`config/relation-registry.yaml`；还没做的部分见 `development-plan.md`。

和代码不一致时以代码为准，并顺手改本文件。

---

# 第一部分：整体架构

## 1.1 问题设定

**输入**：带版本的语料快照——教材章节、Wikipedia 页面、任意本地文本。

**输出**：一张可溯源的知识图。每条边（Claim）都能回答三个问题：

- 这条知识出自哪一份快照的哪一段文字，逐字可定位
- 有几个**互相独立**的来源支持它，有没有反对的
- 它当前的裁决是谁在哪个策略版本下做出的

**不输出**：模型知道但语料没写的东西。

## 1.2 一条约束决定了全部形状

**LLM 的输出是 observation，不是知识。**

模型可以规划阅读、抽取、消歧、判关系、复核蕴含、翻译、排优先级。但它说的每一句
都必须落回语料的某一段原文，否则不入库。同一个模型问十次仍然是一个信源。

这条约束推出三个必须成立的性质，整套算法都是围绕它们设计的：

| 性质 | 含义 | 实现手段 |
|---|---|---|
| **可定位** | 每条证据都能在快照原文里逐字找到 | `evidence_in_text` 机械校验，定位不上整条丢 |
| **可累计** | 同一条 Claim 能持续吸收新证据，先来的不覆盖后来的 | Claim 与 Evidence 分表，canonical key 唯一 |
| **可复现** | 任何一次裁决都能在事后重放出当时的输入 | 版本号 + 追加表 + 裁决快照 |

## 1.3 两条入口

```
                   ┌──────────────────────────────────────┐
   语料快照 ───────▶│ 入口 A：读新文本 → 产生新概念和新 claim │
                   └──────────────┬───────────────────────┘
                                  │
                        Entity / Claim / Evidence
                                  │
                   ┌──────────────▼───────────────────────┐
                   │ 蕴含复核 → 证据计数 → Shadow Decision │
                   └──────────────┬───────────────────────┘
                                  │
                     卡在证据门槛下的 claim
                                  │
                   ┌──────────────▼───────────────────────┐
   语料快照 ───────▶│ 入口 B：定向补证 → 只找新的独立来源    │
                   └──────────────────────────────────────┘
```

**入口 A 是发现**：读一段没读过的文字，抽出里面的概念和关系。

**入口 B 是佐证**：给一条已经存在但证据不够的 claim，在语料里定点找第二个独立
来源。

入口 B 存在的理由是一个实测结论：**通读第二本教材几乎佐证不了已有 claim**。两本
书讲同一个概念，极少写出同一个三元组。nndl 第 3 章整章读下来产出 14 条 claim，
挂到已有 claim 上的是 0 条。

两条入口在「聚合 → 复核 → 计数 → 裁决」这一段走**完全相同**的代码，入口 B 没有
任何捷径。

## 1.4 状态机

**Observation**（模型的一次原始陈述）

```
pending ──端点落地──▶ resolved
   │
   └──契约违例──▶ rejected（终态，没有任何路径会重看）
```

端点暂时落不了地时**保持 pending**，等 `replay-pending` 重放。只有确定违反契约的
才 rejected。

**Entity**

```
proposed ──人工发布──▶ published
    │
    ├──合并──▶ merged（规范名转为目标实体的 verified alias，可撤销）
    └──拒绝──▶ rejected
```

**Alias**

```
proposed ──确定性快路命中 / 跨 ≥2 独立来源组累计──▶ verified
    │
    └──复核判定为独立概念──▶ rejected
```

只有 `verified` 别名参与实体消歧的精确匹配和身份名判定。

**Claim**

```
proposed ──裁决 approve──▶ published
    │
    └──裁决 reject──▶ rejected
```

当前**所有** claim 都停在 `proposed`：`decided_by='shadow'` 的裁决只追加记录，不
改状态。

**Evidence** 不是状态机，是累积的。同一条 Claim 上的 support / oppose /
uncertain 只增不减，后来的既不覆盖也不丢弃先来的。

## 1.5 版本坐标系

五组互相独立的版本号。每一组回答「改了它，什么会重跑」：

| 版本号 | 当前值 | 改了以后 |
|---|---|---|
| `pipeline.ALGORITHM_VERSION` | `grounded-pipeline-4` | 已处理过的 `(快照, 主题)` 重新变成可读 |
| `validators.VALIDATOR_VERSION` | `entailment-validator-1` | `reshadow --only-stale` 认为旧蕴含判定过期 |
| `validators.ENTAILMENT_PROMPT_VERSION` | `entailment-judge-2` | 同上 |
| `decision.POLICY_VERSION` | `claim-policy-4` | 只给裁决打标签，不触发重跑 |
| `entity_resolution.RESOLVER_VERSION` | `entity-resolver-5` | 消歧事件记录的版本 |
| `entity_resolution.ALIGNMENT_POLICY_VERSION` | `entity-alignment-policy-3` | 对齐证据记录的版本 |
| `targeting.ALGORITHM_VERSION` | `claim-targeting-2` | 定向补证的探测记录 |
| 注册表 `version` | `4` | 每次 `db.connect()` 同步进 `relation_definitions` |

**读库时必须先看版本。** 记录带的是产生它时的版本，不是当前版本。一条 claim 可能
有多代裁决，只看最新一条，并确认它是在当前策略下产生的。

---

# 第二部分：数据模型

## 2.1 五类核心记录

**Source / SourceSnapshot** — 来源与它的某个版本。

- 快照按 `content_hash` 去重，同一份文字不会存两遍
- `sources.independence_group` 是**独立性的唯一来源**。同一本书的不同章节、原文
  与译文、镜像站，`independence_group` 都相同，不算两个来源
- `sources.authority_profile` 是**按关系**的权威度，不是一个全局等级。教材对
  `prerequisite_of` 是 high，Wikipedia 不是

**Entity** — 一个概念。`normalized_name` 是身份，`canonical_name` 是显示名。一个
实体可以有多个 alias，但只有 `verified` 的算身份名。

**Claim** — 一条关系断言。canonical key：

```
(subject_id, relation, object_id, qualifiers_hash)
```

对称关系强制 `subject_id < object_id` 归一化，所以 `A—B` 和 `B—A` 是同一条记录。

**Evidence** — 一段支持或反对某个 Entity/Claim 的原文摘录。

- 必须且只能绑定**一个** entity 或 claim（schema CHECK 强制）
- 唯一键 `(target_key, source_snapshot_id, excerpt_hash, polarity)`
- `polarity` ∈ {support, oppose, uncertain} 是抽取时的立场
- `entailment` ∈ {unreviewed, supports, contradicts, insufficient} 是复核后的判定

**Decision** — 一次裁决。追加表，永不修改。

## 2.2 三张追加表

| 表 | 记什么 | 为什么必须追加 |
|---|---|---|
| `entailment_reviews` | 每一次蕴含判定，挂上产生它的 run | 换 prompt 重判后，旧裁决仍要能复现 |
| `targeting_probes` | 每一次定向探测的结果 | 同一段文字不付两次钱 |
| `merge_events` | 合并搬动了哪些行（`payload`） | 撤销要按它原样回滚 |

`evidence.current_entailment_review_id` 指向最新一次判定，`evidence.entailment`
只是它的缓存。`decisions.evidence_review_snapshot` 记下这次裁决基于哪些判定：

```json
[{"evidence_id": 12, "entailment_review_id": 34}, ...]
```

## 2.3 防重复处理

`pipeline_processed` 的键是
`(source_snapshot_id, coverage_topic_id, algorithm_version)`。同一份快照在同一
主题下、同一算法版本只处理一次。

---

# 第三部分：算法细节

## 3.1 文本分块 `split_text`

**输入**一节正文，**输出**若干块，每块单独送模型。

```
limit = 12000（目标，不是硬边界）

units = []
for 每个自然段 p（按 \n\s*\n 切分，去空）:
    units += 按句子边界切分 p   if len(p) > limit
    units += [p]                otherwise

chunks = []; current = ""
for unit in units:
    current += ("\n\n" if current else "") + unit
    if len(current) >= limit:
        emit(current); current = ""
emit(current) if current
```

规则：

- **永远不在段落中间切。** 一段跨过上限时，在这一段的**后面**断开——取长的那一侧
- 在前面断会留下一个刚好卡在上限的短块。抽取质量对上下文完整度敏感，对块长不敏感
- 整段就超过上限的退一步按句子断（`。！？；!?;`），仍然不从句子中间切

实测：58597 个自然段里最长 6401 字，**零段超过 12000**。按句子断这条路当前从不
触发，它只防将来的畸形输入。

**限额是每块的**：`--max-entities` / `--max-claims` 作用于单块。长章节的后半段
不能因为被分块就丢掉候选。

## 3.2 有据抽取 `extract`

每块一次 LLM 调用，走 `llm.pmap` 并发（全局信号量，默认 6）。

提示词里的三份词表全部由 `registry()` 生成：

- 允许的实体类型 ← `reg.entity_types`
- 关系及限定字段契约 ← `reg.extraction_contract()`
- 证据类型可选值 ← `reg.evidence_type_names()`

**代码里不许写第二份词表**，`tests/test_core_isolation.py` 会扫出来。

跨块合并按名字 / 三元组去重，先出现的胜出。

## 3.3 机械校验

### 两套归一化，不能混用

```python
_norm(s)          = re.sub(r"\s+", "", s).casefold()
normalize_name(s) = NFKC → strip → casefold → 空白折叠为单个空格
```

证据定位用 `_norm`：可以完全无视空白，因为 PDF 抽出来的换行位置不可靠。

身份判定用 `normalize_name`：要保留词间空格，`linear regression` 不等于
`linearregression`。

### `evidence_in_text(excerpt, text)`

```
1. 按 \.{3}|…+ 把 excerpt 切成若干段（允许模型用省略号跳过中间内容）
2. 每一段 _norm 之后都必须是 _norm(text) 的子串
3. 任何一段找不到 → False，整条证据丢弃
```

只容忍空白差异和省略号分段。**不做模糊匹配，不做编辑距离。**

### `parse_payload` 的五道闸

全部零 LLM，不合格的整条进 `rejected`：

1. `entity_type` 必须在注册表里
2. 实体的 evidence 必须 `evidence_in_text` 通过；名字不能为空；同批不能重名
3. claim 的 subject 和 object 必须**同时**出现在本批有效 entities 里
4. `validate_claim(..., active_only=True)` 只放行 `lifecycle: core` 的关系；
   qualifiers 按契约校验（含必填）；`evidence_type` 必须在注册表词表里
5. `next_reading_targets` 的 query 必须在正文出现，否则不登记

第 3 条是硬闸，也是当前召回上的主要限制（见 §6.2）。

## 3.4 实体消歧 `resolve`

八步，确定性优先，命中即返回。

```
1. find_canonical_entity(normalize_name(name))       命中 → same_entity
2. find_verified_alias_entities(...) 恰好 1 个        命中 → same_entity
3. _candidate_rows：SequenceMatcher 召回 ≤5 个候选     ← 只召回
4. LLM 分类 → {decision, candidate_id, canonical_name, match_type, confidence}
5. decision=existing 且 conf ≥ 0.80：
       direct = _validated_direct_match_type(name, canonical, match_type)
       safe   = direct ∈ {translation_alias, name_variant} 且 conf ≥ 0.95
       safe → alias 直接 verified，返回 same_entity
6. 否则 add_alignment_evidence 累计 → 达标才 verified
       未达标 → suspected_same_entity（claim 端点进 pending 等）
7. decision=new 且 conf ≥ 0.80 → 建实体
8. 其余 → ambiguous
```

第 1、2 步排除 `status IN ('rejected','merged')` 的实体。merged 实体的规范名在
合并时已经转成目标实体的 verified alias，排除它不丢可达性。

### 相似度只召回

`_candidate_rows` 用 `difflib.SequenceMatcher` 对 `normalized_name` 和各个 alias
算比值，取最高分排序取前 5。**这个分数从不直接决定合并**，它只决定给 LLM 看哪几
个候选。

### `_validated_direct_match_type` 是充分条件

```python
if _name_variant_roots(alias) ∩ _name_variant_roots(canonical):
    return "name_variant"
if claimed == "translation_alias" and _has_cjk(alias) != _has_cjk(canonical):
    return "translation_alias"
return None
```

`_name_variant_roots(s)`：先 `_compact_name`（casefold 后去掉空白和
`` ` ' " _ - — – · ( ) （ ） ``），然后**反复剥离**后缀直到不动点：

```
问题 方法 算法 模型 函数 运算 任务 估计
problem method algorithm model function task estimation
```

所以 `分类` 和 `分类问题` 的词根集都含 `分类`，判为 name_variant。

**命中是充分条件，不是必要条件。** 命中不了**不能**推出「不是同一个概念」——那种
情况走 §3.5 的语料声明累计。把充分条件当必要条件用，是这套算法上一版真实犯过的
错：不符合快路的别名当时没有任何转正通道。

### 累计对齐

`add_alignment_evidence` 每次调用往 `entity_alignment_evidence` 追加一行，然后
重算候选分：

```
按 independence_group 分组，每组只取最高置信度
score = 1 - Π (1 - conf_group)
```

升级为 verified 需要**三条同时成立**：

```
independent_groups ≥ 2
score ≥ 0.95
不存在竞争候选：同名指向别的实体、且分数 ≥ score - 0.10
```

或者 `direct_verify=True`（走了确定性快路）。

没有 `independence_group` 的证据（比如没绑快照的模型判断）**不参与**分组计数。

### 两个门槛不对称

```
LLM_NEW_ENTITY_CONFIDENCE = 0.80   建新实体
LLM_SUSPECT_CONFIDENCE    = 0.80   进入疑似队列
LLM_AUTO_LINK_CONFIDENCE  = 0.95   自动合并到已有实体
```

风险不对称：合并判错会污染图谱且难发现；新建判错只是多一个 proposed 实体，由重复
清扫（§3.10）和对齐队列兜底。低于 0.95 建出来的实体标
`below_auto_link_confidence`，重复清扫优先看它们。

## 3.5 别名的语料声明通道 `alias_evidence`

零 LLM。教材写作时会主动交代别名：

```
支持向量机（Support Vector Machine，SVM）
Logistic 回归也称为对数几率回归
全连接层（fully-connected layer）或称为稠密层（dense layer）
```

四个正则模式，别名与规范名两个方向各两条：

```
括号注释：{canonical}（…{alias}…）        内层限 MAX_INNER=40 字
又称句式：{canonical}…(又称|亦称|也称|简称|又叫|也叫|或称|记作|
                       缩写为|全称为|英文为|英文名为)…{alias}    两侧各限 20 字
```

每条声明置信度 `DECLARATION_CONFIDENCE = 0.9`，**同一来源组只取一条**（一本书写十
遍也只是一个来源）。代入累计公式：

```
1 个独立来源组：0.90        < 0.95   不转正
2 个独立来源组：1 - 0.1² = 0.99 ≥ 0.95   转正
```

0.9 这个取值是为了让「必须跨两个独立来源」成为算术后果，而不是额外加的规则。

**这是语料给的证据，不是模型判断。**

## 3.6 Claim 聚合 `materialize`

**先实体后 claim，顺序不能反。**

实体：写 observation → `resolve` → 成功则写 `entity_description` 证据 + 登记
proposed 别名 → 标 observation resolved。

claim 的端点解析 `_endpoint_id`，两级：

```
1. 本批 resolved 字典
2. 退回库里已有实体的确定性精确匹配（规范名 / 唯一 verified alias）
   —— 只走确定性快路，不做相似度、不调 LLM
```

端点正在等疑似对齐裁决时**跳过第 2 级**：此时认领任何实体都可能抢在裁决之前替它
做决定。

两级都落不了地的，observation **保持 pending 不标 rejected**。后续批次很可能建出
这个实体，届时 `replay-pending` 能把 claim 捡回来。

`replay_pending` 重放时会**重新过一次** `active_only` 闸：抽取时合法不代表现在还
合法，注册表可能已经收窄。

## 3.7 蕴含复核 `verify_entailment_batch`

严格三段，跨 claim 批量：

```
串行读库拼 prompt  →  llm.pmap 并行调用  →  串行落库
```

并发只发生在 HTTP 上，sqlite 始终单线程。**传给 pmap 的函数不得触碰连接**
（`check_same_thread=True` 会大声报错，这是有意的）。

单项失败返回异常对象而不是抛出，不让一条坏数据杀掉整批。

### 提示词按关系拼

- **对称关系**：明确告诉模型不要因为两个端点在原文里出现顺序相反就判 contradicts
- **有向关系**：必须检查方向
- **`part_of` 额外硬闸**：

```
用于 / 依赖 / 参与 / 帮助构建 / 产生 / 输入输出 / 属性 / 子类型 / 先后学习
——都不是 part_of
反事实检查：移除 subject 后，object 是否缺少一个正文明确承认的组成部分或阶段？
仅仅改变构建方式或用途不算。
```

并要求额外输出 `composition_explicit`。

### `_interpret` 的降级规则

```python
if relation == "part_of" and verdict == "supports" \
   and answer.get("composition_explicit") is not True:
    verdict = "insufficient"
```

模型说支持但没明确确认组成关系的一律降级。**默认值不算确认**，必须是显式 `true`。

### 只追加不覆盖

每次判定 `INSERT` 一行 `entailment_reviews` 并挂上 run，然后更新
`evidence.current_entailment_review_id`。

`stale_evidence_ids` 判断哪些证据的当前判定已过期，三类：

```
从未判过 / 判定早于留痕机制（run_id 为空）/ 判定来自旧版本
```

历史遗留的判定补了 review 行但 `run_id` 为 NULL——那些的模型和 prompt 版本已经不
可考，**不伪造版本号**。

## 3.8 证据计数与裁决 `evaluate`

### 两层过滤，管两件不同的事

```
第一层：全局 strength（is_assertive_evidence）
        这段文字是不是一句断言？
        toc_order / hyperlink / cooccurrence 是编排不是断言
        → 支持与反对【两侧都不计】

第二层：关系白名单 accepted_evidence_types（is_strong_evidence）
        这类断言能不能【建立】该关系？
        → 【只作用于支持侧】
```

**第二层不能套到反对侧。** 「特征是预测所依据的自变量」这句定义建立不了
`特征 part_of 样本`，但它确实是对这条 claim 的有效反驳。**建立不了 ≠ 反驳不了。**

把两层混成一层是真实犯过的错：那样会把三条本该人工复核的 claim 误降级成
`needs_more_evidence`。

当前词表：

```
强（断言）：explicit_definition   explicit_taxonomy     explicit_composition
            explicit_function     explicit_prerequisite explicit_comparison
            explicit_derivation
弱（编排）：toc_order   hyperlink   cooccurrence
```

三条 core 关系的白名单：

```
is_a            [explicit_taxonomy, explicit_definition]
part_of         [explicit_composition]                      ← 只此一种
prerequisite_of [explicit_prerequisite, explicit_derivation]
```

`explicit_function` 对 `used_for` 是强证据，对 `part_of` 恰是 description 点名排除
的语义，所以不在它的白名单里。

### 判据顺序，六步短路

```
1. validate_claim 端点类型不合法          → 抛异常
2. _would_cycle 有环                      → human_review
3. 存在断言性 contradicts                 → human_review
4. 没有能建立该关系的 supports            → needs_more_evidence
5. high ≥ explicit_high_authority
   且 independent ≥ independent_*_sources → high_impact_review ? human_review
                                                              : auto_approve
6. 否则                                   → needs_more_evidence
```

被两层过滤掉的证据都写进 `reasons`。**不算数不等于不可见。**

### 门槛取值

```python
_required_independent(policy):
    for key in ("independent_standard_sources", "independent_curriculum_sources"):
        if key in minimum: return int(minimum[key])
    return 2
```

用显式判空，不是 `or` 串联——配成 0 时 `or` 会静默滑到下一个默认值，那是配置被
忽略，不是配置生效。

三条 core 关系当前都是 `high_impact_review: true`，所以**第 5 步永远走不到
`auto_approve`**。

### 裁决的可复现性

`Validation.evidence_reviews` 记下依据的是「这些证据的**这一次**判定」，写进
`decisions.evidence_review_snapshot`。之后重判蕴含，旧裁决仍然复现得出来。

### 已知空转

`_would_cycle` 只遍历 `status='published'` 的 claim。当前没有任何 claim 是
published，所以这一步恒为 False。它在关掉 Shadow 之前不会生效。

## 3.9 定向补证 `targeting`

### 选目标

`stuck_claims`：最新裁决为 `needs_more_evidence` 的 claim。

### 机械检索（零 LLM）

```
left  = 主体的身份名集合（canonical + verified alias，长度 ≥ 2）
right = 客体的身份名集合

对每一段本地语料：
    跳过：已经支持该 claim 的独立来源组
    _cooccurrences(text, left, right, window=600)：
        对每一对 (左命中区间, 右命中区间)：
            区间重叠 → 跳过              ← 关键
            gap = 两区间之间的字符距离
            gap ≤ window → 收下
    按 gap 升序
```

**区间重叠必须排除。** 中文里一端常是另一端的子串（回归/线性回归、梯度下降/梯度
下降法）。位置重叠说明只是同一处文字被两个名字各匹配了一次，不是共现。这个 bug
真实产生过一条错 claim。

**候选的粒度是「一次共现」，不是「一节」。** 同一节里两处讲到同一对概念就是两个
候选，各自竞争名额。只取最近的一处会漏掉别处更明确的陈述。邻域重合的候选去重：
新窗口与已收下的窗口有交集就跳过。

### 取窗口 `_neighborhood`

```
lo = _snap_back(命中起点 - CONTEXT)       CONTEXT = 300
hi = _snap_forward(命中终点 + CONTEXT)

_snap_back：   SNAP_LIMIT(400) 内优先找段首（\n），其次句首，都没有就硬切
_snap_forward：同理向后
```

两个约束同时成立：

- **不能给整节。** 距离窗口必须限定模型读哪里，不只是筛选哪些节参与。一节动辄上
  万字，全给模型它会引用和命中位置无关的句子——真实发生过：检索命中在第 27 字，
  模型跑到第 14090 字引了一句讲 SVM *损失函数* 的话，产出一条错 claim
- **不能只给一句。** 判断关系需要上下文，孤立一句看不出是定义、举例还是并列

实测窗口 365–1263 字，中位 653，相当于命中处所在的一两段完整文字。

`Passage.text` 是送模型的窗口，`Passage.section_text` 是整节原文。**快照登记的是
整节**——窗口只约束模型看哪里，不改变 provenance 的单位。

### 中性抽取

提示词点名两个端点但**不说待验证的关系**，否则是诱导性提问。模型可以返回相反方向
或 `none`。

`_extract_one` 的校验：

```
relation ∉ {"", "none"}
{normalize_name(模型的 subject), normalize_name(模型的 object)}
    == {normalize_name(端点1), normalize_name(端点2)}    ← 集合相等，方向由模型定
evidence_in_text(evidence, 窗口文本)                     ← 对窗口校验，不是对整节
validate_claim(..., active_only=True)
validate_qualifiers(..., require_required=True)
validate_evidence_type(...)
```

方向按模型给的来：若模型的 subject 是端点 2，就反过来建 claim。

### 探测留痕

`targeting_probes` 唯一键 `(claim_id, ref, content_hash)`，`content_hash` 是**窗口
文本**的 sha256。同一段文字不付两次钱。

LLM 调用失败**不记 probe**——返回非法 JSON 不是结论，下一轮该重试。

## 3.10 重复实体清扫 `find_duplicate_candidates`

零 LLM，只报告不改数据。

```
对每一对实体 (a, b)：
    names_a = {a.normalized_name} ∪ a 的非 rejected 别名
    names_b = 同理
    shared    = names_a ∩ names_b
    score     = max over (x,y) SequenceMatcher(x, y).ratio()
    contained = 存在 x ≠ y 使 x ⊂ y 或 y ⊂ x
    收下条件：shared 非空 或 contained 或 score ≥ 0.6
```

阈值 0.6 是按中文调的：`感知机` / `感知器` 的 SequenceMatcher 比值只有 0.667。

排序：低置信度新建的排前 → 有共享名字的排前 → 分数高的排前。

存在的理由：`entity_alignment_candidates` 只在 `resolve` 时由 LLM 提议才产生，
**不会回头看已有实体**。降低新建门槛之后重复会变多，这个扫描补上那一半。

## 3.11 实体合并 `merge_entities` / `revert_merge`

不是自动流程的一部分。由重复清扫报告驱动、人工确认后调用。

```
前置：source ≠ target，两端都存在，source.status ≠ 'merged'

搬动：
  aliases.entity_id           : source → target
  evidence.entity_id          : source → target（同时改 target_key）
  claims.subject_id/object_id : source → target

两类 claim 不搬，标 rejected：
  合并后自指的（new_subject == new_object）
  合并后撞 canonical 唯一键的（视为同一条）

收尾：
  source.canonical_name 插入为 target 的 verified alias
      （alias_type='merged_canonical'）
  source.status = 'merged'
```

搬动的行 id 全部写进 `merge_events.payload`：

```json
{"aliases": [...], "evidence": [...], "claims_subject": [...],
 "claims_object": [...], "claims_dropped": [...],
 "source_status": "proposed", "source_canonical_name": "感知机"}
```

`revert_merge` 按 payload 原样回滚，包括把 dropped 的 claim 恢复成 proposed、删掉
那条 `merged_canonical` 别名、还原 source 的原状态。合并事件转 `reverted`，不能
撤销两次。

## 3.12 复核队列

四条，**顺序有依赖**：

| 顺序 | 队列 | 算法 | 自动升级的条件 |
|---|---|---|---|
| 1 | `align-aliases` | LLM 分类 proposed 别名 | 确定性快路命中且 conf ≥ 0.95 |
| 2 | `review-alignments` | LLM 复核 suspected 候选 | 同上 |
| 3 | `replay-pending` | 重放 pending observation | 端点确定性精确命中 |
| 4 | `review-type-conflicts` | LLM 复核类型冲突 | **永不自动改** |

第 1 条走不了快路的转 `alias_evidence.record` 走语料声明通道，不是死路。

第 4 条的结论只写 `model_queue_reviews`，`auto_changed` **恒为 False**。实体主类型
变更走人工。

顺序有依赖是因为：别名转正会让更多端点在第 3 步精确命中。

---

# 第四部分：复杂度与规模

设 N=实体数，C=claim 数，P=语料段落数，L=平均段落长度。

| 算法 | 复杂度 | 当前规模 | 何时成为瓶颈 |
|---|---|---|---|
| `split_text` | O(文本长度) | — | 不会 |
| `_candidate_rows` | O(N × 名字长度²) | N=114，每次消歧全表扫 | N 上千时 |
| `find_duplicate_candidates` | O(N² × 名字长度²) | N=114 → 6441 对 | N 上千时 |
| `find_passages` | O(P × L) 每条 claim | 全部语料读进内存 | **最先** |
| `_cooccurrences` | O(左命中数 × 右命中数) | — | 极高频词 |
| `evaluate` | O(该 claim 的证据数) | 一次 SQL | 不会 |

`targeting.find_passages` 最先顶不住：每次调用都通过 `local_corpus.passages` 把
全部语料读进内存，对 C 条 claim 就是 O(C × P × L)。语料规模上去要建倒排索引。

LLM 调用是实际的时间成本，不是算法复杂度。M3 一次抽取 1~3 分钟，全局并发上限
`MAX_CONCURRENCY`（默认 6，`KG_LLM_CONCURRENCY` 可覆盖）。

---

# 第五部分：全部可调参数

改这里的任何一个都要有基准证据，不是为了方便。

| 参数 | 位置 | 当前值 | 作用 |
|---|---|---|---|
| `limit` | `observations.split_text` | 12000 | 分块目标长度 |
| `LLM_NEW_ENTITY_CONFIDENCE` | `entity_resolution` | 0.80 | 建新实体门槛 |
| `LLM_SUSPECT_CONFIDENCE` | `entity_resolution` | 0.80 | 进疑似队列门槛 |
| `LLM_AUTO_LINK_CONFIDENCE` | `entity_resolution` | 0.95 | 自动合并门槛 |
| `MAX_CANDIDATES` | `entity_resolution` | 5 | 给 LLM 看几个候选 |
| `DUPLICATE_SCAN_THRESHOLD` | `entity_resolution` | 0.6 | 重复清扫相似度阈值 |
| `DECLARATION_CONFIDENCE` | `alias_evidence` | 0.9 | 每条语料声明的置信度 |
| `MAX_INNER` | `alias_evidence` | 40 | 括号注释内层长度上限 |
| 累计对齐门槛 | `store.add_alignment_evidence` | ≥2 组 且 ≥0.95 | 别名转正 |
| 竞争候选容差 | 同上 | 0.10 | 分差小于它就算有竞争 |
| `WINDOW` | `targeting` | 600 | 共现最大距离 |
| `CONTEXT` | `targeting` | 300 | 命中点前后保留字数 |
| `SNAP_LIMIT` | `targeting` | 400 | 向外找边界的最大距离 |
| `minimum_evidence` | 注册表，按关系 | 2 组 + 1 高权威 | 自动批准门槛 |
| `high_impact_review` | 注册表，按关系 | 三条 core 全 true | 强制人工 |
| `MAX_CONCURRENCY` | `llm` | 6 | LLM 全局并发 |

**不在这张表里的都是硬闸**，不能为了提召回放宽：

- evidence 逐字定位
- claim 端点必须在本批有效 entities 里
- 独立性只来自 `independence_group`
- 消歧快路只认精确匹配
- 一切裁决先 Shadow
- 注册表是唯一词表来源
- `llm.pmap` 的函数不碰 sqlite

---

# 第六部分：算法缺口

不是 bug 列表，是「当前算法做不到什么」。

**6.1 检索只有字面共现。** `targeting` 用纯字符串匹配 + 距离，没有任何语义召回。
同一节里离得远但相关的段落取不到，换个说法的表述也取不到。`llm.py` 有 embo-01，
新核心没用它。

**6.2 端点必须在本批 entities 里。** 模型没把某个概念列进 entities（比如撞上
`--max-entities` 上限），即使库里早有这个实体，claim 也会在 `parse_payload` 就被
丢掉。放宽它是可以论证的——端点实体已经带着自己的 evidence 存在库里，claim 自己
的 evidence 仍然逐字校验——但这是文档里写死的硬闸，改之前要先决定。

**6.3 身份名与证据脱节。** 一条证据可以既通过 `evidence_in_text`、又被判为
supports，却整段没提到端点的身份名。`pipeline identity` 报告当前 57 条 claim 证据
里有 18 条如此，其中 16 条仍算强证据，15 条来自抽取路径而非定向补证。这是最大的
开放数据质量缺口。

**6.4 没有对抗性复核。** LLM 现在担任五个角色：抽取器、实体链接器、关系分类器、
蕴含判定器、阅读规划器。第六个——对抗性批评者，被要求论证自己抽取器产出的 claim
是错的——没有实现。

**6.5 没有校准。** `evaluate` 用不上「同一策略桶的历史精度」这个特征，因为
`benchmarks/gold.jsonl` 只有 3 条负例（目标 300）。在有校准数据之前，任何自动策略
都没有开闸依据。

**6.6 `auto_reject` 从不产生。** 它在 schema 里合法，但 `evaluate` 的六步里没有
任何一步会返回它。机械无效、显式矛盾、校准过的负判定本该走这条路。

**6.7 覆盖规划器没接上。** `reading_tasks` 只写不读，`pipeline.batch` 按
`doc_sections.ord` 顺序选章节，扩展顺序对覆盖率是随机的。
