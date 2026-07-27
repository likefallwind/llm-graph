import sqlite3
import unittest
from unittest.mock import patch

from kg import schema, store, validators


def _claim(conn, relation="alternative_to"):
    if relation == "subfield_of":
        subject_type = object_type = "concept"
    else:
        subject_type = object_type = "solution"

    subject = store.add_entity(conn, f"{relation}-subject", subject_type)
    object_ = store.add_entity(conn, f"{relation}-object", object_type)
    return store.add_claim(conn, subject.id, relation, object_.id)


def _accepted_type(relation):
    """取该关系接受的证据类型；强弱现在是按关系判定的。"""
    from kg.ontology import registry
    return registry().relation(relation)["accepted_evidence_types"][0]


def _support(conn, claim, slug, group, *, high=False, evidence_type=None):
    source_id = store.upsert_source(
        conn,
        slug,
        slug,
        "textbook",
        independence_group=group,
        authority_profile={claim.relation: "high"} if high else {},
    )
    snapshot = store.add_source_snapshot(
        conn,
        source_id,
        "v1",
        content=f"{slug} explicitly supports the claim.",
    )
    store.add_evidence(
        conn,
        snapshot.id,
        f"{slug} explicitly supports the claim.",
        evidence_type or _accepted_type(claim.relation),
        claim_id=claim.id,
        mechanically_valid=True,
        entailment="supports",
    )


class ValidatorEvidenceThresholdTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        schema.ensure(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_single_high_authority_source_meets_the_threshold(self):
        """一处语料原文 + 模型蕴含复核 = 门槛要求的证据量。

        门槛曾经要 2 个独立来源组，那等于把模型那一票也折算成一个来源——
        CLAUDE.md 明确写着同一模型的多次判断不是新增独立信源。
        """
        claim = _claim(self.conn)
        _support(self.conn, claim, "book-a", "book:a", high=True)

        result = validators.evaluate(self.conn, claim.id)

        self.assertEqual(result.outcome, "auto_approve")
        self.assertEqual(result.independent_supports, 1)
        self.assertEqual(result.high_authority_supports, 1)

    def test_two_independent_sources_with_high_authority_auto_approve(self):
        claim = _claim(self.conn)
        _support(self.conn, claim, "book-a", "book:a", high=True)
        _support(self.conn, claim, "book-b", "book:b")

        result = validators.evaluate(self.conn, claim.id)

        self.assertEqual(result.outcome, "auto_approve")
        self.assertEqual(result.independent_supports, 2)
        self.assertEqual(result.high_authority_supports, 1)

    def test_same_independence_group_does_not_inflate_the_count(self):
        """原文与译文是同一个来源组，两条摘录仍然只算一个独立来源。"""
        claim = _claim(self.conn)
        _support(self.conn, claim, "book-a-original", "book:a", high=True)
        _support(self.conn, claim, "book-a-translation", "book:a")

        result = validators.evaluate(self.conn, claim.id)

        self.assertEqual(result.independent_supports, 1)

    def test_no_qualifying_support_still_needs_more_evidence(self):
        """门槛降到 1 不等于取消门槛：一条合格的支持证据都没有仍然不够。"""
        claim = _claim(self.conn)

        result = validators.evaluate(self.conn, claim.id)

        self.assertEqual(result.outcome, "needs_more_evidence")
        self.assertEqual(result.independent_supports, 0)

    def test_independent_sources_without_high_authority_need_more_evidence(self):
        claim = _claim(self.conn)
        _support(self.conn, claim, "book-a", "book:a")
        _support(self.conn, claim, "book-b", "book:b")

        result = validators.evaluate(self.conn, claim.id)

        self.assertEqual(result.outcome, "needs_more_evidence")
        self.assertEqual(result.independent_supports, 2)
        self.assertEqual(result.high_authority_supports, 0)

    def test_evidence_type_not_accepted_by_relation_is_not_strong(self):
        # explicit_function 是 used_for 的强证据，对 part_of 恰是被排除的语义。
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        schema.ensure(conn)
        self.addCleanup(conn.close)
        claim = _claim(conn, "part_of")
        _support(conn, claim, "book-a", "book:a", high=True,
                 evidence_type="explicit_function")
        _support(conn, claim, "book-b", "book:b",
                 evidence_type="explicit_function")

        result = validators.evaluate(conn, claim.id)

        self.assertEqual(result.outcome, "needs_more_evidence")
        self.assertEqual(result.independent_supports, 0)
        self.assertTrue(any("不被 part_of 接受" in r for r in result.reasons))

    def _oppose(self, claim, slug, group, evidence_type=None):
        source_id = store.upsert_source(
            self.conn, slug, slug, "textbook", independence_group=group)
        snapshot = store.add_source_snapshot(
            self.conn, source_id, "v1", content=f"{slug} contradicts the claim.")
        store.add_evidence(
            self.conn, snapshot.id, f"{slug} contradicts the claim.",
            evidence_type or _accepted_type(claim.relation),
            claim_id=claim.id, mechanically_valid=True, entailment="contradicts")

    def test_strong_opposing_evidence_goes_to_human_review(self):
        claim = _claim(self.conn)
        _support(self.conn, claim, "book-a", "book:a", high=True)
        self._oppose(claim, "book-b", "book:b")

        result = validators.evaluate(self.conn, claim.id)

        self.assertEqual(result.outcome, "human_review")
        self.assertTrue(any("反对证据" in r for r in result.reasons))

    def test_non_assertive_opposing_evidence_does_not_force_human_review(self):
        # 共现是编排不是断言：它支持不了一个关系，同样也反驳不了。原来只在支持侧
        # 过滤，一条共现的 contradicts 却能把 claim 判去人工，那是不对称的。
        claim = _claim(self.conn)
        _support(self.conn, claim, "book-a", "book:a", high=True)
        _support(self.conn, claim, "book-b", "book:b")
        self._oppose(claim, "book-c", "book:c", evidence_type="cooccurrence")

        result = validators.evaluate(self.conn, claim.id)

        self.assertEqual(result.outcome, "auto_approve")

    def test_non_assertive_evidence_stays_visible_in_reasons(self):
        """目录序反驳不了一个关系，但它出现过这件事要留在理由里。"""
        claim = _claim(self.conn)
        _support(self.conn, claim, "book-a", "book:a", high=True)
        self._oppose(claim, "book-c", "book:c", evidence_type="toc_order")

        result = validators.evaluate(self.conn, claim.id)

        self.assertEqual(result.outcome, "auto_approve")
        self.assertTrue(any("编排而非断言" in r for r in result.reasons))

    def test_assertive_opposing_evidence_counts_even_if_it_cannot_establish(self):
        # explicit_definition 建立不了 part_of，但一句定义确实能反驳 part_of。
        # 白名单管的是「能不能建立」，不管「能不能反驳」。
        claim = _claim(self.conn, "part_of")
        self._oppose(claim, "book-c", "book:c", evidence_type="explicit_definition")

        result = validators.evaluate(self.conn, claim.id)

        self.assertEqual(result.outcome, "human_review")
        self.assertTrue(any("反对证据" in r for r in result.reasons))

    def test_support_that_cannot_establish_is_not_reported_as_missing(self):
        claim = _claim(self.conn, "part_of")
        _support(self.conn, claim, "book-a", "book:a", high=True,
                 evidence_type="explicit_function")

        result = validators.evaluate(self.conn, claim.id)

        self.assertEqual(result.outcome, "needs_more_evidence")
        self.assertIn("现有支持都不足以建立该关系", result.reasons)

    def test_no_reviewed_evidence_is_reported_as_missing(self):
        claim = _claim(self.conn)

        result = validators.evaluate(self.conn, claim.id)

        self.assertEqual(result.outcome, "needs_more_evidence")
        self.assertIn("没有通过蕴含验证的支持证据", result.reasons)

    def test_required_independent_zero_is_honoured_not_skipped(self):
        # 配成 0 时不能静默滑到默认值 2；那是配置被忽略，不是配置生效。
        self.assertEqual(
            validators._required_independent(
                {"minimum_evidence": {"independent_standard_sources": 0}}), 0)
        self.assertEqual(
            validators._required_independent(
                {"minimum_evidence": {"independent_curriculum_sources": 3}}), 3)
        self.assertEqual(validators._required_independent({}), 2)

    def test_high_impact_relations_require_human_review(self):
        for relation in ("is_a", "subfield_of", "part_of", "prerequisite_of"):
            with self.subTest(relation=relation):
                conn = sqlite3.connect(":memory:")
                conn.row_factory = sqlite3.Row
                schema.ensure(conn)
                self.addCleanup(conn.close)
                claim = _claim(conn, relation)
                _support(conn, claim, "book-a", "book:a", high=True)
                _support(conn, claim, "book-b", "book:b")

                result = validators.evaluate(conn, claim.id)

                self.assertEqual(result.outcome, "human_review")
                self.assertIn("高影响关系在校准完成前保留人工审核", result.reasons)


class EntailmentDirectionTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        schema.ensure(self.conn)

    def tearDown(self):
        self.conn.close()

    def _unreviewed_evidence(self, relation):
        claim = _claim(self.conn, relation)
        source_id = store.upsert_source(
            self.conn,
            "direction-source",
            "direction-source",
            "textbook",
            independence_group="book:direction",
            authority_profile={relation: "high"},
        )
        snapshot = store.add_source_snapshot(
            self.conn,
            source_id,
            "v1",
            content="The object is an alternative to the subject.",
        )
        evidence = store.add_evidence(
            self.conn,
            snapshot.id,
            "The object is an alternative to the subject.",
            "explicit_comparison",
            claim_id=claim.id,
            mechanically_valid=True,
        )
        return claim, evidence

    @patch("kg.validators.llm.chat_json")
    def test_symmetric_relation_prompt_ignores_endpoint_order(self, chat_json):
        chat_json.return_value = {"verdict": "supports", "reason": "symmetric"}
        claim, evidence = self._unreviewed_evidence("alternative_to")

        validators.verify_entailment(self.conn, claim.id)

        prompt = chat_json.call_args.args[0][0]["content"]
        self.assertIn("交换 subject/object 不改变含义", prompt)
        self.assertEqual(
            store.evidence_for_claim(self.conn, claim.id)[0].entailment,
            "supports",
        )

    @patch("kg.validators.llm.chat_json")
    def test_directional_relation_prompt_requires_endpoint_order(self, chat_json):
        chat_json.return_value = {"verdict": "supports", "reason": "directional"}
        claim, _ = self._unreviewed_evidence("derived_from")

        validators.verify_entailment(self.conn, claim.id)

        prompt = chat_json.call_args.args[0][0]["content"]
        self.assertIn("必须检查 subject/object 方向", prompt)

    @patch("kg.validators.llm.chat_json")
    def test_part_of_support_requires_explicit_composition_confirmation(
            self, chat_json):
        chat_json.return_value = {
            "verdict": "supports",
            "reason": "参与了构建过程",
        }
        claim, _ = self._unreviewed_evidence("part_of")

        validators.verify_entailment(self.conn, claim.id)

        prompt = chat_json.call_args.args[0][0]["content"]
        self.assertIn("帮助构建", prompt)
        self.assertEqual(
            "insufficient",
            store.evidence_for_claim(self.conn, claim.id)[0].entailment,
        )

    @patch("kg.validators.llm.chat_json")
    def test_part_of_accepts_explicit_composition_confirmation(self, chat_json):
        chat_json.return_value = {
            "verdict": "supports",
            "composition_explicit": True,
            "reason": "正文明确说明是结构组成部分",
        }
        claim, _ = self._unreviewed_evidence("part_of")

        validators.verify_entailment(self.conn, claim.id)

        self.assertEqual(
            "supports",
            store.evidence_for_claim(self.conn, claim.id)[0].entailment,
        )

    @patch("kg.validators.llm.chat_json")
    def test_force_rechecks_reviewed_evidence(self, chat_json):
        chat_json.return_value = {"verdict": "supports", "reason": "corrected"}
        claim, evidence = self._unreviewed_evidence("alternative_to")
        store.update_entailment(
            self.conn,
            evidence.id,
            "contradicts",
            reason="stale direction judgment",
        )

        validators.verify_entailment(self.conn, claim.id, force=True)

        chat_json.assert_called_once()
        updated = store.evidence_for_claim(self.conn, claim.id)[0]
        self.assertEqual(updated.entailment, "supports")
        self.assertEqual(updated.metadata["entailment_reason"], "corrected")

class KnowledgeObjectionTests(unittest.TestCase):
    """领域知识只能否决不能支持。

    拿模型知识去支持，等于把模型记忆当知识写进图，那是核心不变式禁止的；
    拿它去否决，最坏只是多送一条给人看。这个不对称和证据类型白名单同构——
    `is_strong_evidence` 也只作用于支持侧。
    """

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        schema.ensure(self.conn)

    def tearDown(self):
        self.conn.close()

    def _reviewed(self, claim, *, objection=None):
        _support(self.conn, claim, "book-a", "book:a", high=True)
        evidence_id = self.conn.execute(
            "SELECT id FROM evidence WHERE claim_id=?", (claim.id,)).fetchone()["id"]
        payload = {"verdict": "supports", "reason": "正文明确表述"}
        if objection is not None:
            payload["knowledge_objection"] = True
            payload["knowledge_objection_reason"] = objection
        review = store.add_entailment_review(
            self.conn, evidence_id, "supports", reason="正文明确表述",
            raw_output=payload)
        self.conn.execute(
            "UPDATE evidence SET current_entailment_review_id=? WHERE id=?",
            (review.id, evidence_id))
        self.conn.commit()

    def test_without_objection_the_claim_passes(self):
        claim = _claim(self.conn)
        self._reviewed(claim)

        self.assertEqual("auto_approve", validators.evaluate(self.conn, claim.id).outcome)

    def test_objection_sends_the_claim_to_human_review(self):
        claim = _claim(self.conn)
        self._reviewed(claim, objection="方向反了，实际是 object 派生自 subject")

        result = validators.evaluate(self.conn, claim.id)

        self.assertEqual("human_review", result.outcome)
        self.assertTrue(any("领域知识否决" in r for r in result.reasons))
        self.assertTrue(any("方向反了" in r for r in result.reasons))

    def test_objection_never_turns_insufficient_evidence_into_approval(self):
        """否决只能减分。没有合格证据时，模型说什么都不该让它通过。"""
        claim = _claim(self.conn)

        result = validators.evaluate(self.conn, claim.id)

        self.assertEqual("needs_more_evidence", result.outcome)

    def test_malformed_raw_output_is_ignored(self):
        claim = _claim(self.conn)
        _support(self.conn, claim, "book-a", "book:a", high=True)
        evidence_id = self.conn.execute(
            "SELECT id FROM evidence WHERE claim_id=?", (claim.id,)).fetchone()["id"]
        review = store.add_entailment_review(
            self.conn, evidence_id, "supports", reason="x")
        self.conn.execute(
            "UPDATE entailment_reviews SET raw_output='不是JSON' WHERE id=?", (review.id,))
        self.conn.execute(
            "UPDATE evidence SET current_entailment_review_id=? WHERE id=?",
            (review.id, evidence_id))
        self.conn.commit()

        self.assertEqual("auto_approve", validators.evaluate(self.conn, claim.id).outcome)

if __name__ == "__main__":
    unittest.main()
