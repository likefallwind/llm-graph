# 算法逻辑

本文描述知识图谱的核心算法（代码在 `kg/`，语料库化架构已于 2026-07-03 实现）。
设计动机见 [plan.md](plan.md)，使用方式见 [README.md](README.md)。

## 0. 总体架构与核心原则

**核心原则：LLM 是信息抽取器，不是知识源。**
图谱内容必须扎根在外部权威语料（当前为 Wikipedia）上，LLM 只做判断题（边类型、粒度、证据摘取），
不做默写题（凭记忆生成知识）。理由：

1. **可校准**：语料快照固定后，换模型/换 prompt 重跑可 diff、可对照人工标注算准确率；
2. **不循环**：图谱以后要指导 LLM 教学、当 LLM 的事实锚，它本身就不能是 LLM 记忆的投影；
3. **可审核**：review 界面上是维基原文引文，人判断证据比判断 LLM 自由陈述便宜得多。

管道全景：

```
圈定领域子语料（corpus crawl/grow：内链扩散，本地缓存+revision）
  + 教材语料（docs add/fetch/translate：章节快照+目录序，英文源翻译后入库，§4.1）
  → 结构挖掘（mine aliases/categories/wikidata：别名/分类边/QID 关系，零 LLM）
  → LLM 批量抽取（ingest --batch [--doc]：只做判断——边类型/粒度/证据，evidence 机械校验；
     语料分级 §4.2：教材/教案 high 级优先提取，维基 mid 级兜底）
  → 去重 → proposed → 复核（verify：结构佐证+LLM 判断题，双重一致可自动裁决，批次可回滚）
  → 人工审核（review，裁决留痕）→ 守卫（check）
```

## 1. 数据模型与状态机

两张表（SQLite，`kg/db.py`）：

```
nodes:  id, name(唯一), aliases[], definition, facets[],
        status, source, embedding, created_at
edges:  id, src, dst, type, confidence, rationale, source, status, created_at
        UNIQUE(src, dst, type)
```

辅助表：语料三张（维基两张 §4 + `doc_sections` 教材章节 §4.1），`ingest_log` 防重复提取，
复核三张——`review_log`（每次人工/自动裁决留痕，校准数据；含 `batch_id`
批次号，自动裁决可整批回滚 §6.7）、
`review_signals`（结构佐证 + LLM 复核结论）、`page_qid`/`wikidata_claims`（Wikidata 缓存，§5.1）。

**status 兼作审核队列**，是整个半自动进化的骨干：

```
seed ──────────────┐
                   ├──> 生效（图算法只看这两种）
proposed ─审核a──> approved
    │
    └──审核r/m──> rejected（m 合并时边转移到目标节点后再 reject）
```

- LLM 产出的一切节点和边都先进 `proposed`。生效有两条路：人工审核，
  或 `verify --apply` 的双重一致自动裁决（见 §6.5；边可自动批准/拒绝，
  节点只自动批准，拒绝/降级一律人工）——
  两条路都写 `review_log`，自动放行的用 `review --audit` 抽检。
- 所有图算法（守卫、先修链、度数、选点）只在 `seed + approved` 子图上运行，
  未审核的提议污染不了图的结构性质。
- `source` 字段全程溯源：`seed:...`、`mine:category:<lang>`、`wikidata:<属性>`、
  `wiki:<lang>:<页面名>@<revision_id>`——锁定到页面版本，抽取可复现、可 diff。

## 2. 边类型语义（层次靠边表示，不靠节点属性）

| 类型 | 语义 | 约束 |
|---|---|---|
| `is_a` | A 是 B 的一种 | — |
| `part_of` | A 是 B 的组成部分 | — |
| `prerequisite_of` | 不懂 A 就无法理解 B（教学先修） | 生效子图必须是 DAG，守卫检测 |
| `related_to` | 受限关联 | 只允许三种 kind，见下 |

**related_to 限制**：“相关”太模糊——绝大多数概念都能说相关，所以它是一个封闭枚举
（`db.RELATED_KINDS`），LLM 必须在 `kind` 字段注明属于哪一种：

