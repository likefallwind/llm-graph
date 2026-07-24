import sqlite3
import unittest
from unittest.mock import patch

from kg import claims, entity_resolution, schema, store
from kg.observations import (
    ClaimObservation,
    EntityObservation,
    ObservationBatch,
)


def observation(name: str, entity_type: str = "concept") -> EntityObservation:
    return EntityObservation(
        name=name,
        entity_type=entity_type,
        definition=f"{name} 的定义",
        aliases=(),
        evidence=f"教材明确讨论了{name}。",
        location="§1",
        raw={},
    )


class EntityResolutionTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        schema.ensure(self.conn)

    def tearDown(self):
        self.conn.close()

    def add_snapshot(self, slug: str, group: str):
        source_id = store.upsert_source(
            self.conn, slug, slug, "textbook",
            independence_group=group)
        return store.add_source_snapshot(
            self.conn, source_id, "v1", content=f"{slug} content")

    def test_canonical_exact_has_priority_over_alias(self):
        canonical = store.add_entity(self.conn, "机器学习", "field")
        other = store.add_entity(self.conn, "机器学习方法", "method")
        with self.assertRaisesRegex(ValueError, "规范名冲突"):
            store.add_alias(
                self.conn, other.id, "机器学习", status="verified")

        result = entity_resolution.resolve(
            self.conn, observation(" 机器学习 ", "field"))

        self.assertEqual(canonical.id, result.entity_id)
        self.assertEqual("canonical_exact", result.matched_by)

    def test_verified_alias_is_reused_without_llm(self):
        entity = store.add_entity(self.conn, "监督学习", "method")
        store.add_alias(
            self.conn, entity.id, "Supervised Learning",
            language="en", status="verified")

        def fail_if_called(_observation, _candidates):
            self.fail("verified alias 命中时不应调用 LLM")

        result = entity_resolution.resolve(
            self.conn, observation("supervised learning", "method"),
            llm_normalizer=fail_if_called)

        self.assertEqual(entity.id, result.entity_id)
        self.assertEqual("verified_alias_exact", result.matched_by)

    def test_proposed_alias_does_not_auto_match(self):
        entity = store.add_entity(self.conn, "监督学习", "method")
        store.add_alias(
            self.conn, entity.id, "Supervised Learning",
            language="en", status="proposed")

        result = entity_resolution.resolve(
            self.conn, observation("Supervised Learning", "method"),
            llm_normalizer=lambda _observation, _candidates: {
                "decision": "ambiguous",
                "canonical_name": "",
                "confidence": 0.4,
                "reason": "证据不足",
            })

        self.assertIsNone(result.entity_id)
        self.assertEqual("llm_ambiguous", result.matched_by)

    def test_exact_name_type_conflict_reuses_entity_and_records_assertion(self):
        entity = store.add_entity(self.conn, "预测", "task")

        result = entity_resolution.resolve(
            self.conn, observation("预测", "concept"),
            source_snapshot_id=None, observation_id=None)

        self.assertEqual(entity.id, result.entity_id)
        self.assertEqual("type_conflict", result.outcome)
        assertion = self.conn.execute(
            "SELECT * FROM entity_type_assertions WHERE entity_id=?",
            (entity.id,)).fetchone()
        self.assertEqual("conflict", assertion["status"])
        self.assertEqual("concept", assertion["observed_type"])

    def test_llm_canonicalization_can_reuse_existing_entity(self):
        entity = store.add_entity(self.conn, "监督学习", "method")
        snapshot = self.add_snapshot("book-a", "book:a")

        result = entity_resolution.resolve(
            self.conn, observation("Supervised Learning", "method"),
            source_snapshot_id=snapshot.id,
            llm_normalizer=lambda _observation, _candidates: {
                "decision": "existing",
                "candidate_id": None,
                "canonical_name": "监督学习",
                "proposed_alias": "Supervised Learning",
                "match_type": "translation_alias",
                "confidence": 0.98,
                "reason": "中英文同义术语",
            })

        self.assertEqual(entity.id, result.entity_id)
        self.assertEqual("llm_translation_alias", result.matched_by)
        alias = self.conn.execute(
            "SELECT status FROM aliases WHERE entity_id=? AND normalized_name=?",
            (entity.id, store.normalize_name("Supervised Learning"))).fetchone()
        self.assertEqual("verified", alias["status"])
        self.assertEqual(
            [entity], store.find_verified_alias_entities(
                self.conn, "Supervised Learning"))
        candidate = self.conn.execute(
            "SELECT * FROM entity_alignment_candidates").fetchone()
        self.assertEqual("suspected_same_entity", candidate["relation"])
        self.assertEqual(1, candidate["independent_sources"])

    def test_high_confidence_translation_is_immediately_verified(self):
        entity = store.add_entity(self.conn, "二分类", "task")
        snapshot = self.add_snapshot("book-a", "book:a")

        result = entity_resolution.resolve(
            self.conn, observation("Binary classification", "task"),
            source_snapshot_id=snapshot.id,
            llm_normalizer=lambda _observation, _candidates: {
                "decision": "existing",
                "candidate_id": entity.id,
                "canonical_name": "二分类",
                "match_type": "translation_alias",
                "confidence": 0.98,
                "reason": "英文术语的直接中文翻译",
            })

        self.assertEqual(entity.id, result.entity_id)
        self.assertEqual("llm_translation_alias", result.matched_by)
        alias = self.conn.execute(
            "SELECT status,alias_type FROM aliases WHERE entity_id=?",
            (entity.id,)).fetchone()
        self.assertEqual("verified", alias["status"])
        self.assertEqual("translation_alias", alias["alias_type"])
        candidate = self.conn.execute(
            "SELECT status FROM entity_alignment_candidates").fetchone()
        self.assertEqual("verified", candidate["status"])

    def test_high_confidence_abbreviation_is_not_immediately_verified(self):
        entity = store.add_entity(self.conn, "交叉熵损失", "loss")
        snapshot = self.add_snapshot("book-a", "book:a")

        result = entity_resolution.resolve(
            self.conn, observation("CE", "loss"),
            source_snapshot_id=snapshot.id,
            llm_normalizer=lambda _observation, _candidates: {
                "decision": "existing",
                "candidate_id": entity.id,
                "canonical_name": "交叉熵损失",
                "match_type": "abbreviation",
                "confidence": 0.99,
                "reason": "可能的缩写",
            })

        self.assertIsNone(result.entity_id)
        self.assertEqual("suspected_same_entity", result.outcome)
        alias = self.conn.execute(
            "SELECT status,alias_type FROM aliases WHERE entity_id=?",
            (entity.id,)).fetchone()
        self.assertEqual("proposed", alias["status"])
        self.assertEqual("abbreviation", alias["alias_type"])

    def test_semantic_prefix_cannot_masquerade_as_name_variant(self):
        entity = store.add_entity(self.conn, "交叉熵损失", "loss")
        snapshot = self.add_snapshot("book-a", "book:a")

        result = entity_resolution.resolve(
            self.conn, observation("softmax-交叉熵损失", "loss"),
            source_snapshot_id=snapshot.id,
            llm_normalizer=lambda _observation, _candidates: {
                "decision": "existing",
                "candidate_id": entity.id,
                "canonical_name": "交叉熵损失",
                "match_type": "name_variant",
                "confidence": 0.99,
                "reason": "模型误认为只是名称变化",
            })

        self.assertIsNone(result.entity_id)
        self.assertEqual("suspected_same_entity", result.outcome)
        alias = self.conn.execute(
            "SELECT status FROM aliases WHERE entity_id=?",
            (entity.id,)).fetchone()
        self.assertEqual("proposed", alias["status"])
        candidate = self.conn.execute(
            "SELECT status FROM entity_alignment_candidates").fetchone()
        self.assertEqual("suspected", candidate["status"])

    def test_chinese_suffix_is_validated_as_name_variant(self):
        entity = store.add_entity(self.conn, "分类问题", "task")
        snapshot = self.add_snapshot("book-a", "book:a")

        result = entity_resolution.resolve(
            self.conn, observation("分类", "task"),
            source_snapshot_id=snapshot.id,
            llm_normalizer=lambda _observation, _candidates: {
                "decision": "existing",
                "candidate_id": entity.id,
                "canonical_name": "分类问题",
                "match_type": "name_variant",
                "confidence": 0.98,
                "reason": "只省略问题后缀",
            })

        self.assertEqual(entity.id, result.entity_id)
        alias = self.conn.execute(
            "SELECT status,alias_type FROM aliases WHERE entity_id=?",
            (entity.id,)).fetchone()
        self.assertEqual("verified", alias["status"])
        self.assertEqual("name_variant", alias["alias_type"])

    def test_low_confidence_llm_does_not_merge_or_create(self):
        result = entity_resolution.resolve(
            self.conn, observation("全新术语"),
            llm_normalizer=lambda _observation, _candidates: {
                "decision": "new",
                "canonical_name": "全新术语",
                "confidence": 0.8,
                "reason": "可能是新概念",
            })

        self.assertIsNone(result.entity_id)
        self.assertIsNone(store.find_canonical_entity(self.conn, "全新术语"))

    def test_llm_ambiguous_does_not_merge_despite_existing_canonical_name(self):
        store.add_entity(self.conn, "监督学习", "method")

        result = entity_resolution.resolve(
            self.conn, observation("Supervised Learning", "method"),
            llm_normalizer=lambda _observation, _candidates: {
                "decision": "ambiguous",
                "canonical_name": "监督学习",
                "confidence": 0.99,
                "reason": "上下文不足",
            })

        self.assertIsNone(result.entity_id)
        self.assertEqual("llm_ambiguous", result.matched_by)

    def test_ambiguous_verified_alias_can_be_disambiguated_by_llm(self):
        first = store.add_entity(self.conn, "梯度方法", "method")
        second = store.add_entity(self.conn, "统计梯度", "concept")
        store.add_alias(self.conn, first.id, "SG", status="verified")
        store.add_alias(self.conn, second.id, "SG", status="verified")

        result = entity_resolution.resolve(
            self.conn, observation("SG", "method"),
            llm_normalizer=lambda _observation, candidates: {
                "decision": "existing",
                "candidate_id": first.id,
                "canonical_name": "梯度方法",
                "confidence": 0.99,
                "reason": f"在 {len(candidates)} 个同名候选中结合上下文选择",
            })

        self.assertIsNone(result.entity_id)
        self.assertEqual("suspected_same_entity", result.outcome)
        self.assertEqual(first.id, result.selected_candidate_id)

    def test_high_confidence_llm_new_creates_canonical_and_proposed_alias(self):
        result = entity_resolution.resolve(
            self.conn, observation("RLHF", "method"),
            llm_normalizer=lambda _observation, _candidates: {
                "decision": "new",
                "canonical_name": "基于人类反馈的强化学习",
                "proposed_alias": "RLHF",
                "confidence": 0.99,
                "reason": "规范为中文全称",
            })

        entity = store.get_entity(self.conn, result.entity_id)
        self.assertEqual("基于人类反馈的强化学习", entity.canonical_name)
        alias = self.conn.execute(
            "SELECT status FROM aliases WHERE entity_id=?",
            (entity.id,)).fetchone()
        self.assertEqual("proposed", alias["status"])

    def test_medium_confidence_existing_match_is_saved_as_suspected(self):
        entity = store.add_entity(self.conn, "分类问题", "task")
        snapshot = self.add_snapshot("book-a", "book:a")

        result = entity_resolution.resolve(
            self.conn, observation("分类", "concept"),
            source_snapshot_id=snapshot.id,
            llm_normalizer=lambda _observation, _candidates: {
                "decision": "existing",
                "candidate_id": entity.id,
                "canonical_name": "分类问题",
                "confidence": 0.82,
                "reason": "定义一致但类型观察有冲突",
            })

        self.assertIsNone(result.entity_id)
        self.assertEqual("suspected_same_entity", result.outcome)
        self.assertEqual(entity.id, result.selected_candidate_id)
        candidate = self.conn.execute(
            "SELECT * FROM entity_alignment_candidates").fetchone()
        self.assertEqual("suspected", candidate["status"])
        self.assertAlmostEqual(0.82, candidate["score"])

    def test_two_independent_suspicions_promote_verified_alias(self):
        entity = store.add_entity(self.conn, "分类问题", "task")
        first = self.add_snapshot("book-a", "book:a")
        second = self.add_snapshot("book-b", "book:b")

        def same_entity(_observation, _candidates):
            return {
                "decision": "existing",
                "candidate_id": entity.id,
                "canonical_name": "分类问题",
                "confidence": 0.82,
                "reason": "定义一致",
            }

        first_result = entity_resolution.resolve(
            self.conn, observation("分类", "task"),
            source_snapshot_id=first.id, llm_normalizer=same_entity)
        second_result = entity_resolution.resolve(
            self.conn, observation("分类", "task"),
            source_snapshot_id=second.id, llm_normalizer=same_entity)

        self.assertEqual("suspected_same_entity", first_result.outcome)
        self.assertEqual(entity.id, second_result.entity_id)
        self.assertEqual("accumulated_alignment", second_result.matched_by)
        candidate = self.conn.execute(
            "SELECT * FROM entity_alignment_candidates").fetchone()
        self.assertEqual("verified", candidate["status"])
        self.assertEqual(2, candidate["independent_sources"])
        self.assertGreater(candidate["score"], 0.95)
        alias = self.conn.execute(
            "SELECT status FROM aliases WHERE entity_id=?"
            " AND normalized_name=?",
            (entity.id, store.normalize_name("分类"))).fetchone()
        self.assertEqual("verified", alias["status"])

    def test_same_source_group_does_not_promote_suspected_alias(self):
        entity = store.add_entity(self.conn, "分类问题", "task")
        first = self.add_snapshot("edition-a", "book:shared")
        second = self.add_snapshot("edition-b", "book:shared")
        response = lambda _observation, _candidates: {
            "decision": "existing",
            "candidate_id": entity.id,
            "canonical_name": "分类问题",
            "confidence": 0.90,
            "reason": "定义一致",
        }

        entity_resolution.resolve(
            self.conn, observation("分类", "task"),
            source_snapshot_id=first.id, llm_normalizer=response)
        result = entity_resolution.resolve(
            self.conn, observation("分类", "task"),
            source_snapshot_id=second.id, llm_normalizer=response)

        self.assertIsNone(result.entity_id)
        candidate = self.conn.execute(
            "SELECT * FROM entity_alignment_candidates").fetchone()
        self.assertEqual("suspected", candidate["status"])
        self.assertEqual(1, candidate["independent_sources"])

    def test_alias_identical_to_canonical_is_not_stored(self):
        entity = store.add_entity(self.conn, "牛顿法", "method")

        alias_id = store.add_alias(
            self.conn, entity.id, " 牛顿法 ", status="proposed")

        self.assertIsNone(alias_id)
        count = self.conn.execute(
            "SELECT COUNT(*) FROM aliases WHERE entity_id=?",
            (entity.id,)).fetchone()[0]
        self.assertEqual(0, count)


