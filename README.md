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

# 1b. 教材语料（教学性关系的富矿；英文源自动翻译成中文入库）
python -m kg docs add sources/d2l-zh.yaml   # 首次登记源（sources/ 下已有 4 个）
python -m kg docs fetch d2l-zh --limit 20   # 增量抓章节
python -m kg docs translate cs231n --limit 3  # 可选：预翻译英文源（不跑则用到时才翻）

# 2. 结构挖掘（零 LLM、免费）：重定向→别名，分类→候选边，Wikidata 关系→候选边
python -m kg mine aliases
python -m kg mine categories
python -m kg mine wikidata

# 3. 提取（主通道）：缺口驱动自动选锚点批量提取，或指定锚点
python -m kg ingest --batch 3
python -m kg ingest 卷积神经网络            # 指定锚点
python -m kg ingest 卷积神经网络 --dry-run  # 只看结果不入库
python -m kg ingest 线性回归 --doc d2l-zh   # 教材通道：从教材章节提取

# 4. 假设生成器（辅助）：LLM 提缺口名字 -> 语料验证 -> 有据提取
python -m kg expand

# 5. 复核：结构佐证（零 LLM，含教材目录序信号）+ LLM 判断题复核；--apply 自动裁决
python -m kg verify --limit 20 --apply

# 6. 人工审核剩余队列：按信号分层排序（冲突项在前），逐条裁决
python -m kg review
python -m kg review --audit 5   # 抽检自动放行的条目（跑过 --apply 后定期做）

# 7. 守卫 + 校准 + 看图
python -m kg check
python -m kg calibrate   # 各通道×佐证组合的裁决 precision，指导自动裁决松紧
python -m kg viz    # 生成 out/graph.html，浏览器打开
```

注意点：

- `ingest` 的锚点必须是图里已有的节点名（或别名），新知识要挂在骨架上。
- 所有提取产物都带来源 `wiki:语言:页面@版本号` 或 `doc:教材:章节@内容hash`，审核时可溯源；语料在本地，同版本可复现。
- 英文教材翻译入库即快照（evidence 对中文译文校验）；PDF 源需要 `pdftotext`（`sudo apt install poppler-utils`）。
- 一次 M3 提取要 1~3 分钟（reasoning 模型思考久），批量时耐心等。
- viz 图里虚线黄色的是 proposed 待审核内容，实线是已生效的。
- `verify --apply` 的自动裁决很保守：边要 LLM 复核与结构佐证双重一致；节点只自动批准
  （LLM 判「独立概念」且名字精确命中语料页），拒绝/降级 facet 一律留人工。
  自动放行后用 `review --audit N` 抽检兜底。

## 全部命令

```bash
# 图谱数据
python -m kg seed seeds/ai_core.yaml   # 导入种子 YAML（--no-embed 跳过 embedding）
python -m kg stats                     # 节点/边统计（按状态、类型分布）+ 裁决日志汇总
python -m kg check                     # 一致性守卫：先修/is_a/part_of 环 / 先修传递冗余 / 正反向同型边 / 孤儿 / facet 重名
python -m kg viz                       # 生成 out/graph.html（--out 路径，--approved-only 只看生效子图）
python -m kg export 神经网络           # 教学接口：节点先修链/邻域结构化 JSON
python -m kg embed                     # 补齐缺失的 embedding

# 语料库（维基）
python -m kg corpus crawl              # 抓生效节点的维基页面（增量；--limit 限页数）
python -m kg corpus grow --limit 10    # 沿内链把被引最多的页面扩进语料
python -m kg corpus stats              # 语料库统计与节点覆盖率

# 语料库（教材，sources/*.yaml 声明式源：d2l-zh / cs231n / cs229 / sutton-barto）
python -m kg docs add sources/<book>.yaml   # 登记源（章节元数据入库）
python -m kg docs fetch <book>              # 抓章节正文（--limit 限节数，--sec 指定章节强制重抓）
python -m kg docs translate <book>          # 翻译英文源为中文（--limit / --sec；minimax-m2.7）
python -m kg docs stats                     # 各书抓取/翻译进度

# 结构挖掘（零 LLM）
python -m kg mine aliases              # 重定向 → 别名
python -m kg mine categories           # 分类 → part_of 候选边
python -m kg mine wikidata             # Wikidata QID 关系 → 候选边 + 同 QID 疑似同概念仲裁

# 提取与扩展（LLM）
python -m kg ingest --batch 3          # 缺口驱动自动选锚点批量提取（主通道）
python -m kg ingest <锚点名>           # 指定锚点（--limit 每锚点概念数上限，--dry-run 不入库）
python -m kg ingest <锚点名> --doc [BOOK]  # 教材通道（缺省在所有书里找锚点章节）
python -m kg expand                    # 假设生成器（--node 指定节点，--count 前沿节点数，
                                       #   --limit 每节点提议数上限，--dry-run 只验证不提取）

# 复核与审核
python -m kg verify --limit 20         # 结构佐证 + LLM 判断题复核（--no-llm 只跑结构佐证，
                                       #   --redo 重跑已有结论，--apply 双重一致自动裁决）
python -m kg review                    # 人工审核队列：节点 a批准/r拒绝/m合并/d降级facet/s跳过/q退出，
                                       #   边 a/r/f翻转方向/t改类型/s/q
python -m kg review --audit 5          # 抽检 N 条自动放行的条目
python -m kg calibrate                 # 裁决 precision（通道×类型×佐证组合）+ 抽检推翻率
```

数据库在 `data/kg.db`（SQLite），可用 `KG_DB` 环境变量指向测试库以免污染真实数据。