- **同题替代**：A 和 B 是解决同一问题的两种方法（GRU vs LSTM）
- **演化启发**：A 直接启发或发展出 B
- **教学对比**：教材常把 A 和 B 放在一起对比讲解（L1 vs L2 正则化）

“同属一个领域”“正文顺带提到”不算相关。双保险：prompt 写明规则，代码层再把没有合法
kind 的 related_to 直接丢弃（统计为 `dropped_related`）。通过的边把 kind 记入
rationale 前缀（`[同题替代] ...`），审核时可见。

**prerequisite 的特殊性**：先修是最有教学价值、却最不“可抽取”的边——
维基正文几乎从不写“学 B 之前必须先懂 A”。处理方式：

- 这是唯一允许 LLM 推断的边：**两个概念都出现在正文中**且教学依赖明确时可推断，
  但必须标 `inferred: true`——代码层加 rationale 前缀 `[推断]`、confidence 封顶 0.6，
  与有据边区分信任层级；
- ［以后］引入真正编码了先修顺序的语料：教材章节顺序（d2l 本身就是）、课程大纲。
  “权威来源”不必只是维基。

## 3. 节点粒度：facet 升级规则

候选知识点**默认不是节点**，而是父节点的一个 facet（字符串，≤12 字名词短语，
如 softmax 的「温度参数」）。只有当别的知识点需要单独依赖它时（要给它连边了），
才升级为独立节点。粒度是随图长出来的，不预先设计。

对应到提取 prompt 的硬规则：算法的内部步骤、阶段、参数、公式细节一律进 facets；
拿不准时宁可放 facets。代码层再过滤一次（facet 长度 ≤16 字符才收）。

守卫里的 `facet_shadows` 是这条规则的逆向检查：某个 facet 文本与已有节点重名，
说明它已经被升级了，应从父节点的 facets 里移除。

## 4. 语料层：领域定向语料库（`kg/corpus.py` + `kg/wiki.py`）

不做全量 dump（zhwiki 2–3 GB / 140 万+ 条目，95% 与领域无关），
而是**领域定向爬取 + 本地缓存**（以后要“包括所有知识”时再换 dump，抽取层不变）：

```
corpus 表：   lang, page_id, title, revision_id, text,
              redirects[], categories[], links[], fetched_at
node_page 表：node_id -> (lang, page_id)   节点↔页面显式关联
```

- `corpus crawl`：把生效节点缺失的页面抓进语料库（zh 优先、en 兜底，跟随重定向）；
  `corpus grow --limit N`：抓取被语料内链引用最多、尚不在库的页面——图长到哪，语料圈到哪。
- 记录 `revision_id`：语料即快照，任何时候可取回当时版本，抽取结果可复现、可 diff。
- **搜索相关性门槛**：维基搜索是全文模糊匹配，可能返回毫不相干的页面
  （实测「Xavier初始化」搜到过病毒条目），所以页面标题或任一重定向必须与查询词
  相似度 ≥0.75 才收录。子串命中不再直接满分：还要求短串占长串长度比 ≥0.6
  （防「目标检测」命中「维奥拉-琼斯目标检测框架」）；阈值 0.75 是错例回归定的
  （「深度Q网络」vs 重定向「深度神经网络」= 0.727 必须拦住）。
- **消歧义页守卫**：消歧义页只是同名词条列表，且常带精确重定向
  （「Pooling」→ en:Pool），字符串相似度拦不住，按分类标记
  （`disambiguation pages` / `消歧义`）直接拒收（crawl 与 grow 都查）。
- **相似度的盲区**：短别名与无关页面精确撞名（「GRU」= 神偷奶爸角色页 en:Gru、
  「DQN」= 日本网络用语 zh:DQN）任何字符串手段都无法区分，
  只能删错误映射后用 `node_page` 手动指定正确页面。
  **重定向也会精确撞名，且更隐蔽**：zh「UMAP」重定向指向「亞太大學交流會」
  （高教组织；降维算法 UMAP 无中文条目），2026-07-05 实测 expand 的假设经此重定向
  通过相关性门槛，主题提取把错误页面的内容成套提进图谱，
  又因名字精确命中而被自动批准（见 §6.5 盲区）。
