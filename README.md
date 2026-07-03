# llm-graph — 自进化 AI 知识图谱 MVP

设计方案见 [plan.md](plan.md)，算法逻辑见 [algorithm.md](algorithm.md)。

## 安装

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
export MINIMAX_API_KEY=...   # MiniMax M3 + embo-01
```

## 首次使用：导入种子图

```bash
python -m kg seed seeds/ai_core.yaml   # 109 节点 / 177 边，自动算 embedding + 跑守卫
```

## 日常进化循环

每次开终端先激活环境（或者直接用 `.venv/bin/python` 代替 `python`）：

```bash
cd ~/code/llm-graph
source .venv/bin/activate
```

然后跑进化循环（详细算法见 algorithm.md §10）：

```bash
# 1. 语料：把生效节点的维基页面抓进本地语料库（增量，抓过的不重抓）
python -m kg corpus crawl
python -m kg corpus grow --limit 10    # 可选：沿内链把被引最多的页面扩进语料

# 2. 结构挖掘（零 LLM、免费）：重定向→别名，分类→候选边
python -m kg mine aliases
python -m kg mine categories

# 3. 提取（主通道）：缺口驱动自动选锚点批量提取，或指定锚点
python -m kg ingest --batch 3
python -m kg ingest 卷积神经网络            # 指定锚点
python -m kg ingest 卷积神经网络 --dry-run  # 只看结果不入库

# 4. 假设生成器（辅助）：LLM 提缺口名字 -> 语料验证 -> 有据提取
python -m kg expand

# 5. 审核：逐条批准/拒绝（a批准 r拒绝 m合并 s跳过 q退出）
python -m kg review

# 6. 守卫 + 看图
python -m kg check
python -m kg viz    # 生成 out/graph.html，浏览器打开
```

注意点：

- `ingest` 的锚点必须是图里已有的节点名（或别名），新知识要挂在骨架上。
- 所有提取产物都带来源 `wiki:语言:页面@版本号`，审核时可溯源；语料在本地，同版本可复现。
- 一次 M3 提取要 1~3 分钟（reasoning 模型思考久），批量时耐心等。
- viz 图里虚线黄色的是 proposed 待审核内容，实线是已生效的。

## 其他命令

```bash
python -m kg stats                     # 节点/边统计（按状态、类型分布）
python -m kg corpus stats              # 语料库统计与节点覆盖率
python -m kg export 神经网络           # 教学接口：节点先修链/邻域结构化 JSON
python -m kg embed                     # 补齐缺失的 embedding
python -m kg viz --approved-only       # 只看已生效子图
```

数据库在 `data/kg.db`（SQLite），可用 `KG_DB` 环境变量覆盖。
