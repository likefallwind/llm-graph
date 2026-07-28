"""名称同一性的三档判据。

判据分三档，每档能证明的东西不一样，测试也按这三档写：

1. **纯形式变化**——大小写、全半角、空格、标点、修饰性「的」。字符没有增减
   语义成分，确定性可判同名。
2. **类别后缀包含**——一个名字就是另一个加上「问题/方法/算法/模型……」。剥掉
   后缀不改变主类型时可走快路，但快路只是**确认** LLM 的提议，不自己发起。
3. **同词根异中心词**——「机器学习方法」与「机器学习算法」剥到共同词根
   「机器学习」，但谁也不是谁的前缀。这类**绝不能**进确定性快路：共同词根只
   说明它们谈论同一个领域，不说明它们是同一个知识对象。

第三档是这个文件存在的理由。原先的实现取的是词根**集合的交集**，两个名字各剥
各的、剥到中间碰头就算同名，于是整个第三档静默地漏进了快路。这里把它钉死，
而不是等库里先出现一条错边。
"""
import unittest

from kg.entity_resolution import (
    _is_sibling_head_variant,
    _needs_separation_review,
    _validated_direct_match_type,
)


def direct(alias, canonical, claimed="name_variant", observed="", existing=""):
    return _validated_direct_match_type(
        alias, canonical, claimed, observed_type=observed, entity_type=existing)


class FormOnlyVariantTests(unittest.TestCase):
    """第一档：没有增减任何语义成分，确定性判同名。"""

    def test_spacing_case_and_width_are_the_same_name(self):
        for alias, canonical in (
                ("Logistic回归", "Logistic 回归"),
                ("softmax函数", "Softmax 函数"),
                ("Ｌogistic 回归", "logistic回归"),
                ("0-1损失", "0-1 损失")):
            with self.subTest(alias=alias):
                self.assertEqual("name_variant", direct(
                    alias, canonical, observed="concept", existing="concept"))

    def test_attributive_de_is_droppable(self):
        """「的」是修饰连接成分，删掉它不改变所指。"""
        for alias, canonical in (
                ("通用人工智能的生存风险", "通用人工智能生存风险"),
                ("机器学习的方法", "机器学习方法")):
            with self.subTest(alias=alias):
                self.assertEqual("name_variant", direct(
                    alias, canonical, observed="solution", existing="solution"))

    def test_de_elision_does_not_bypass_the_type_gate(self):
        """「机器学习的方法」不是「机器学习」，拦住它的是类型闸。

        删「的」之后「机器学习方法」对「机器学习」是标准的第二档包含关系，形状上
        与「反向传播算法／反向传播」一模一样。区别是语义的：领域是 concept，那一
        类做法是 solution。所以这里由类型闸负责，第一档不需要也不应该自己再拦一次
        ——抽取契约（observations.EXTRACT_PROMPT 第 2b 条）在源头也要求不许截短。
        """
        self.assertIsNone(direct(
            "机器学习的方法", "机器学习",
            observed="solution", existing="concept"))


class SuffixContainmentTests(unittest.TestCase):
    """第二档：一个名字是另一个加类别后缀，且剥掉后缀不改主类型。"""

    def test_containment_keeps_the_fast_path(self):
        for alias, canonical in (
                ("反向传播算法", "反向传播"),
                ("梯度下降算法", "梯度下降"),
                ("交叉熵损失函数", "交叉熵损失"),
                ("感知器损失函数", "感知器损失")):
            with self.subTest(alias=alias):
                self.assertEqual("name_variant", direct(
                    alias, canonical, observed="solution", existing="solution"))

    def test_type_changing_suffix_still_loses_the_fast_path(self):
        self.assertIsNone(direct(
            "回归问题", "回归", observed="task", existing="solution"))


class SiblingHeadWordTests(unittest.TestCase):
    """第三档：共同词根 + 不同类别中心词，绝不进确定性快路。

    这里不断言这些名称对最终一定不同——「平方误差函数」和「平方损失」多半确实
    是一回事。断言的是**判定它们同一必须经过语境判断**，不能靠字符串规则盖章。
    """

    SIBLINGS = (
        ("机器学习方法", "机器学习算法"),
        ("机器学习算法", "机器学习模型"),
        ("分类模型", "分类算法"),
        ("生成模型", "生成方法"),
        ("线性分类模型", "线性分类器"),
        ("softmax运算", "softmax函数"),
        ("分类问题", "分类任务"),
    )

    def test_siblings_never_take_the_fast_path(self):
        for alias, canonical in self.SIBLINGS:
            with self.subTest(alias=alias, canonical=canonical):
                self.assertIsNone(direct(
                    alias, canonical, observed="solution", existing="solution"))

    def test_siblings_are_flagged_as_high_risk(self):
        """认出来才谈得上送反向复核，所以标记本身也要测。"""
        for alias, canonical in self.SIBLINGS:
            with self.subTest(alias=alias, canonical=canonical):
                self.assertTrue(_is_sibling_head_variant(alias, canonical))

    def test_plain_containment_is_not_high_risk(self):
        """第二档不该被误判成第三档，否则每条都要多花一次 LLM。"""
        for alias, canonical in (
                ("反向传播算法", "反向传播"),
                ("Logistic回归", "Logistic 回归")):
            with self.subTest(alias=alias):
                self.assertFalse(_is_sibling_head_variant(alias, canonical))

    def test_unrelated_names_are_not_high_risk(self):
        self.assertFalse(_is_sibling_head_variant("梯度下降", "牛顿法"))


class SeparationReviewTriggerTests(unittest.TestCase):
    """哪些名称对不许一轮提问就落地。

    除了同词根异中心词，还有一类形状不同但风险相同的：一个名字是另一个加了**非
    类别**的修饰成分。类别后缀（算法／方法）剥掉是安全的，限定词（softmax、欧盟、
    线性）剥掉往往正好剥掉了区别本身。
    """

    def test_semantic_modifiers_need_review(self):
        for alias, canonical in (
                ("softmax-交叉熵损失", "交叉熵损失"),
                ("欧盟人工智能法案", "人工智能法案"),
                ("随机梯度下降", "梯度下降")):
            with self.subTest(alias=alias):
                self.assertTrue(_needs_separation_review(alias, canonical))

    def test_sibling_head_words_need_review(self):
        self.assertTrue(_needs_separation_review("机器学习方法", "机器学习算法"))

    def test_deterministic_pairs_skip_review(self):
        """确定性规则已经确认过的不必再花一次 LLM，M3 一次要 1~3 分钟。"""
        for alias, canonical in (
                ("反向传播算法", "反向传播"),
                ("Logistic回归", "Logistic 回归"),
                ("通用人工智能的生存风险", "通用人工智能生存风险")):
            with self.subTest(alias=alias):
                self.assertFalse(_needs_separation_review(alias, canonical))

    def test_unrelated_names_skip_review(self):
        """字符上不沾边的名称对没有这类风险，正向一轮足够。"""
        for alias, canonical in (("CE", "交叉熵损失"), ("多分类", "多类分类")):
            with self.subTest(alias=alias):
                self.assertFalse(_needs_separation_review(alias, canonical))


if __name__ == "__main__":
    unittest.main()