- **node_page 显式映射**：节点名和页面标题未必字面相等（「特征值与特征向量」↔
  页面「特征值和特征向量」），关联在抓取时显式落表，不依赖字面匹配。
- API 访问统一节流（≥1s 间隔）+ 429 指数退避重试。

### 4.1 文档语料通道（`kg/docs.py`，2026-07-05 新增）

维基是百科文体，系统性缺教学性内容（先修顺序、教学对比在正文里极稀疏），
所以补第二个语料层：教材/课程讲义。与维基语料平行，共用全部下游约束。

- **声明式源配置**（`sources/<book>.yaml`）：book slug、语言、类型（html/pdf）、
  章节列表 `[ord, sec_id, 标题, 定位串]`。加源=加一个 yaml，不改代码。
  首批四源：d2l-zh（中文 HTML）、cs231n（英文 HTML）、cs229 / sutton-barto（英文 PDF）。
- **抓取**（`kg docs add/fetch`）：html 用 stdlib 提取器（`kg/htmltext.py`，
  优先 main/article 区域、剥代码块）；pdf 用 poppler `pdftotext` CLI 按页码范围取文
  （PDF 缓存 `data/docs/`，只下载一次）；任意节可用 `path:` 指向本地文本兜底。
- **翻译即快照**（`kg docs translate` 或用到时 lazy 触发）：英文源用翻译模型
  （`llm.TRANSLATE_MODEL`，默认 minimax-m2.7，同 endpoint）按段落分块（≤3000 字）译为中文，
  整节原子写入 `doc_sections.text` 并落 `content_hash`（sha256 前 12 位）。
  **evidence 对翻译后的中文文本做子串校验**——翻译文本入库即快照，不变式不破坏；
  重翻译 → hash 变 → source 变新版本，同锚点可重提取，近重复由去重流水线兜住。
- **source 格式** `doc:<book>:<sec_id>@<content_hash>`，与 `wiki:<lang>:<title>@<rev>` 平行，
  verify 复核取上下文两种都认（hash 与当前不符时用当前文本并注明已更新）。
- **章节顺序**（`ord` 列）是 §6.5 toc 先修信号的数据源：教材目录天然编码教学先修顺序，
  这是比 LLM 零样本判先修（研究显示勉强赢简单启发式）硬得多的独立信源。
- 概念↔章节映射（`section_for_node` / `concept_positions`）：节标题命中优先
  （相似度阈值同维基 0.75），其次正文首次出现；短名守卫——<3 字符不匹配、
  ASCII 名要求词边界（防「GRU」式误命中）。

### 4.2 语料质量分级（`kg/quality.py`，2026-07-06 新增）

从知识点提取的角度，教材/教案是最高质量语料（教学性关系——先修顺序、教学对比——
密度远高于百科文体），维基其次。显式三级，新语料源按级别声明式接入：

| 级别 | 来源 | 用法 |
|---|---|---|
| **high** | 种子骨架、教材/教案/讲义（docs 通道；`sources/*.yaml` 可用 `tier` 字段覆盖，缺省 high） | 批量提取的首选（`ingest --batch N --doc`，日常循环排在维基批量之前） |
| **mid** | 维基提取、mine 结构挖掘、Wikidata | 覆盖面兜底提取 + 结构佐证来源 |
| **low** | 弱校对语料（博客/论坛等，暂未接入，保留级别） | **一律不参与 `verify --apply` 自动裁决**，只能人工审 |

判定按 source 前缀（`quality.source_tier`），doc 来源查所在书的 yaml 配置（缓存）。
以后接入新语料只需加抓取通道 + 定级；下游（选点顺序、自动裁决把关）自动生效。
calibrate 的通道维度天然细于 tier，攒够裁决数据后可按 tier 汇总看各级 precision。

## 5. 结构挖掘：零 LLM 的免费标注（`kg/mine.py`）

语料在本地后，维基自带结构直接变成标注数据，不花一次 LLM 调用：

