import sqlite3
import unittest
from unittest.mock import patch

from kg import entity_resolution, schema, store, targeting


DOC_SCHEMA = """
CREATE TABLE IF NOT EXISTS doc_sections (
    id INTEGER PRIMARY KEY, book TEXT, sec_id TEXT, ord INTEGER,
    title TEXT, text TEXT DEFAULT '', content_hash TEXT DEFAULT '',
    orig_lang TEXT DEFAULT 'zh', url TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS corpus (
    id INTEGER PRIMARY KEY, lang TEXT, title TEXT, page_id INTEGER,
    revision_id INTEGER, text TEXT DEFAULT '');
"""


def _setup(conn):
    schema.ensure(conn)
    conn.executescript(DOC_SCHEMA)
    conn.commit()


def _pair(conn):
    subject = store.add_entity(conn, "线性回归", "solution")
    object_ = store.add_entity(conn, "回归", "task")
    claim = store.add_claim(conn, subject.id, "is_a", object_.id)
    return subject, object_, claim


def _section(conn, book, sec_id, text):
    conn.execute(
        "INSERT INTO doc_sections(book,sec_id,ord,title,text,content_hash)"
        " VALUES (?,?,1,?,?,?)", (book, sec_id, f"{book} {sec_id}", text, "hash1"))
    conn.commit()


class PassageSearchTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        _setup(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_finds_passage_where_both_endpoints_appear(self):
        _, _, claim = _pair(self.conn)
        _section(self.conn, "cs229", "1", "线性回归是一种回归方法。")

        hits = targeting.find_passages(self.conn, claim.id)

        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].ref, "doc:cs229:1")
        self.assertEqual(hits[0].independence_group, "book:cs229")

    def test_ignores_passage_missing_one_endpoint(self):
        _, _, claim = _pair(self.conn)
        _section(self.conn, "cs229", "1", "线性回归很常用，但这里不提另一个概念。")

        self.assertEqual(targeting.find_passages(self.conn, claim.id), [])

    def test_respects_distance_window(self):
        _, _, claim = _pair(self.conn)
        _section(self.conn, "cs229", "1", "线性回归" + "填充" * 500 + "回归")

        self.assertEqual(targeting.find_passages(self.conn, claim.id, window=50), [])
        self.assertEqual(len(targeting.find_passages(self.conn, claim.id, window=2000)), 1)

    def test_skips_groups_that_already_support_the_claim(self):
        subject, object_, claim = _pair(self.conn)
        _section(self.conn, "cs229", "1", "线性回归是一种回归方法。")
        source_id = store.upsert_source(
            self.conn, "doc-cs229", "cs229", "textbook",
            independence_group="book:cs229")
        snapshot = store.add_source_snapshot(
            self.conn, source_id, "v1", content="线性回归是一种回归方法。")
        store.add_evidence(
            self.conn, snapshot.id, "线性回归是一种回归方法。", "explicit_taxonomy",
            claim_id=claim.id, mechanically_valid=True)

        self.assertEqual(targeting.find_passages(self.conn, claim.id), [])

    def test_prompt_text_is_scoped_to_the_cooccurrence(self):
        _, _, claim = _pair(self.conn)
        noise = "无关内容。" * 2000
        _section(self.conn, "cs229", "1", noise + "线性回归是一种回归方法。" + noise)

        hit = targeting.find_passages(self.conn, claim.id)[0]

        # 送模型的文本必须远小于整节，否则共现窗口只是筛选，管不住模型看哪里。
        self.assertIn("线性回归是一种回归方法。", hit.text)
        self.assertLess(len(hit.text), 1000)
        self.assertEqual(len(hit.section_text), len(noise) * 2 + 12)

    def test_window_is_verbatim_slice_of_the_section(self):
        _, _, claim = _pair(self.conn)
        text = "前文。" * 200 + "线性回归是一种回归方法。" + "后文。" * 200
        _section(self.conn, "cs229", "1", text)

        hit = targeting.find_passages(self.conn, claim.id)[0]

        self.assertIn(hit.text, hit.section_text)
        self.assertEqual(
            hit.section_text[hit.offset:hit.offset + len(hit.text)], hit.text)

    def test_each_cooccurrence_is_its_own_candidate(self):
        _, _, claim = _pair(self.conn)
        filler = "无关内容。" * 300
        _section(self.conn, "cs229", "1",
                 "线性回归是一种回归方法。" + filler + "回归里最简单的是线性回归。")

        hits = targeting.find_passages(self.conn, claim.id)

        # 只取最近的一处会漏掉同一节里别处更明确的陈述。
        self.assertEqual(len(hits), 2)
        self.assertIn("线性回归是一种回归方法。", hits[0].text + hits[1].text)
        self.assertIn("回归里最简单的是线性回归。", hits[0].text + hits[1].text)

    def test_overlapping_cooccurrences_are_not_sent_twice(self):
        _, _, claim = _pair(self.conn)
        _section(self.conn, "cs229", "1",
                 "线性回归是一种回归方法，线性回归也叫回归分析。")

        hits = targeting.find_passages(self.conn, claim.id)

        self.assertEqual(len(hits), 1)

    def test_window_does_not_start_mid_sentence(self):
        _, _, claim = _pair(self.conn)
        _section(self.conn, "cs229", "1",
                 "开头。" * 200 + "这句讲线性回归也讲回归。" + "结尾。" * 200)

        hit = targeting.find_passages(self.conn, claim.id)[0]

        self.assertTrue(hit.text.startswith("开头。"))

    def test_window_keeps_surrounding_context_not_just_the_hit(self):
        _, _, claim = _pair(self.conn)
        _section(self.conn, "cs229", "1",
                 "前一段讲的是别的内容。\n"
                 "线性回归是一种回归方法。它假设目标是输入的线性函数。\n"
                 "后一段接着讲参数估计。")

        hit = targeting.find_passages(self.conn, claim.id)[0]

        # 孤立一句看不出是定义还是举例，判断关系需要上下文。
        self.assertIn("前一段讲的是别的内容。", hit.text)
        self.assertIn("后一段接着讲参数估计。", hit.text)

    def test_window_starts_at_a_paragraph_boundary(self):
        _, _, claim = _pair(self.conn)
        # 命中点前 300 字落在「铺垫」这一段里，段首在 SNAP_LIMIT 之内，
        # 就该对齐到段首，而不是从段落中间某个句号后面开始。
        _section(self.conn, "cs229", "1",
                 "无关段。" * 20 + "\n" + "铺垫。" * 100
                 + "线性回归是一种回归方法。" + "收尾。" * 50)

        hit = targeting.find_passages(self.conn, claim.id)[0]

        self.assertTrue(hit.text.startswith("铺垫。"))
        self.assertNotIn("无关段", hit.text)

    def test_alias_widens_the_search(self):
        subject, _, claim = _pair(self.conn)
        store.add_alias(self.conn, subject.id, "linear regression", status="verified")
        _section(self.conn, "cs229", "1", "linear regression 属于回归。")

        hits = targeting.find_passages(self.conn, claim.id)

        self.assertEqual(len(hits), 1)


class NeutralExtractionTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        _setup(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_prompt_does_not_reveal_the_expected_relation(self):
        subject, object_, _ = _pair(self.conn)
        prompt = targeting.TARGET_PROMPT.format(
            left=subject.canonical_name, right=object_.canonical_name,
            relations="{}", evidence_types="explicit_taxonomy", passage="正文")

        # 提示里不能出现「is_a」这个待验证结论，否则就是诱导性提问。
        self.assertNotIn("is_a", prompt)
        self.assertIn("没有明确陈述就返回", prompt)
        self.assertIn("方向由你根据正文判断", prompt)

    def test_rejects_answer_whose_evidence_is_not_verbatim(self):
        subject, object_, _ = _pair(self.conn)
        payload = {
            "relation": "is_a", "subject": "线性回归", "object": "回归",
            "evidence": "这句话不在正文里", "evidence_type": "explicit_taxonomy"}

        self.assertIsNone(targeting._extract_one(
            payload, "线性回归是一种回归方法。", subject, object_))

    def test_rejects_answer_about_other_entities(self):
        subject, object_, _ = _pair(self.conn)
        payload = {
            "relation": "is_a", "subject": "支持向量机", "object": "回归",
            "evidence": "线性回归是一种回归方法。"}

        self.assertIsNone(targeting._extract_one(
            payload, "线性回归是一种回归方法。", subject, object_))

    def test_rejects_none_relation(self):
        subject, object_, _ = _pair(self.conn)

        self.assertIsNone(targeting._extract_one(
            {"relation": "none"}, "无关正文", subject, object_))

    def test_accepts_reversed_direction_from_the_model(self):
        subject, object_, _ = _pair(self.conn)
        payload = {
            "relation": "is_a", "subject": "回归", "object": "线性回归",
            "evidence": "线性回归是一种回归方法。",
            "evidence_type": "explicit_taxonomy"}

        parsed = targeting._extract_one(
            payload, "线性回归是一种回归方法。", subject, object_)

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["subject_id"], object_.id)
        self.assertEqual(parsed["object_id"], subject.id)

    def test_rejects_experimental_relation(self):
        left = store.add_entity(self.conn, "批量梯度下降", "solution")
        right = store.add_entity(self.conn, "随机梯度下降", "solution")
        payload = {
            "relation": "alternative_to", "subject": "批量梯度下降",
            "object": "随机梯度下降", "evidence": "两者是不同的做法。"}

        self.assertIsNone(targeting._extract_one(
            payload, "两者是不同的做法。", left, right))


class ProbeLogTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        _setup(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_probed_passage_is_not_offered_again(self):
        _, _, claim = _pair(self.conn)
        _section(self.conn, "cs229", "1", "线性回归是一种回归方法。")
        passage = targeting.find_passages(self.conn, claim.id)[0]

        targeting.record_probe(
            self.conn, claim.id, passage, result="no_relation", reason="未陈述")

        self.assertEqual(targeting.find_passages(self.conn, claim.id), [])

    def test_changed_text_is_offered_again(self):
        _, _, claim = _pair(self.conn)
        _section(self.conn, "cs229", "1", "线性回归是一种回归方法。")
        passage = targeting.find_passages(self.conn, claim.id)[0]
        targeting.record_probe(
            self.conn, claim.id, passage, result="no_relation")

        self.conn.execute(
            "UPDATE doc_sections SET text=? WHERE book='cs229'",
            ("线性回归属于回归分析的一种。",))
        self.conn.commit()

        self.assertEqual(len(targeting.find_passages(self.conn, claim.id)), 1)

    def test_skip_probed_can_be_turned_off(self):
        _, _, claim = _pair(self.conn)
        _section(self.conn, "cs229", "1", "线性回归是一种回归方法。")
        passage = targeting.find_passages(self.conn, claim.id)[0]
        targeting.record_probe(self.conn, claim.id, passage, result="no_relation")

        hits = targeting.find_passages(self.conn, claim.id, skip_probed=False)

        self.assertEqual(len(hits), 1)


class RunSelectionTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        _setup(self.conn)

    def tearDown(self):
        self.conn.close()

    @patch("kg.targeting.llm.pmap")
    def test_limit_counts_only_claims_that_have_passages(self, pmap):
        pmap.return_value = []
        # 两条没有候选段落的 claim，id 排在有段落的那条前面。
        for name in ("甲", "乙"):
            left = store.add_entity(self.conn, f"{name}左", "solution")
            right = store.add_entity(self.conn, f"{name}右", "task")
            claim = store.add_claim(self.conn, left.id, "is_a", right.id)
            store.decide(self.conn, "claim", claim.id, "needs_more_evidence",
                         decided_by="shadow")
        subject, object_, claim = _pair(self.conn)
        store.decide(self.conn, "claim", claim.id, "needs_more_evidence",
                     decided_by="shadow")
        _section(self.conn, "cs229", "1", "线性回归是一种回归方法。")

        targeting.run(self.conn, limit=1, verify_llm=False)

        # 额度应该落在唯一有段落的那条上，而不是被前两条空转用光。
        prompts = pmap.call_args.args[1]
        self.assertEqual(len(prompts), 1)
        self.assertEqual(prompts[0][3].ref, "doc:cs229:1")


class DuplicateScanTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        schema.ensure(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_reports_similar_names(self):
        store.add_entity(self.conn, "梯度下降", "solution")
        store.add_entity(self.conn, "梯度下降法", "solution")

        pairs = entity_resolution.find_duplicate_candidates(self.conn)

        self.assertTrue(any(
            {item["left"], item["right"]} == {"梯度下降", "梯度下降法"}
            for item in pairs))

    def test_low_confidence_creations_are_ranked_first(self):
        store.add_entity(self.conn, "感知机", "solution")
        store.add_entity(self.conn, "感知器", "solution",
                         metadata={"below_auto_link_confidence": True})
        store.add_entity(self.conn, "向量", "concept")
        store.add_entity(self.conn, "向量空间", "concept")

        pairs = entity_resolution.find_duplicate_candidates(self.conn)

        self.assertTrue(pairs[0]["low_confidence_creation"])

    def test_unrelated_entities_are_not_reported(self):
        store.add_entity(self.conn, "线性回归", "solution")
        store.add_entity(self.conn, "卷积神经网络", "solution")

        self.assertEqual(entity_resolution.find_duplicate_candidates(self.conn), [])


if __name__ == "__main__":
    unittest.main()
