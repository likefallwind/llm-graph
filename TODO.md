# TODO — 语料映射修复（2026-07-03 遗留，2026-07-04 已完成）

原任务：全量 crawl 后发现约 7 个节点映射到错误页面，修复前禁跑 mine / ingest。
**除人工 review 外全部完成**，数据库修改前的备份在 `data/kg.db.bak-20260704`。

## 已完成

1. **收紧 `kg/corpus.py` 匹配**（改动已同步进 algorithm.md §4）：
   - 子串命中不再直接满分，要求短串占长串长度比 ≥0.6（`SUBSTRING_MIN_RATIO`）；
   - `MATCH_THRESHOLD` 0.55 → **0.75**（0.7 拦不住「深度Q网络」vs 重定向「深度神经网络」=0.727）；
   - 新增**消歧义页守卫** `_is_disambiguation()`（en:Pool 有精确重定向「Pooling」，相似度拦不住），
     crawl 与 grow 都拒收；
   - 错例 + 正例回归全部通过。
2. **7 个错误映射全部清理并重建**（覆盖率 111/111）：

   | 节点 | 现映射 | 方式 |
   |---|---|---|
   | 目标检测 | en:Object detection | 重爬自动 |
   | 多任务学习 | en:Multi-task learning | 重爬自动 |
   | 图像分类 | en:Computer vision | 重爬经「Image classification」精确重定向回来（维基编辑指向，algorithm 认可） |
   | 门控循环单元 | en:Gated recurrent unit | 手动指定（别名 GRU 精确撞《神偷奶爸》en:Gru，字符串手段无解） |
   | 池化 | en:Pooling layer | 手动指定（zh 无条目；搜索首位是消歧义页 en:Pool） |
   | 预训练与微调 | zh:微调 (深度学习) | 手动指定（仅 1181 字，en:Fine-tuning (deep learning) 5281 字可换） |
   | 深度Q网络 | en:Q-learning | 手动指定（无独立条目；zh:DQN 是日语网络用语；维基把 Deep Q-learning 重定向到此页。留空则每次 crawl 会重新抓错页） |

   无关页面 en:Pool、en:Gru、zh:维奥拉-琼斯目标检测框架、zh:DQN 已连 corpus 行删除。
3. `check` 通过（环/孤儿 OK，4 条 facet 重名提示待 review 时处理）。
4. `mine aliases` +123 别名（已剔除误挂到深度Q网络的「Q learning」，那是 Q学习 的名字）；
   `mine categories` +81 条 part_of 候选边（proposed）。
5. `ingest --batch 3` 已跑（2026-07-04）：锚点 前向传播 / K近邻 / 核方法，
   新提议节点 4（基于实例的学习、惰性学习、核函数、默瑟定理）、新边 14、别名合并 2、facets +25。
   现状：13 proposed 节点、112 proposed 边待审。

## 2026-07-04 新增：校准层（人工审核 → AI 为主的过渡设施）

已实现并验证（详见 algorithm.md §5.1、§6.5、§6.6）：

1. ingest evidence 子串校验（伪证据整条丢弃）；
2. review 裁决留痕 `review_log` + 新操作（节点 d 降级 facet；边 f 翻转 / t 改类型）；
3. `kg verify`：结构佐证（互链 / RefD 先修方向 / Wikidata claim）+ LLM 判断题复核，
   `--apply` 对双重一致的边自动裁决，`kg review --audit N` 抽检；
4. `kg mine wikidata`：QID 关系 → 候选边（31 条），同 QID 节点对告警（10 组）。

## 待人工

```bash
.venv/bin/python -m kg verify --limit 20   # 攒复核信号（可多跑几轮）
.venv/bin/python -m kg verify --no-llm --apply  # 双重一致自动裁决
.venv/bin/python -m kg review              # 人工裁决剩余 + --audit 抽检
```

- 队列：proposed 节点 13 个 + 边百余条（review 界面已带佐证行）。
- **同 QID 告警（mine wikidata 机械产出，比 embedding 硬）**：特征值与特征向量↔特征空间、
  最优化↔凸优化、梯度下降↔随机梯度下降、前向传播↔反向传播（共享来源页，应拆分映射而非合并）、
  权重初始化↔Xavier初始化↔He初始化、残差连接↔ResNet、Transformer↔位置编码、
  语言模型↔大语言模型、计算机视觉↔图像分类、Q学习↔深度Q网络。
- 边界映射留意（不算错）：自注意力→en:Attention Is All You Need、位置编码→en:Transformer、
  GPT→zh:ChatGPT、语言模型→zh:大型语言模型、深度Q网络→en:Q-learning（可否决）。
- 「机器学习 part_of 深度学习」等方向反了的分类边可用 review 的 f 键翻转。