| 维基结构 | 变成什么 | 去向 |
|---|---|---|
| 重定向表 | 别名（`mine aliases`，实测一轮 +80 别名） | 直接生效并喂去重第 1 级；重定向撞上别的节点名时报“疑似同一概念”供人工裁决 |
| 分类树 | part_of 候选边（`mine categories`，conf 0.5） | 进 proposed 人工审（维基分类混有维护性分类，宁保守） |
| 内链密度 | 概念重要性 + 图谱缺口分 | `ingest --batch` 与 `corpus grow` 的选点依据 |
| Wikidata claims | is_a/part_of/related_to 候选边（`mine wikidata`，conf 0.7，见 §5.1） | 进 proposed；人类校对过的类型化关系，比分类树干净 |

LLM 的工作被压缩到它不可替代的部分：边类型判断（尤其先修）、粒度裁决、证据摘取。

### 5.1 Wikidata 通道（`kg/wikidata.py`）

每个语料页面对应一个 Wikidata QID（`page_qid` 表缓存），QID 之间有类型化 claims
（`wikidata_claims` 表缓存，只存关心的属性）。映射：

- `P279`（子类）→ `is_a`；`P361`（组成部分）→ `part_of`；
- `P737`（受启发于）→ `related_to`，方向翻转为 启发者→被启发者，rationale 前缀 `[演化启发]`；
- **同 QID 仲裁**：两个节点共用一个 QID，要么是同一概念（该合并），要么共享来源页
  （如前向/反向传播同页），报警人工裁决——这比 embedding≥0.90 硬得多的去重信号；
- 歧义 QID（多节点共用）不做边的源头和目标，claims 归属不明会指错边
  （实测曾产生「BERT -is_a-> 位置编码」）。

## 6. 流水线

### 6.1 种子导入（`kg/seed.py`）

人工校验过的 YAML（d2l.ai / CS229 / CS231n 大纲整理）→ 入库，status 直接 `seed`。
LLM 只在这副骨架的边缘扩展，不凭空生成拓扑。导入后批量算 embedding、跑守卫。

### 6.2 来源提取 ingest（`kg/ingest.py`）——主通道

围绕一个**已存在的锚点节点**，从语料库读取它的页面（缺页自动抓取入库）并提取：

```
锚点 → node_page 映射 / 语料匹配（缺页现抓）
  → 正文 ≥300 字才算有效来源
  → 分块（split_blocks）+ 选块（pick_blocks）→ 逐块 M3 提取（约束见下）
  → anchor_facets 直接生效（有正文依据且不改拓扑）
  → 每个候选概念走去重流水线（§6.4）
  → 未重复者入库 proposed，边 rationale = 正文证据引文
  → ingest_log 记录（锚点, 页面@版本），同版本不重复提取
```

**分块提取**（2026-07-05 取代旧的 7000 字整页截断——旧法系统性丢掉长页后半部分，
而变体/比较/演化恰恰集中在那里）：按维基 extract 的 `== 节标题 ==` 切节
（跳过参考文献/外部链接类节），超长节按空行段落再切，相邻小块贪心合并至 ≤6000 字；
选块规则：首块（导言）必选，其余按锚点名/别名出现密度取满 3 块（`MAX_BLOCKS` 成本保险丝），
保持原文顺序。块间 LLM 调用并行（§8 并发闸门），全部完成后才入库（中途失败不留半页产物）；
evidence 校验一律对整页全文；ingest_log 仍整页一条（选块确定性、一次跑完，不支持断点补块）。

**产出预算**：`--limit`（默认 6）是**每页**的概念产出上限，平摊到各块
（每块 prompt 写 `1~⌈limit/块数⌉ 个`），合并去重后超预算截断——
分块不放大"宁缺毋滥"的总闸门，limit 控制的是图谱增速与审核队列长度，不是调用次数。

**教材通道**（`ingest_topic_doc`，`kg ingest <锚点> --doc [BOOK]`）：锚点 →
`docs.section_for_node` 找章节（英文源在此触发 lazy 翻译）→ 走同一个 `_extract`，
source 记 `doc:...`，约束与 wiki 通道完全一致。

