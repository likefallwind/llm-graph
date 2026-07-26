# 实体主类型改造（注册表 v5）

**状态：提案，未实施。** 代码和 `config/relation-registry.yaml` 仍是 v4 的十一类
词表。本文件记录改造的结论、判据、未定项和阶段计划，实施完成后内容并入
`design/ontology.md` 和 `design/algorithm.md`，本文件删除。

关系与证据语义见 `design/ontology.md`；每一步算什么见 `design/algorithm.md`；
机器权威是 `config/relation-registry.yaml`。和代码不一致时以代码为准。

---

## 一、结论

实体主类型从十一类收敛到六类，单值、必填：

```
resource | criterion | data | task | solution | concept
```

列名沿用 `entities.entity_type`，`entity_type_assertions` 和类型冲突队列的机制
不变。

六类是**一个维度**，不是两个。它问的是"这个实体在 AI 知识体系里占哪个位置"，
`concept` 是"哪个位置都不占"这一格，不是本体维度的残留。

## 二、判据：优先序，命中即停

```
1. 供人阅读、学习或引用的知识载体                    → resource
2. 用于优化、比较、评分或评测的标准、指标、目标、协议  → criterion
3. 样本、记录、实例或观测值的集合                    → data
4. 需要解决的问题，有目标、输入输出或成功条件         → task
5. 用于解决或支撑任务的算法、过程、模型、架构、系统、工具 → solution
6. 以上都不占                                       → concept
```

三条附加规则：

**优先序只用来让前五格压过 `concept`。** `concept` 是"抽象知识对象"，跟前五格
永远同时成立——损失函数既是标准也是概念，反向传播既是算法也是概念。所以需要
一条规则说"占了槽位就不算 concept"。

**前五格之间不该出现平局。** 真出现了，说明把两个实体混成了一个，该拆而不是
排序。MMLU 是评测基准（criterion），它包含的题目集合是另一个实体（data），两者
用关系连；ImageNet 数据集与以它为基础的评测基准同理。这条取代了早期草案里
"criterion 和 data 谁排前面"的争论——那个问题不存在。

待验：`task` 与 `solution` 之间可能有真平局。「优化」「模型选择」「推断」都像
"既是要做的事又是一套做法"。按本规则它们也该拆（优化问题 / 优化算法），但得看
语料里是不是真分得开。阶段 A 用这三个校验。

**判规范身份，不判局部用途。** 某段正文说 X"用于分类"，不足以把 X 判成
solution。要看 X 的规范定义。

**solution 和 concept 的边界。** solution 是可执行的算法、过程、模型、架构、
系统、工具——有步骤或有结构。concept 是数学对象、运算、性质、定律、现象、量
本身。被某个 solution 采用不足以让它成为 solution。

```
反向传播、自动微分、梯度下降、人工神经网络、单层神经网络   → solution
Softmax 函数、链式法则、Hadamard 积、仿射变换、导数、范数   → concept
```

**没有"泛称"这条轴。** 任何概念都统辖着更具体的东西，「梯度下降」相对
「小批量随机梯度下降」也是泛称。判的时候不问是泛称还是具体，只按优先序问占不占
前五格。所以「损失函数」是 criterion、「数据集」是 data、「模型」「概率模型」
「深度神经网络」「非参数算法」是 solution，跟它们统辖的具体实例落在同一类。

### 六类的定义、正例、反例

注入 prompt 的就是这一节。反例是重点——放"看着像这一类但不是"的那些。

**resource** — 供人阅读、学习或引用的知识载体。

```
正例：论文、教材、课程、规范文档、教程、API 文档
反例：ImageNet（是被消费的数据，不是读物 → data）
      深度学习框架（是工具 → solution）
```

**criterion** — 用于优化、比较、评分或评测的标准、指标、目标、协议。

```
正例：交叉熵损失、平方损失、Hinge 损失、0-1 损失函数、感知器损失、
      损失函数、替代损失函数、准确率、BLEU、困惑度
反例：梯度（数学对象 → concept）
      熵（数学量；只有被当成被优化的目标时才是 criterion → concept）
      交叉熵损失的最小化（是过程 → solution）
```

**data** — 样本、记录、实例或观测值的集合。

