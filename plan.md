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
   - `related_to`（弱关联）
3. **节点粒度：facet 升级规则**。候选知识点默认作为父节点的 facet（如"温度参数"是 softmax 节点的 facet），只有当它需要自己的边时才升级为独立节点（如加入"知识蒸馏"时，它依赖的是温度参数而非整个 softmax）。粒度是随图长出来的，不预先设计。
4. **不用现成框架**（Graphiti 等），自建轻量 MVP：Python + SQLite + embedding，零框架依赖。
5. **种子来自人工校验过的材料**（d2l.ai 目录、CS229/CS231n 大纲），LLM 只在骨架边缘扩展，不凭空生成拓扑。
6. **LLM 教学结合推迟**，现在只保留一个接口：节点邻域导出为结构化 JSON（定义 + facets + 先修链 + 下游）。
7. **模型**：显式调用 LLM 用 MiniMax M3（`MINIMAX_API_KEY`，reasoning 模型，需给足 max_tokens）；embedding 用 embo-01。

## 数据模型（SQLite）

```
nodes:  id, name, aliases[], definition, facets[],
        status(seed|proposed|approved|rejected), source, embedding, created_at
edges:  id, src, dst, type(is_a|part_of|prerequisite_of|related_to),
        confidence, rationale, source, status, created_at
```

`status` 字段兼作审核队列；`rationale` 记录 LLM 提议该边的理由，供审核参考。

## 四条流水线

1. **种子导入**：课程大纲 → YAML → 入库（source=seed，直接 approved）。
2. **扩展 agent**：选前沿节点（新加入/边稀疏）→ M3 提议邻居和边 → 去重（embedding 相似度过阈值的交给 LLM 判同一性，命中合并为 alias）→ 写入 proposed。
3. **审核**：CLI 逐条 approve / reject / merge。
4. **一致性守卫**：先修子图环检测（出环必有错边）、孤儿节点检测、facet 升级提示（facet 被其他节点的边指到时提示）。

## 里程碑

- [x] ① 种子图 + 可视化（109 节点 / 177 边，2026-07-03）
- [x] ② 去重流水线 + 扩展 agent 跑通一轮（M3 提议 3 节点 9 边入 proposed）
- [x] ③ 审核 CLI + 一致性守卫，进入日常"每天进化一点"运转
- [ ] ④（以后）学习者掌握度层 + 教学 prompt 接口

## 参考

- 增量式构建 + 实体消解流水线：Zep/Graphiti（arXiv:2501.13956）
- LLM 建图综述：arXiv:2510.20345
- 先修关系数据与方法：MOOCCube(X)、LectureBank、arXiv:2507.18479
