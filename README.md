# llm-graph

语料驱动的 AI 知识图谱。LLM 负责阅读、抽取、实体消歧、关系判断和复核，
但 LLM 输出本身不是知识：所有 Entity 和 Claim 必须绑定可定位的来源快照与 Evidence。

设计与开发约束：

- [development-plan.md](development-plan.md)
- [design/ontology.md](design/ontology.md)
- [design/evidence-policy.md](design/evidence-policy.md)
- [config/relation-registry.yaml](config/relation-registry.yaml)
- [config/ai-coverage-taxonomy.yaml](config/ai-coverage-taxonomy.yaml)

## 安装

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
export MINIMAX_API_KEY=...
```

数据库缺省为 `data/kg.db`。开发或试跑时使用 `KG_DB` 指向副本：

```bash
KG_DB=/tmp/kg-test.db .venv/bin/python -m kg pipeline status
```

## 当前主流程

### 1. 查看状态

```bash
.venv/bin/python -m kg pipeline status
```

### 2. 预览历史数据迁移

```bash
.venv/bin/python -m kg pipeline migrate
```

正式迁移前先备份：

```bash
cp data/kg.db data/kg.db.bak
.venv/bin/python -m kg pipeline migrate --apply
```

迁移具有幂等性：

- 旧节点转为 proposed Entity；
- 旧边转为 proposed Claim；
- 来源和理由转为 Evidence；
- 无法安全判断的旧关系进入 `migration_issues`；
- 不会因为旧状态直接发布新 Claim。

### 3. 自动读取本地语料批次

```bash
.venv/bin/python -m kg pipeline batch \
  --topic ai \
  --docs 1 \
  --wiki-pages 1
```

Pipeline 会：

```text
Source Snapshot
  -> LLM Grounded Observation
  -> Evidence 机械校验
  -> Entity 消歧
  -> Claim 聚合
  -> LLM Evidence 蕴含复核
  -> 关系专用验证
  -> Shadow Decision
```

每个来源快照只由同一算法和 coverage topic 处理一次。扩大批次前先看状态和
Shadow 结果。

### 4. 日常入口

```bash
./evolve.sh --fast
./evolve.sh --migrate --topic ai --docs 1 --wiki 1
```

`--fast` 不调用 LLM，只显示迁移预览和 Pipeline 状态。

常用参数：

```text
--topic ID
--docs N
--wiki N
--max-entities N  # 每个文本块的实体上限；整章会跨块去重合并
--max-claims N    # 每个文本块的 Claim 上限；整章会跨块去重合并
--fetch
--migrate
--concurrency N
```

所有新决策当前保持 Shadow，不会自动发布。

## 指定语料

### 教材章节

```bash
.venv/bin/python -m kg pipeline doc \
  --book cs229 \
  --sec 1 \
  --topic supervised_learning
```

### 本地 Wikipedia 页面

```bash
.venv/bin/python -m kg pipeline wiki \
  --lang zh \
  --title 人工智能 \
  --topic ai
```

页面必须已经存在于本地 `corpus` 表。

### 任意 UTF-8 本地文本

```bash
.venv/bin/python -m kg pipeline read \
  --file /path/to/source.txt \
  --source source-slug \
  --source-name "Source Name" \
  --source-type textbook \
  --independence-group book-source \
  --authority '{"is_a":"high","prerequisite_of":"high"}' \
  --topic supervised_learning \
  --language zh
```

缺省由 LLM 抽取。也可以通过 `--observations observations.json` 导入已有结构化
Observation；Evidence 仍必须能在原文中机械定位。

`--no-verify-llm` 会跳过 LLM 蕴含复核，Claim 的 Shadow 结果应停留在
`needs_more_evidence`，适合无网络管线测试。

## 语料采集

教材：

```bash
.venv/bin/python -m kg docs add sources/d2l-zh.yaml
.venv/bin/python -m kg docs fetch d2l-zh
.venv/bin/python -m kg docs translate cs229 --limit 1
```

Wikipedia：

```bash
.venv/bin/python -m kg corpus crawl
.venv/bin/python -m kg corpus grow --limit 10
```

采集层暂时继续复用现有 `docs` 和 `corpus` 实现。

## 兼容命令

以下命令仍操作旧 `nodes/edges`，仅用于迁移期间对照，不属于当前主流程：

```text
seed
ingest
expand
mine
verify
review
rollback
check
calibrate
viz
stats
export
embed
```

当前 `viz` 和 `review` 尚未切换到 Entity/Claim/Evidence 模型。查看新核心请使用：

```bash
.venv/bin/python -m kg pipeline status
```

## 安全原则

- 不允许没有 Source Snapshot 的正式知识。
- Evidence 必须能在快照中机械定位。
- 同一 Claim 可以积累多个支持、反对和不确定 Evidence。
- 同一模型的多次调用不算独立来源。
- 缺证据是 `needs_more_evidence`，不是自动拒绝。
- 自动规则先运行 Shadow，达到关系级校准门槛后才能启用。