**教材批量模式**（`kg ingest --batch N --doc [BOOK]`，2026-07-06 新增）：教材是
high 级语料（§4.2），批量提取优先走这里。`pick_anchors_doc` 缺口驱动选锚点：
「教材单独设节讲解（节标题命中，权重 2）或正文提及（权重 1）、而图谱稀疏」的
生效节点优先，`score = 命中权重 / (1 + 生效度数)`；跳过同一节版本已提取过的锚点
（未翻译节 hash 未定，按节前缀查重）。

提取的核心约束是**有据可查**：

1. 只提取正文明确讨论的概念和关系，禁止 LLM 凭记忆补充；
2. 每个概念、每条边必须附正文原文摘录（≤60 字）作为 evidence
   （唯一例外：先修边允许推断级，见 §2）；
3. **evidence 机械校验**（`evidence_in_text`）：摘录规范化（去空白、小写、按省略号切段）后
   必须逐段是来源正文的子串，否则整个概念/边丢弃（`dropped_no_evidence`）——
   LLM 完全可能编造一句"原文"，这一行校验是后续一切佐证的地基。
   丢弃前有一次**逐字重引**机会（`_requote_failed`，2026-07-06 新增）：概念/边往往
   是对的、只是引文被改写，让 LLM 对着原文重新摘录一次（只一轮，不做多轮修正循环），
   重引后仍须通过同一校验——不变式不破坏，召回率白捡（统计为 `requoted`）；
4. 粒度规则（§3）优先级最高；
5. related_to 必须带合法 kind（§2）；
6. 宁缺毋滥，每次最多提取 `--limit` 个概念。

**批量模式**（`ingest --batch N`）：缺口驱动选锚点——
`score = 语料内链入度 / (1 + 图谱生效度数)`，即“语料显示它重要、图谱里却很稀疏”
的节点优先；跳过 ingest_log 里同版本已提取过的锚点。

### 6.3 expand：假设生成器（`kg/expand.py`）

LLM 记忆负责“往哪看”，语料负责“什么是真的”：

```
M3 凭知识提议“图谱缺个 XX”（只报名字+理由，不编内容）
  → 语料验证：本地语料 / 维基检索（name 和 aliases 都试，过相关性门槛）
      找到来源页 → 转主题模式提取（XX 本身+它与已有节点的有据边，走 §6.2 全部约束）
      找不到     → 丢弃（假设不成立或不可考证）
```

LLM 记忆的不可校准性被隔离在选点环节，不进图谱内容。
前沿选择（`pick_frontier`）：生效节点按 `(生效度数升序, 加入时间降序)` 取前 k。

已知盲区：语料验证只保证「这个名字有来源页」，不保证该页就是提议所指的概念——
多义短名/缩写词可被重定向劫持到无关页面（UMAP 事故，§4 / §6.5）。

### 6.4 去重（`kg/dedup.py`）

四级流水线，逐级升高成本：

```
1. 精确匹配：name / alias 命中（不区分大小写）           → 命中即重复
2. 维基重定向：name 重定向到的页面已对应某节点          → 命中即重复（人工维护的同义词典）
3. embedding 召回："{name}：{definition}" 算 query 向量，
   与全部未拒绝节点的库向量算 cosine，≥ 0.90 进候选       → 只召回，不裁决
4. LLM 三元裁决：对相似度最高的前 3 个候选逐一问 M3
   same（同义词/译名/缩写）/ facet（是对方的内部细节）/ different
```

- 第 4 级是**三元**而非二元（2026-07-06 改）：embedding 高相似 ≠ 同一概念——
  粒度不同的近邻（「温度参数」vs「softmax」）合并为别名是错的。判 `facet` 时
  该名字降级为已有节点的 facet（不建节点、不挂别名，其边一并丢弃，
  统计为 `demoted_facets`；名字 >16 字符不降级，按 different 处理）。
- 判定重复 → `merge_as_alias`：新名字挂为已有节点的别名，不建新节点。
- 未重复 → 返回已算好的 embedding 直接复用入库，避免二次调 API。
- embedding 用 MiniMax embo-01（入库 `db` / 查询 `query`），阈值 `SIM_THRESHOLD = 0.90`。
- `mine aliases` 把重定向名批量转成别名后，多数同义词在第 1 级就被拦下。