```
正例：ImageNet、COCO、训练数据集、验证数据集、数据集、训练数据
反例：样本、特征、标签（是集合里的成分，不是集合 → concept）
      数据预处理、数据操作（是过程 → solution）
```

**task** — 需要解决的问题，有目标、输入输出或成功条件。

```
正例：图像分类、机器翻译、二分类、多类分类、分类问题、预测、结构化学习
反例：分类器、线性分类器（是解法 → solution）
      标签（是任务的产物，不是任务 → concept）
```

**solution** — 用于解决或支撑任务的算法、过程、模型、架构、系统、工具。判据是
**有步骤或有结构**。

```
正例：梯度下降、随机梯度下降、反向传播、自动微分、线性回归、支持向量机、
      人工神经网络、Transformer、PyTorch、深度学习框架、知识蒸馏
反例：Softmax 函数、链式法则、Hadamard 积、仿射变换、导数、范数
      （数学对象、运算、定律 → concept）
      过拟合、泛化（现象与性质 → concept）
```

**concept** — 前五格都不占的抽象知识对象：数学对象、运算、性质、定律、现象、量。

```
正例：过拟合、欠拟合、泛化、熵、概率、梯度、矩阵、向量、张量、范数、
      线性可分、超参数、特征、标签、样本、微积分、线性代数、
      Softmax 函数、链式法则、仿射变换
反例：反向传播（算法 → solution）
      损失函数（标准 → criterion）
      预测（问题 → task）
      数据集（集合 → data）
```

四条消歧从句。前三条防止 `concept ↔ method` 那条老边界从 solution 一侧长回来；
每一条都是首轮双轮判定实测暴露出来的，不是预设的（见第十一节）。

**类别名跟它统辖的那一类落在同一格。** 不因为"是个类别"就退回 concept。
「模型」「概率模型」「深度神经网络」「非参数算法」都是 solution，「损失函数」
「替代损失函数」是 criterion，「数据集」是 data——正如「线性回归」是 solution、
「交叉熵损失」是 criterion、「ImageNet」是 data。**只在成员是可指认的具体东西
时适用**：成员本身就是能力设想或技术范畴的，按下一条判 concept。

**研究领域、学科方向、技术范畴、能力设想判 concept。** 它们不是有成功条件的
问题。人工智能、机器学习、深度学习、自然语言处理、计算机视觉、微积分、线性
代数、人工通用智能、生成式人工智能、深度伪造都是 concept，既不是 task 也不是
solution。旧词表的 `field` 整格并入 concept。

**多义检测优先于类别名跟随。** 一个名字同时指一个问题和一族做法时先判
`ambiguous`，不要用"它是一类方法"把它压成 solution。

**被某个 solution 内部采用的数学构造仍是 concept。** 核函数、激活函数、假设
函数、决策函数、计算图、张量这类数学对象与数据结构都是 concept；把它们组织成的
可执行做法（支持向量机、Softmax 回归、反向传播）才是 solution。solution 判据里
的"有结构"指**有可执行的组成步骤**，不指"它本身是一种结构"。

**同时像 task 和 solution 的，多半是两个东西共用一个名字。** 「优化」既指"求
最优解这个问题"又指"一族算法"，「推断」既指任务又指过程，「回归」既指预测连续
值的问题又指一族建模方法。这种判 `ambiguous` 并写清该拆成哪两个，不要硬选
一边——见第五节第三项。

## 三、为什么不是两个维度

讨论中出现过三版方案：

1. `form(4)` 本体 + `function(5)` 功能，form 为主
2. `kind(2) = abstract|artifact` 本体 + `role(5)` 功能，kind 为主
3. 本版：本体维取消，功能维升为主类型并加 `concept` 兜底

砍掉本体维的理由是实测的（数据见第九节）：

- `abstract : artifact = 112 : 2`。本体维在当前语料里没有区分度，当不了消歧
  判别器、当不了关系闸门、也产生不了质量信号。
- `concept / procedure` 那版本体维的边界本身不稳：十五条类型冲突里有七条卡在
  这条边界上，换个名字（method → procedure）不改变判定难度。

