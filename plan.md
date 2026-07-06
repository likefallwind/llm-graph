# 自进化知识图谱

## 目标

1. 能够根据相关知识，自己产生相应节点（通过 LLM）
2. 图谱以后可以很大，希望包括所有知识
3. 图谱要考虑知识点的层次问题（如 神经网络 vs 神经元 不是一个层次）
4. 后续指导 LLM 进行个性化教学
5. 图谱可以随领域扩充而自己增长

当前阶段：围绕 AI 领域建立知识图谱 MVP。

## 已确定的设计决策（2026-07-03）

1. **半自动进化**：LLM 提议，人工批准。所有 LLM 产出先入 `proposed` 状态，图算法只在 approved 子图上运行。
2. **层级不用节点属性表示，用类型化的边表示**，共 4 种：
   - `is_a`（CNN is_a 神经网络）
   - `part_of`（神经元 part_of 神经网络）
   - `prerequisite_of`（神经元 prerequisite_of 神经网络 —— 教学先修，必须构成 DAG）
   - `related_to`（受限关联：绝大多数概念都能说"相关"，所以只允许三种情形——同题替代 / 演化启发 / 教学对比，LLM 必须注明是哪一种（kind 字段，记入 rationale 前缀），说不出来的边代码层直接丢弃）
3. **节点粒度：facet 升级规则**。候选知识点默认作为父节点的 facet（如"温度参数"是 softmax 节点的 facet），只有当它需要自己的边时才升级为独立节点（如加入"知识蒸馏"时，它依赖的是温度参数而非整个 softmax）。粒度是随图长出来的，不预先设计。
4. **不用现成框架**（Graphiti 等），自建轻量 MVP：Python + SQLite + embedding，零框架依赖。
5. **种子来自人工校验过的材料**（d2l.ai 目录、CS229/CS231n 大纲），LLM 只在骨架边缘扩展，不凭空生成拓扑。
6. **LLM 教学结合推迟**，现在只保留一个接口：节点邻域导出为结构化 JSON（定义 + facets + 先修链 + 下游）。
7. **模型**：显式调用 LLM 用 MiniMax M3（`MINIMAX_API_KEY`，reasoning 模型，需给足 max_tokens）；embedding 用 embo-01。
8. **LLM 是抽取器，不是知识源**：图谱内容必须扎根在外部权威语料上，否则以后无法校准 LLM、也无法当 LLM 的事实锚（循环论证）。落地路线：领域定向语料库（从种子沿内链/分类扩散圈子语料，本地缓存+revision_id，不做全量 dump）+ 结构挖掘（重定向→别名、分类树→候选边、链接密度→选点，零 LLM）+ LLM 批量抽取只做判断题。expand 降级为假设生成器（提议须经语料验证转 ingest，否则丢弃）。详见 algorithm.md。
9. **语料质量分级（2026-07-06）**：从知识点提取的角度，教材/教案是最高质量语料（教学性关系密度最高），维基其次。显式三级（`kg/quality.py`）：high=种子/教材/教案（批量提取优先走教材通道 `--batch × --doc`）、mid=维基/结构挖掘/Wikidata（覆盖面兜底+结构佐证）、low=弱校对语料（保留级别，一律不参与自动裁决）。新语料源按级别声明式接入（yaml `tier` 字段），扩展时下游把关自动生效。

## 数据模型（SQLite）

```
nodes:  id, name, aliases[], definition, facets[],
        status(seed|proposed|approved|rejected), source, embedding, created_at
edges:  id, src, dst, type(is_a|part_of|prerequisite_of|related_to),
        confidence, rationale, source, status, created_at
```

`status` 字段兼作审核队列；`rationale` 记录 LLM 提议该边的理由，供审核参考。

## 流水线

1. **种子导入**：课程大纲 → YAML → 入库（source=seed，直接 approved）。
2. **来源提取（ingest，现阶段自进化的主通道）**：围绕已有锚点节点搜 Wikipedia（中文优先、英文兜底）→ M3 只提取正文明确讨论的概念与关系，每条必须附正文证据引文；算法内部步骤/参数一律降级为 facet → 去重 → proposed。source 记 `wiki:<lang>:<页面名>`，全程可溯源。
3. **扩展 agent（expand，辅助通道）**：选前沿节点 → M3 凭知识提议邻居和边 → 去重（embedding 相似度过阈值的交给 LLM 判同一性，命中合并为 alias）→ 写入 proposed。
4. **审核**：CLI 逐条 approve / reject / merge。
5. **一致性守卫**：先修子图环检测（出环必有错边）、孤儿节点检测、facet 升级提示（facet 被其他节点的边指到时提示）。

## 里程碑

- [x] ① 种子图 + 可视化（109 节点 / 177 边，2026-07-03）
- [x] ② 去重流水线 + 扩展 agent 跑通一轮（M3 提议 3 节点 9 边入 proposed）
- [x] ③ 审核 CLI + 一致性守卫，进入日常"每天进化一点"运转
- [x] ④ Wikipedia 来源提取（ingest）：有据可查的自进化主通道
- [x] ⑤ 语料库化改造（2026-07-03，见 algorithm.md）：corpus 层（定向爬取+revision 快照）→ 结构挖掘（重定向/分类/链接密度）→ 批量 ingest + 缺口驱动选点 → expand 改假设生成器 → 先修推断级标记
- [x] ⑥ 高质量教材语料 + 调研结论落地（2026-07-05，见 algorithm.md §4.1/§6.5/§7）：docs 教材通道（d2l-zh / cs231n / cs229 / sutton-barto；英文源 minimax-m2.7 翻译即快照）→ toc 目录序先修佐证信号 → ingest 分块提取（去掉 7000 字截断）→ 守卫扩展（is_a/part_of 环、先修传递冗余、related_to 反向拦截）→ calibrate 校准命令
- [x] ⑦ 质量硬化 + 语料分级（2026-07-06，见 algorithm.md §4.2/§6.5/§6.7、docs/tech-plan.html）：语料三级 high/mid/low（quality.py，教材批量优先 `--batch × --doc`，low 不参与自动裁决）→ 自动裁决批次化（batch_id + `kg rollback` 整批撤销）→ 邻居重叠守卫（UMAP 撞名补丁）→ 自动批准边前环检测（守卫前移）→ evidence 重引重试 → 去重三元裁决（same/facet/different）
- [x] ⑧ 教学接口增强（2026-07-06，见 algorithm.md §3/§6.5/§6.6/§7.1/§9）：neighborhood 输出附讲解资源（教材章节+维基页，零 LLM，顺序即推荐优先级）→ 误区（Misconception）作 `误区:` 前缀特殊 facet 提取（走 ingest 全部约束，导出时拆独立字段）→ review 节点批准后就地裁决关联边 → burn-in 影子模式（`verify --apply --dry-run` 只记 auto_would 不改状态）+ calibrate 影子 vs 人工一致率表（实验本身随金标裁决积累进行）
- [ ] ⑨ 学习者掌握度层 + 教学 prompt 接口（设计原则：共享图+学生 overlay、状态节点、只追加证据、行为信号不让 LLM 猜；误区届时升级为可挂诊断证据的独立类型）

## 参考

- 增量式构建 + 实体消解流水线：Zep/Graphiti（arXiv:2501.13956）
- LLM 建图综述：arXiv:2510.20345
- 先修关系数据与方法：MOOCCube(X)、LectureBank、arXiv:2507.18479