### 6.5 复核层 verify（`kg/verify.py`）——人工审核走向 AI 为主的桥

给 proposed 条目积累**两个相互独立的信源**的意见，写入 `review_signals` 表：

**结构佐证（零 LLM，`kg verify --no-llm`）**——把维基里人类校对过的结构搬来交叉验证：

- 互链：边两端节点的语料页是否互相链接（双向/单向/无）；
- RefD 式先修方向信号（仅 prerequisite_of）：讲解 dst 的页面引用 src 而 src 页不引用 dst
  → `refd=+1` 支持先修方向；只反向引用 → `refd=-1` 方向可疑；
- **toc 教材目录序信号**（仅 prerequisite_of，2026-07-05 新增）：所有共同出现的教材里
  src 首现章节序 < dst → `toc=+1` 支持先修方向；反之 `toc=-1`；多书矛盾或同节 → `toc=0`
  （宁可无信号不可假信号）；无共同教材不写键。不依赖维基页面存在，纯教材来源的边也有信号。
  这是推断先修边（无正文证据、LLM 复核帮不上）缺失的那个独立信源；
- Wikidata：两端 QID 之间是否存在 P279/P361/P737 claim（读 §5.1 的缓存）；
- 节点信号：是否有语料页、名字是否精确命中页面标题/重定向。

**LLM 判断题复核（`kg verify --limit N`）**——与提取不同的视角，节点优先：

- 只给材料（evidence 及其上下文节选、双方定义、图谱邻域），只回答判断题，
  prompt 明令禁止使用模型记忆——复核时 LLM 也不是知识源，语料才是
  （裸判的 LLM 验证器倾向来者不拒，必须让它做阅读理解题而非知识回忆题）；
- 边：支持/不支持/证据不足 + 方向是否正确；结构挖掘边（无正文证据）给两端页面开头；
- 节点：独立概念/应为 facet（归哪个概念）/证据不足——粒度裁决进复核；
- 逐条 try/except，单条截断失败不杀整轮。

**自动裁决（`kg verify --apply`，保守）**：两个独立信源一致才动，
每次运行共用一个批次号（`auto-YYYYMMDD-HHMMSS`）写入 `review_log.batch_id`，
误放行可 `kg rollback` 整批撤销（§6.7）——

- **分级把关**（§4.2）：low 级语料的条目一律不自动裁决，只能人工审；
- 节点只自动批准：LLM 判「独立概念」+ 结构信号 `has_page` 且 `exact_title`
  （名字精确命中语料页标题/重定向）+ **`neighbor_overlap`（语料页与图谱邻居有重叠，
  UMAP 撞名补丁，见下）**；拒绝与降级 facet 一律留人工
  （降级要人选归属概念）。先裁节点再裁边——节点生效后其关联边才能进入边裁决，
  刚生效节点的边通常还没有 LLM 结论，下一轮 verify 补上；
- 边批准：LLM「支持」且方向无疑 + 至少一项结构佐证（页面链接**任一方向**即算 /
  Wikidata / toc=+1）+ 先修边额外要求 refd 与 toc 都不反对——
  推断先修边 + LLM「支持」+ 教材目录序一致即可自动批准。
  **无环类型（先修/is_a/part_of）批准前在生效子图上模拟加边查环，成环留人工**
  （守卫从"事后报告"前移为"放行拦截"，批一条更新一条邻接表，
  自动放行不可能把环引入生效图，2026-07-06 新增）；
- 边拒绝：LLM「不支持」且无任何结构佐证；
- 其余留人工。自动裁决记 `review_log(decided_by='auto', batch_id=...)`，
  用 `kg review --audit N` 抽检；抽检错误率高就该收紧规则。

不用 LLM 自报的数值置信度做分流：自报置信度校准差（错的时候照样报高分），
「两个独立信源是否一致」本身就是更可靠的置信度。