class ExistingSchemaMigrationTests(unittest.TestCase):
    def test_existing_aliases_are_migrated_as_verified(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at REAL NOT NULL
            );
            CREATE TABLE entities (
                id INTEGER PRIMARY KEY,
                canonical_name TEXT NOT NULL,
                normalized_name TEXT NOT NULL UNIQUE,
                entity_type TEXT NOT NULL,
                definition TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'proposed',
                embedding TEXT,
                metadata TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE aliases (
                id INTEGER PRIMARY KEY,
                entity_id INTEGER NOT NULL REFERENCES entities(id),
                name TEXT NOT NULL,
                normalized_name TEXT NOT NULL,
                language TEXT NOT NULL DEFAULT '',
                alias_type TEXT NOT NULL DEFAULT 'alias',
                source_snapshot_id INTEGER,
                created_at REAL NOT NULL,
                UNIQUE(entity_id, normalized_name, language)
            );
            INSERT INTO entities VALUES
                (1,'监督学习','监督学习','method','','proposed',NULL,'{}',0,0);
            INSERT INTO aliases VALUES
                (1,1,'Supervised Learning','supervised learning','en','alias',NULL,0);
        """)

        schema.ensure(conn)

        row = conn.execute("SELECT status FROM aliases WHERE id=1").fetchone()
        self.assertEqual("verified", row["status"])
        migration = conn.execute(
            "SELECT name FROM schema_migrations WHERE version=5").fetchone()
        self.assertEqual("auditable_entity_resolution", migration["name"])
        conn.close()


class SuspectedMaterializationTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        schema.ensure(self.conn)
        source_id = store.upsert_source(
            self.conn, "book-a", "book-a", "textbook",
            independence_group="book:a")
        self.snapshot = store.add_source_snapshot(
            self.conn, source_id, "v1", content="分类材料")
        self.run_id = store.create_run(
            self.conn, "test", "test-version")
        store.add_entity(self.conn, "分类问题", "task")
        store.add_entity(self.conn, "二分类", "task")

    def tearDown(self):
        self.conn.close()

    def test_suspected_entity_and_dependent_claim_remain_pending(self):
        batch = ObservationBatch(
            entities=(
                observation("分类", "task"),
                observation("二分类", "task"),
            ),
            claims=(
                ClaimObservation(
                    subject="二分类",
                    relation="is_a",
                    object="分类",
                    qualifiers={},
                    evidence_type="explicit_taxonomy",
                    evidence="二分类属于分类。",
                    location="§1",
                    raw={},
                ),
            ),
            next_reading_targets=(),
            rejected=(),
        )

        with patch(
                "kg.entity_resolution._llm_normalize",
                return_value={
                    "decision": "existing",
                    "candidate_id": 1,
                    "canonical_name": "分类问题",
                    "confidence": 0.82,
                    "reason": "定义一致",
                }):
            result = claims.materialize(
                self.conn, batch, source_snapshot_id=self.snapshot.id,
                run_id=self.run_id)

        self.assertEqual((), result.claim_ids)
        statuses = {
            row["subject_text"]: row["status"]
            for row in self.conn.execute(
                "SELECT subject_text,status FROM observations"
                " WHERE run_id=?", (self.run_id,))
        }
        self.assertEqual("pending", statuses["分类"])
        claim_status = self.conn.execute(
            "SELECT status FROM observations WHERE run_id=? AND relation='is_a'",
            (self.run_id,)).fetchone()["status"]
        self.assertEqual("pending", claim_status)


class ProposedAliasReviewTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        schema.ensure(self.conn)
        source_id = store.upsert_source(
            self.conn, "book-a", "book-a", "textbook",
            independence_group="book:a")
        self.snapshot = store.add_source_snapshot(
            self.conn, source_id, "v1", content="术语材料")
        self.entity = store.add_entity(self.conn, "梯度上升", "method")
        store.add_alias(
            self.conn, self.entity.id, "Gradient ascent",
            source_snapshot_id=self.snapshot.id, status="proposed")

    def tearDown(self):
        self.conn.close()

    def test_translation_review_verifies_proposed_alias(self):
        with patch(
                "kg.entity_resolution._llm_normalize",
                return_value={
                    "decision": "existing",
                    "candidate_id": self.entity.id,
                    "canonical_name": "梯度上升",
                    "match_type": "translation_alias",
                    "confidence": 0.99,
                    "reason": "直接翻译",
                }):
            results = entity_resolution.review_proposed_aliases(
                self.conn, limit=10)

        self.assertEqual("verified", results[0]["status"])
        alias = self.conn.execute(
            "SELECT status,alias_type FROM aliases").fetchone()
        self.assertEqual("verified", alias["status"])
        self.assertEqual("translation_alias", alias["alias_type"])

    def test_single_llm_failure_does_not_abort_alias_review_batch(self):
        other = store.add_entity(self.conn, "牛顿法", "method")
        store.add_alias(
            self.conn, other.id, "Newton's method",
            source_snapshot_id=self.snapshot.id, status="proposed")

        def classify(observation, _candidates):
            if observation.name == "Gradient ascent":
                raise ValueError("bad JSON")
            return {
                "decision": "existing",
                "candidate_id": other.id,
                "canonical_name": "牛顿法",
                "match_type": "translation_alias",
                "confidence": 0.99,
                "reason": "直接翻译",
            }

        with patch("kg.entity_resolution._llm_normalize", side_effect=classify):
            results = entity_resolution.review_proposed_aliases(
                self.conn, limit=10)

        by_alias = {item["alias"]: item for item in results}
        self.assertEqual("proposed", by_alias["Gradient ascent"]["status"])
        self.assertEqual("verified", by_alias["Newton's method"]["status"])


if __name__ == "__main__":
    unittest.main()