留下功能维的理由是它有消费者：`solves` / `used_for` 的宾语要 task，`trained_on`
的宾语要 data，`evaluated_by` / `optimizes` 的宾语要 criterion。类型系统只表达
有人消费的区别。

**多值 role 被否决。** 现有的冲突检测靠"同一实体两次观察类型不一致"产生
`type_conflict`。改成多值可空之后，一次标 `[task]` 一次标 `[solution]` 会被解释
成信息补充，冲突率归零——但歧义一条没少，只是从可见变成不可见。如果将来确实
需要多值，必须同时定义集合层面的冲突（两次观察的集合不相交记冲突，子集才算
兼容），否则就是用消灭度量冒充消灭问题。

## 四、`is_a` 两端同类型不变式

优先序一致执行的结果是：`is_a` 两端的主类型必然相同。`is_a` 说的就是"同一类
东西的更具体一种"，跨类型的 `is_a` 本身就是可疑信号。

现有二十条 `is_a` 有九条两端类型不一致，全部会在改造后归零：六条是
method/model 之争（合并进 solution 后消失），三条是类别名被标成 concept
（`非参数算法`、`替代损失函数`、`概率模型`，按优先序应为 solution / criterion /
solution）。

这条不变式给主类型第二个消费者：越界的自动进类型复核队列。零 LLM、确定性、
不依赖任何已发布数据。

**它只做检测器和候选生成器，不做自动赋值，也不做关系准入闸。** 拿未发布的
`is_a` 传递闭包去校验其他关系是被明确否决的方案：`is_a` 标了
`high_impact_review: true`，全部 claim 停在 shadow，那样的闸门今天空转，而且把
确定性硬闸换成依赖未校准数据的图遍历。

## 五、待定项

改造开始前必须定下，由阶段 A 的判定结果关闭。

1. **六类各自的定义、正例、反例。** 这是阶段 A 的主要产出，也是六类改造里最
   直接见效的一步——现在 `kg/observations.py:217` 只把十一个光秃秃的类型名拼进
   prompt，注册表里的 `description` 一个字都没传下去，模型判类型完全靠猜。

   每一类要有：一段定义、三到五个正例、三到五个**反例**。反例是重点，放"看着
   像这一类但不是"的那些——concept 的反例里放反向传播，solution 的反例里放
   Softmax 函数和链式法则。整套注进 prompt。

2. **solution 与 concept 边界的措辞。** 第二节给了初稿，用库里的硬骨头校验：
   核函数、假设函数、决策函数、优化、推断、泛化、模型选择。合格标准定成可检验
   的——同一份判据，两轮独立判定这几个词结果一致，且人看了不觉得别扭。不一致的
   就是措辞漏洞，补进反例清单。

   这条不关闭，`concept ↔ method` 那条老边界会原样长回到 `concept ↔ solution`
   上，六类改造白做。

3. **多义名的识别判据。** 处理方式见第六节第一条，那里定的是"怎么做"。还没定
   的是"怎么认出来"——从外面看，多义和判错长得一模一样，都是同名两次观察给了
   不同类型：

   ```
   回归      一次 method 一次 concept   ← 可能真是两个东西（回归问题 / 回归方法）
   反向传播  一次 method 一次 concept   ← 一个东西，其中一次标错了
   ```

   处理方式相反：前者拆，后者 retype。判错方向的代价不对称——把判错当多义，好
   观察卡在 pending；把多义当判错，两个不同的东西被永久合成一个，而主类型不可变
   使它很难被发现。

   判据不能看类型差异本身（对两种情况都成立），要看定义和证据是否指向同一个
   东西。这是一次消歧判断，复用 `kg/entity_resolution.py` 现有的 LLM 消歧路径，
   不新造机制。

## 六、现有约束与已知缺口

改造必须绕开或修复的东西，都已在代码和库里核实。

**多义名的障碍不在 schema，在消歧器的输出选项。**

`entities.normalized_name` 全局唯一（`kg/schema.sql:40`），所以同一个
`canonical_name` 不能有两行。但两个**不同 canonical_name** 的实体可以共享一个
多义 alias，两处唯一键都不冲突：

