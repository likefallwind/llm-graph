import sqlite3
import unittest
from unittest.mock import patch

from kg import schema, store, validators


def _claim(conn, relation="alternative_to"):
    if relation == "subfield_of":
        subject_type = object_type = "field"
    else:
        subject_type = object_type = "method"
    subject = store.add_entity(conn, f"{relation}-subject", subject_type)
    object_ = store.add_entity(conn, f"{relation}-object", object_type)
    return store.add_claim(conn, subject.id, relation, object_.id)


def _support(conn, claim, slug, group, *, high=False):
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
        "explicit_comparison",
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

    def test_single_high_authority_source_needs_more_evidence(self):
        claim = _claim(self.conn)
        _support(self.conn, claim, "book-a", "book:a", high=True)

        result = validators.evaluate(self.conn, claim.id)

        self.assertEqual(result.outcome, "needs_more_evidence")
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

    def test_two_sources_in_same_independence_group_need_more_evidence(self):
        claim = _claim(self.conn)
        _support(self.conn, claim, "book-a-original", "book:a", high=True)
        _support(self.conn, claim, "book-a-translation", "book:a")

        result = validators.evaluate(self.conn, claim.id)

        self.assertEqual(result.outcome, "needs_more_evidence")
        self.assertEqual(result.independent_supports, 1)

    def test_independent_sources_without_high_authority_need_more_evidence(self):
        claim = _claim(self.conn)
        _support(self.conn, claim, "book-a", "book:a")
        _support(self.conn, claim, "book-b", "book:b")

        result = validators.evaluate(self.conn, claim.id)

        self.assertEqual(result.outcome, "needs_more_evidence")
        self.assertEqual(result.independent_supports, 2)
        self.assertEqual(result.high_authority_supports, 0)

    def test_high_impact_relations_require_human_review(self):
        for relation in ("is_a", "subfield_of", "prerequisite_of"):
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


if __name__ == "__main__":
    unittest.main()