**UMAP 撞名补丁（2026-07-05 事故，2026-07-06 已收紧）**：`exact_title` 对多义
短名/缩写词恰恰是危险信号而非佐证——重定向劫持来的错误页面「名字精确命中」
天然成立，LLM 复核只读到该页材料也会判「独立概念」，两个信源一起被同一个
错误来源污染，不再独立。实测「亚太大学交流会」（zh「UMAP」重定向所指）被
自动批准入库，人工发现后 reject（review_log 留痕，恰好是一条自动放行被推翻的
校准数据）。对策已实现：结构佐证新增 `neighbor_overlap` 信号
（`_neighbor_overlap`）——节点语料页的内链/正文是否提到它在图谱中的邻居概念
（错误页面不会提到真概念的邻居；名字 <3 字符不匹配、ASCII 名要求词边界）；
无重叠或无邻居可查（孤立提议）都不给自动放行开门，review 队列里也把
「名字命中但无重叠」排进信号冲突层（最优先人工看）。

### 6.6 审核（`kg/cli.py` review）

先审节点再审边（边要求两端节点均已生效才出现在队列里）。队列按复核信号分层排序，
人工时间优先花在信号冲突上（`_triage`）：

- **0 信号冲突**（排最前）：LLM 与结构佐证矛盾（如「不支持」但有互链/Wikidata 佐证、
  「支持」但 refd 反对、方向存疑）、节点判「应为 facet」（需人选归属）、
  判「独立概念」但名字未命中语料页；
- **1 证据不足/未复核**：LLM 判证据不足，或还没跑过 verify；
- **2 信号弱一致**：信号同向但没达到自动放行门槛（如边「支持」但无结构佐证）。

每条展示分层标签与佐证行：

- 节点：**a** 批准 / **r** 拒绝（级联拒绝其关联的 proposed 边）/
  **m** 合并到已有节点（名字变别名，边转移到目标节点，原节点 reject）/
  **d** 降级为已有节点的 facet（粒度裁决落地）/ **s** 跳过。
- 边：a / r / **f** 方向反了（翻转并批准，反向边已存在则拒绝）/
  **t** 改类型并批准（related_to 需补 kind）/ s。
- **每次裁决写 `review_log`**（动作、来源通道、human/auto）：这是校准自动放行阈值的
  标注数据——攒够裁决后按（通道 × 边类型 × 佐证组合）统计 precision（`kg stats` 汇总），
  高精度组合放宽自动裁决，人工逐步从守门员转为抽检员。
- 审核结束自动跑一遍守卫。

### 6.7 批次回滚（`kg rollback`，2026-07-06 新增）

自动裁决的"错了能低成本撤销"通道（与 §6.5 的两道拦截闸共同构成 UMAP 类事故的
完整防线：邻居重叠降低放行错误概率，环检测保证放行不破坏结构不变式，
回滚保证错了能整批撤销）：

- `kg rollback`（不带参数）：列出所有自动裁决批次（时间、批准/拒绝数）；
- `kg rollback <batch_id>`：该批次的自动 approve/reject 条目整批**退回 proposed**
  （重新排队人工审，而非直接 reject——裁决权在人）；
- **后来的裁决优先**：条目在该批次之后另有 review_log 记录（人工确认/推翻过）则跳过；
- **级联**：节点回滚时把它名下仍 approved 的边一并退回 proposed
  （端点未生效的边不该保持生效）；
- 回滚本身也写 `review_log(action='rollback', batch_id=...)` 留痕，结束后跑守卫。

## 7. 一致性守卫（`kg/guards.py`）

每次 seed / review / check 后在**生效子图**上运行：

| 守卫 | 算法 | 含义 |
|---|---|---|
| 环检测 ×3 | prerequisite / is_a / part_of 子图各跑三色 DFS | 先修与层级都必须无环，出环必有错边 |
| 先修传递冗余 | 直连边 A→C 存在绕开该边的 A→…→C 路径 | 冗余直连搅乱教学路径，考虑拒绝直连边 |
| 正反向同型边 | SQL 自连接找 A→B 与 B→A 同类型并存 | related_to 为重复；其他类型必有一条方向错 |
| 孤儿检测 | 生效节点无任何生效边 | 知识点悬空，接不进教学路径 |
| facet 重名 | facet 文本 ≍ 任一节点名/别名（不区分大小写） | facet 已被升级，父节点该清理 |

守卫只报告不自动修——裁决权在人。