```
entities.normalized_name  UNIQUE                    → 回归问题 / 回归分析方法，两行
aliases  UNIQUE(entity_id, normalized_name, language) → 「回归」同时挂到两个 entity_id
```

读取侧早就为此留了位置。`kg/store.py:557` 的 `identity_catalog` 明写"一个键可能
仍指向多个实体；调用方只能在结果长度为 1 时确定性认领"，`unique_identity_types`
（`:580`）用 `len(entities) == 1` 显式排除歧义键。

缺的是写入侧：`kg/entity_resolution.py` 的消歧器只能输出 `new` 或 `existing`，
没有第三个分支说"这个名字对应两个东西，用消歧后的名字各建一个，原名做共享
alias"。所以现在第二个义项到来时，resolve 返回 `type_conflict` 并把观察挂到第一
个实体上，只留一条类型断言——**第二个义项被静默吞掉**。

因此本版**不改身份键**，只加消歧器的第三条输出分支和对应写入路径。识别判据见
第五节第三项；判不出来时 observation 停 pending 并留痕，不再静默并入。

**判类型的输入必须是定义，不是名字。**

现在 EXTRACT_PROMPT 让模型在同一个 JSON 里并列输出 `name`、`entity_type`、
`definition`，没有先后。模型完全可以先看名字定类型，再补一句定义来圆它。改成
有序两步——**先按正文写定义，再仅依据定义按优先序判类型**——判据才作用在语义
上，而不是名字的字面。

副产品是类型变得可抽检而不必回语料：看定义和类型对不对得上即可。这是减人工里
杠杆最大的一步。

**v4 词表的 `description` 从来没被任何人读过，而且是坏的。** 它们写成 YAML
流式映射，逗号把值切断了：

```python
>>> registry().entity_types["concept"]
{'description': 'Abstract idea', 'property': None, 'principle': None,
 'object': None, 'or phenomenon.': None}
```

`concept` 的描述只剩前三个词，另外四段变成了值为 null 的假键。这个错误活到今天
没被发现，正因为没有任何代码消费它——CLAUDE.md 说的"死配置会让人以为某条规则在
生效"，这就是一个实例。v5 改成块式标量并由 `Registry.entity_type_contract()`
注入提示词，判据才第一次真正到达模型。

**`entities.definition` 现在不承重。** 字段早就有（`kg/schema.sql:42`），库里
112/114 有值，抽取时写入（`kg/observations.py:145`），消歧
（`kg/entity_resolution.py:199`、`:309`）和复核队列（`kg/review_queues.py:45`）
都会读。问题是它是个软字段：

- `parse_payload` 只做 `.strip()`，无非空要求、无长度要求、不跟 evidence 比对
- 与主类型一样写一次永不更新（`kg/entity_resolution.py:455`），后来更好的定义
  进不来
- 质量参差且不可见。库里实例：「优化：深度学习所围绕的核心主题。」什么也没说；
  「反向传播：自动微分章节（2.5.2）讨论的非标量变量梯度计算方法。」在引章节号；
  「Softmax 函数：将 R^k 上的向量转换为元素非负且和为 1 的概率向量的函数」才是
  能据以判型的写法

定义质量直接决定类型判得准不准，所以阶段 C 要给它加下限校验，`retype` 入口的
范围也要覆盖定义修订——两个字段是一体的，定义改了类型往往要跟着改。

**歧义产生在 `canonical_name`，不在 `normalized_name`。** 后者是机械派生的
（`kg/store.py:23`，NFKC + casefold + 空白折叠），不做任何语义决定。名字定成
「MMLU」就分不出评测基准和题目集合；定成「MMLU 评测基准」「MMLU 题目集合」，
`normalized_name` 自然分开。

`kg/entity_resolution.py` 的 LLM 规范化器现在的职责是"名称规范化与对齐"——把
表面变体归并到规范名。它没有**消歧命名**的职责，即"这个名字太笼统，得起个能
区分的名字"。加上这个职责，与第五节第三项、本节第一条的第三输出分支是同一件事。

**主类型没有任何修改路径。** 全库没有一条 `UPDATE entities SET entity_type`，
`model_queue_reviews.auto_changed` 恒 False，类型复核队列只进不出。十一类时代
尚可忍受，六类之后类型要承担关系闸门和 `is_a` 不变式两项职责，判错一条的代价
更大。**这是自动化的前提条件**——没有事后纠错的出口，人就只能在写入前逐条把关，
自动判定不敢放开。

