# llm-graph — 自进化 AI 知识图谱 MVP

设计方案见 [plan.md](plan.md)。

## 安装

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
export MINIMAX_API_KEY=...   # MiniMax M3 + embo-01
```

## 常用命令

```bash
python -m kg seed seeds/ai_core.yaml   # 导入种子（自动算 embedding + 跑守卫）
python -m kg stats                     # 节点/边统计
python -m kg viz                       # 生成 out/graph.html 可视化
python -m kg expand --count 1          # 扩展 agent：LLM 提议前沿节点的邻居（proposed 入库）
python -m kg expand --node 迁移学习 --dry-run   # 指定节点、只看提议不入库
python -m kg review                    # 逐条审核 proposed 节点与边
python -m kg check                     # 一致性守卫：先修环 / 孤儿 / facet 重名
python -m kg export 神经网络           # 教学接口：节点邻域结构化 JSON
```

## 日常进化循环

```
expand（LLM 提议）→ review（人工批准）→ check（守卫）→ viz（看图）
```

数据库在 `data/kg.db`（SQLite），可用 `KG_DB` 环境变量覆盖。