入库层的配套拦截：`db.add_edge` 对 `related_to`（语义对称）在插入前查反向未拒绝边，
已存在即视为重复跳过——ingest / mine / wikidata / seed 四个调用点自动生效。

## 7.1 校准（`kg/calibrate.py`，`kg calibrate`）

零 LLM，把 review_log × review_signals 变成自动放行阈值的标注数据汇总：

- **人工裁决 precision**：按（通道 × 节点/边类型 × 佐证组合桶）分组，
  precision = approve/(approve+reject)，n<3 标注样本不足；merge/flip/retype/demote
  算「改动」单列不进分母。高精度组合可放宽自动裁决规则，低的收紧。
- **自动放行抽检推翻率**：`review --audit` 的 audit_reject/(confirm+reject)，按通道分组。
- 已知取舍：review_signals 是最新态（可能晚于裁决更新），precision 是近似；
  严格快照需要裁决时冻结信号副本，暂不做。

## 8. LLM 调用层（`kg/llm.py`）

- 对话模型 MiniMax-M3（reasoning 模型，思考可以很长，不设 max_tokens 限制）；
  翻译用 `TRANSLATE_MODEL`（minimax-m2.7，同 endpoint，`KG_TRANSLATE_MODEL` 可覆盖）。
- **全局并发闸门**：所有 chat/embed 请求过同一个信号量，上限 `MAX_CONCURRENCY`
  （默认 6，`KG_LLM_CONCURRENCY` 可覆盖）；并行入口 `llm.pmap`——verify 复核批、
  翻译分块、ingest 分块都走它，fn 内不碰 sqlite 连接（prompt 准备与落库仍串行）。
  429/5xx 指数退避重试（并发后限流概率上升）。
- `chat_json`：容忍 markdown 代码块包裹，用括号配对（忽略字符串内字符）截取 JSON；
  解析失败时自动发起第二次调用请模型充当”JSON 修复器”（temperature 0.1）再解析一次。

## 9. 教学接口（`kg/export.py`，为以后预留）

`neighborhood(name)` 返回一个节点的教学上下文 JSON：定义、facets、直接先修、
先修链（沿 prerequisite 反向 BFS 收集全部祖先）、下游解锁（unlocks）、is_a/part_of 分类边。
未来个性化教学系统只依赖这个接口，不直接碰库。

## 10. 日常进化循环

```
kg corpus crawl            # 补齐生效节点的页面（新节点批准后跑一次）
kg corpus grow --limit 10  # 沿内链扩语料（可选）
kg docs fetch <book>       # 教材语料增量抓取（首次先 kg docs add sources/<book>.yaml）
kg docs translate <book> --limit N   # 英文源预翻译（可选，不跑则用到时 lazy 翻译）
kg mine aliases            # 重定向→别名（零 LLM，随时可跑）
kg mine categories         # 分类→候选边（零 LLM）
kg mine wikidata           # Wikidata claims→候选边 + 同 QID 仲裁（零 LLM）
kg ingest --batch 3 --doc  # 教材缺口驱动批量提取（high 级语料，主通道优先）
kg ingest --batch 3        # 维基缺口驱动批量提取（mid 级，覆盖面兜底）
kg expand                  # 假设生成器补盲区（辅助）
kg verify --limit 20       # 结构佐证（含 toc/邻居重叠）+ LLM 判断题复核
                           #   --apply 双重一致自动裁决（输出批次号）
kg review                  # 人工裁决剩余队列（--audit N 抽检自动放行）
kg rollback <batch_id>     # 发现坏批次时整批撤销（不带参数列批次）
kg calibrate               # 定期看各通道×佐证组合的 precision，调自动裁决松紧
kg check → kg viz
```

未做（记在 plan.md 里程碑）：burn-in 质量校准实验（calibrate 已就绪，
攒金标裁决后跑）、教学接口资源映射（neighborhood 输出附讲解章节/页面）、
误区（Misconception）facet 提取、学习者掌握度层（状态节点 + 只追加证据 +
行为信号不让 LLM 猜）、review 节点批准后就地顺带裁决其关联边
（上下文只读一次，review_log 照常逐条记录；拒绝方向的级联已存在）。