**`config/ai-coverage-taxonomy.yaml:129-130` 是死配置。**
`required_entity_types` / `recommended_entity_types` 在 `kg/`、`scripts/`、
`tests/` 里没有任何消费者。换词表时删掉或接上消费者，二选一。

**`benchmarks/gold.jsonl` 三条负例带旧类型名**（`model` / `method` / `concept`）。
`gold.schema.json:69` 只要求非空字符串不是 enum，所以不会报错，会静默引用不存在
的类型值。迁移时一起改。

**`subfield_of` 的类型约束会变成空的。** 它现在是 `[field] → [field]`，`field`
取消后只能写成 `[concept] → [concept]`，跟 `is_a` 在签名上再也分不开——而它的
语义（"子领域"）本来就是 `is_a` 的一个特例。它是 `experimental`、不在抽取路径
上，所以没有现实风险，但保留一条靠类型撑着、类型没了就空转的关系是自欺。
建议随本次改造退役，`subfield_of` 的语义并入 `is_a`。这是关系语义变更，要按
CLAUDE.md 的规矩成套改，不在本文件单方面决定——列在这里等拍板。

现有测试其实已经这么用了：`tests/test_relation_contract.py:55` 叫
`test_field_taxonomy_is_absorbed_by_is_a`，断言「分类 is_a 研究领域」成立；
下一条 `:63` 断言 `subfield_of` 在抽取边界被拒。也就是说领域分类走 `is_a` 已经
是既定行为，`subfield_of` 从来没有真正投入使用。

**关系签名重写后部分闸门会变松。** `part_of` 现在是
`[concept, method, model, architecture, loss, system] → [concept, method, model,
architecture, system]`，六类下会塌成大致 `[concept, solution, criterion] →
[concept, solution]`，基本不设防；`prerequisite_of` 同理。功能槽位那几条
（task / data / criterion）反而变强。松紧变化必须用回放量化，不靠感觉。

**functional relations 现在都不在抽取路径上。** `trained_on`、`evaluated_by`、
`solves` 等全是 `experimental`，三处抽取路径都传 `active_only=True`
（`kg/observations.py:169`、`kg/claims.py:216`、`kg/targeting.py:288`）。签名
重写对当前抽取没有影响，只影响将来开闸。

## 七、阶段计划

### 阶段 0：`retype` 入口（独立于词表改造，先做）— 已完成

补的是第六节"主类型没有任何修改路径"那个洞。在十一类词表下就能用，不必等 v5。

```bash
kg pipeline retype --entity 65 --to concept --definition "…" --reason "…"
kg pipeline revert-retype --revision 1
kg pipeline revisions [--entity 65]
```

实现落在新表 `entity_revisions`（schema 版本 12），不是原计划的
`entity_type_assertions`——后者有 `UNIQUE(observation_id)` 且
`status CHECK IN ('consistent','conflict')`，语义绑的是观察，人工修订塞进去会
污染观察史。新表按 `merge_events` 的形状：`payload` 存改前的值，撤销时原样回滚。

- `store.revise_entity` / `revert_revision` / `entity_revisions`
- 理由必填；类型过注册表校验；定义不得改成空；空修订拒绝；已合并的实体不可修订
- 类型和定义在同一次修订里一起改、一起撤销——定义改了类型往往要跟着改
- `revised_by` 默认 `human`。自动路径不得调用它绕过「一切裁决先 Shadow」

配套改了 `review_queues.review_type_conflicts` 的查询：加
`a.observed_type != e.entity_type`。断言是追加的观察史，不因人工改型而改写；
"是否冲突"是相对当前主类型的派生事实，现算。不加这条，retype 之后队列还在报
同一批已解决的冲突，改了等于白改。

`tests/test_entity_revision.py` 十一条覆盖上述规则，含"改型后队列清空但断言行
不变"。

### 阶段 A：判据固化与首轮判定（不改库）

- A1 AI 判全部实体，同一批跑两轮独立判定，输出三档：两轮一致、两轮不一致、
  命中不确定信号
- A2 人只看两类——不一致的那批，加从一致里随机抽二十条。动作是看 AI 给出的
  类型、理由、命中优先序第几档，同意或否决
- A3 人判过的每一条进 `benchmarks/entity_types.jsonl`（实体级，独立于 claim 级
  的 `gold.jsonl`，需另写 schema）。判据以后改动，重跑这个集合测回归
- A4 判据定稿——六类的定义、正例、反例写全，第五节三项待定在此关闭

不确定信号用确定性规则，不用 LLM 自报置信度：

```
两轮判定不一致
判定落到 concept 兜底档（前五格都没命中，最易出错）
is_a 邻居的类型与自己不一致
名称含「函数 / 法则 / 定理 / 变换」等数学对象词却判成 solution
```

**闸门**：判不下来的 ≤ 2 条。超了说明六类边界还不够清楚，不要往下走。

### 阶段 B：词表与关系签名（几乎纯配置）

- B1 `config/relation-registry.yaml` 升到 version 5，`entity_types` 换成六值，
  `description` 写进优先序措辞
- B2 十五条关系的 `subject_types` / `object_types` 全部重写
- B3 临时回放脚本（scratchpad，不进仓库）：现有 claim 和 observation 在新签名下
  过一遍 `validate_claim`，输出"新拒"和"新放行"两张清单
- B4 删掉 `config/ai-coverage-taxonomy.yaml:129-130`

**闸门**：B3 的新拒清单人工过一遍，确认没有误伤。

### 阶段 C：代码（改逻辑，不碰数据）

- C1 `kg/observations.py:217` 现在只把类型名拼进 prompt，注册表的 `description`
  没有传下去。六类完全靠优先序才能确定性判定，必须注入完整判据（定义 + 正例 +
  反例），并把输出改成有序两步：先写定义，再仅依据定义判类型
- C1b `definition` 加下限校验：非空、不得只是章节引用、不得只重复名称本身
- C2 `kg/entity_resolution.py:198`、`:581` 的消歧 prompt 同步
- C3 `kg/review_queues.py:16` 的复核 prompt 同步
- C4 新增 `is_a` 两端同类型检查，越界进类型复核队列。零 LLM。阶段 D 完成前会
  大量报警，属正常
- C5 resolve 遇到 `type_conflict` 时 observation 停 pending 并留痕，不再静默
  并入第一个实体
- C5b LLM 规范化器加第三条输出分支「多义，需消歧命名」：给出两个可区分的
  `canonical_name`，原名注册为共享 alias。判据见第五节第三项
- C6 `tests/test_relation_contract.py` 更新，新增 `is_a` 同类型测试和 retype
  留痕测试

**闸门**：`.venv/bin/python -m unittest discover -s tests -t .` 全绿。

### 阶段 D：数据迁移（改库，需明确许可）

跑的是阶段 A 那套机制的第二次执行——双轮判、分歧送人、抽检，代码复用。

按旧词表能安全一对一映射的只有 `field → concept`、`task → task`、
`loss → criterion`，其余每一桶里都混着别的：`system` 里「专家系统」「深度学习
框架」是 concept 不是 solution；`resource` 那一条是「训练数据」，本来就标错了，
应为 data；`concept` 里藏着模型、数据集、损失函数这些要改判的。所以不做批量
映射，全部走判定机制。

- D1 迁移前 `cp data/kg.db data/archive/kg-pre-v5.db`
- D2 `kg/schema.py` 加迁移函数，登记 `schema_migrations`
- D3 判定结果走阶段 0 的 retype 入口写入，留痕
- D4 一致性检查：`is_a` 越界数、关系签名违规数
- D5 `benchmarks/gold.jsonl` 三条负例的旧类型名一起改

**闸门**：`is_a` 越界归零，或每条越界都有书面解释。

### 阶段 E：重抽与度量

- E1 `algorithm_version` → `grounded-pipeline-4`，`prompt_version` →
  `grounded-extract-4`
- E2 跑 `kg pipeline batch`，全部 source_snapshot 重来
- E3 算新的类型冲突率，跟 16.7% 的基线比

**闸门**：冲突率明显下降。没下降说明判据有问题，回阶段 A，不要靠调 prompt
硬补。

### 阶段 F：文档

`design/ontology.md`（Entity Types 一节整体重写）、`design/algorithm.md`、
`development-plan.md`、`CLAUDE.md`、`benchmarks/README.md`。本文件在此删除。

### 可回滚性

阶段 0 到 C 是配置和代码，git 可回。阶段 D 有库备份且 retype 留痕。阶段 E 是
追加，旧 run 记录不删。不可逆点在 D3——判歪了靠备份整体回退，不逐条修。

## 八、人工投入的定位

三件事必须分开：

- **定判据**——一次性，不随图规模增长，必须人来
- **应用判据**——必须全自动，图多大都一样
- **防止 AI 一致地错**——抽检，固定条数

人的工作量是常数，不是比例。图长到五千个实体，每轮仍然是分歧几十条加抽检
二十条。

**双轮判定不是两个独立信源。** CLAUDE.md 写着同一模型的多次调用是审计证据，
不是新增独立信源。两轮只用来决定这条送不送人看，一致不写进任何证据链，也不
提高任何置信度。

由此带来这套机制唯一的真风险：**M3 会稳定地错在同一个地方，两轮一致地错。**
抽检专门防这个，不能省。抽检发现的每一条错必须回流成
`benchmarks/entity_types.jsonl` 的条目。

## 九、这一版明确不做

```
is_artifact（连 metadata 都不放，代码里一处不出现）
statement 类型
evaluated_on / measured_by 拆关系
身份键改造
experimental 关系转正
Phase 7 旧核心迁移
```

`statement` 暂缓的理由是没有消费者——没有 `has_assumption` / `proved_by` 这类
关系，也没有结构化保存条件、结论、证明状态，加了只增加分类负担。定理、假设先
按优先序判（多数落 concept），通过 `is_a` 细分。将来加证明类关系时再加
`statement` 是个加法，不用重做。

`is_artifact` 暂缓的理由是样本量：库里明确的正例只有两个，BERT 家族 / 具体
checkpoint / 模型规格之间的边界没有任何校准数据。区分它们首先该靠规范名称、
定义和实体消歧，不是一个布尔位。

## 十、实测基线

数据取自 `data/kg.db` 在 2026-07-26 的副本，共 114 个实体、44 条 claim、
262 条 observation、19 个 source_snapshot，全部停在 shadow，从未发布。

```
类型冲突率        15 / 90 有类型断言的实体 = 16.7%
is_a 跨类型       9 / 20
abstract:artifact 112 : 2（真 artifact 只有 ImageNet、Eureqa）
旧类型分布        concept 53、method 27、model 8、loss 6、task 6、field 5、
                  dataset 3、system 3、architecture 2、resource 1
```

十五条冲突实体：

```
线性回归(method←model)      人工神经网络(method←concept)  回归(method←concept)
自动微分(method←concept)     反向传播(method←concept)      Softmax 函数(concept←method)
Softmax 回归(method←model)   单层神经网络(model←architecture)
预测(task←concept)           分类问题(task←concept)        二分类(task←concept)
多类分类(task←concept)       损失函数(loss←concept)
线性代数(concept←field)      微积分(concept←field)
```

## 十一、阶段 A 首轮判定（2026-07-26）

114 个实体，MiniMax-M3，双轮，每批 12 条，第二轮按固定种子打乱分批，所以两轮的
批内邻居不同。

```
两轮一致        109 / 114 = 95.6%
判定分布        concept 46、solution 41、criterion 7、task 6、data 5、
                ambiguous 3、resource 1
```

**首版不确定信号里"落到 concept 兜底"是废的**——114 条命中 46 条。concept 是
最大的一格，拿它当筛子等于不筛。改成看**改判是否超出十一类到六类的显然映射**：
落在映射表内的是词表合并的机械后果，风险低；落在表外的才是判据真正做出的重新
分类，价值和风险都在那里。需人看的条数因此从 75 降到 41（命中信号 21 + 抽检
20）。

首轮暴露了判据的三处缺口，第二节的四条消歧从句就是据此补的：

- **类别名归属没写进去。** 「非参数算法」「概率模型」两轮分歧，一轮认为"算法
  类别"是抽象分类归 concept，一轮认为归 solution。设计文档第二节本来就有
  "没有泛称这条轴"，是注入 prompt 时漏了。
- **领域没有归宿。** `field` 取消后「自然语言处理」两轮分成 concept 和 task。
  判据完全没说研究方向该落哪格。
- **"有结构"被读成"是一种结构"。** 「计算图」一轮判 solution（"是有结构的
  工具"），一轮判 concept（"是 solution 内部采用的数学构造"）。原措辞没排除
  数据结构本身。

**多义识别按设计工作。** 三条 `ambiguous` 是「回归」「优化」「推断」，正是第五节
第三项预判的那三个，模型给出了拆分建议（回归任务／回归方法、优化问题／优化
算法、推断任务／推断过程）而不是硬选一边。

### 第二轮：补上三处缺口后

```
两轮一致  108 / 114 = 94.7%
```

三处缺口全部修复：非参数算法 → solution，自然语言处理 → concept，
计算图 → concept，概率模型 → solution，`field` 五条整齐并入 concept。

**但一致率没升反降，因为新规则有两处越界，方向相反。**

「类别名跟随类别」被过度套用到没有具体成员的词上：人工通用智能、通用人工智能、
生成式人工智能、深度伪造四条新出现分歧，一轮的理由都是"是一类 AI 系统，按类别名
跟随判 solution"。这条规则本来是给「模型」「损失函数」「数据集」用的——它们的
成员是可指认的具体东西；AGI 的"成员"是一种能力设想。

同一条规则还压掉了多义检测：「回归」从 `ambiguous` 变成 `solution`，理由是
"它是一类建模方法"。这恰好是多义识别最该抓住的那个词。

于是补两条边界：类别名跟随只在成员可指认时适用；多义检测优先于类别名跟随。
这两条都是我新写的规则自身的作用域和优先级 bug，跟第一轮那三处缺口同类，不是
需要人拍板的判断题。

**批处理会让实体静默消失。** 「概率」在第二轮的输出里整条缺失，模型没有为它
产出结果。已经落进"缺一轮结果"信号进复核队列，但值得记下来：按批判定时输出
条数不等于输入条数，必须逐条核对而不能假定对齐。

### 第三轮：判据收敛

```
两轮一致  113 / 114 = 99.1%
判定分布  concept 49、solution 42、criterion 7、task 6、data 5、
          ambiguous 3、resource 1
需人看    42 = 命中信号 22 + 抽检 20
直接采信  92
```

第二轮的五条分歧全部消解：人工通用智能、生成式人工智能、通用人工智能、深度伪造
判 concept，概率判 concept，回归回到 `ambiguous`。

**只剩一条两轮不一致，而且它本身是个判据问题而不是模型问题。**「预测」的定义写
的是"在查询点 x 处计算假设输出 h(x) 的过程"，一轮据此判 `ambiguous`（理由是它与
「推断＝推断任务／推断过程」同构），另一轮判 `task`（理由是判据的 task 正例里
直接列了"预测"）。两个理由都对——是我把「预测」写进 task 正例，压住了本该触发的
多义检测。「预测」和「推断」是同一种任务／过程二义，要么两个都拆，要么两个都不
拆，不能一个进正例一个判多义。这条留给人拍板。

**三轮判据迭代的性质**：第一轮三处是设计文档里已有、注入提示词时漏掉的规则；
第二轮两处是我新写规则自身的作用域与优先级 bug（类别名跟随被套用到没有具体成员
的词上；类别名跟随压掉了多义检测）。到第三轮，剩下的分歧是真正需要人判断的
取舍，机械迭代到此为止。

判据每改一次都全量重跑，没有只补跑分歧项——判据变了，此前的一致也不再算数。

复现（只读，先复制库避免 `db.connect()` 写入）：

```bash
cp data/kg.db /tmp/kg-copy.db
sqlite3 /tmp/kg-copy.db "
  SELECT e.canonical_name, e.entity_type, group_concat(DISTINCT a.observed_type)
  FROM entity_type_assertions a JOIN entities e ON e.id = a.entity_id
  GROUP BY a.entity_id;"
```
