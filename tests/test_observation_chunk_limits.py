import unittest
from unittest.mock import patch

from kg.observations import extract, split_text


class SplitTextTests(unittest.TestCase):
    def test_never_cuts_inside_a_paragraph(self):
        paragraphs = [f"第{i}段。" + "内容。" * 200 for i in range(20)]
        chunks = split_text("\n\n".join(paragraphs), limit=1000)

        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            for piece in chunk.split("\n\n"):
                self.assertIn(piece, paragraphs)

    def test_straddling_paragraph_goes_to_the_longer_side(self):
        # 第二段跨过上限：应该断在它后面（取长），而不是前面留一个短块。
        chunks = split_text("甲。" * 300 + "\n\n" + "乙。" * 300, limit=800)

        self.assertEqual(1, len(chunks))
        self.assertIn("乙。", chunks[0])

    def test_oversized_paragraph_is_not_hard_cut(self):
        chunks = split_text("句子内容。" * 500, limit=1000)

        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertTrue(chunk.endswith("。"))
        self.assertEqual("句子内容。" * 500, "".join(chunks))

    def test_short_text_stays_one_chunk(self):
        self.assertEqual(["一段话。"], split_text("一段话。", limit=1000))

    def test_no_content_is_lost(self):
        text = "\n\n".join(f"段{i}。" + "字" * 300 for i in range(15))
        chunks = split_text(text, limit=1000)

        self.assertEqual(
            text.replace("\n\n", ""), "".join(chunks).replace("\n\n", ""))


def payload(name: str, evidence: str) -> dict:
    return {
        "entities": [{
            "name": name,
            "entity_type": "concept",
            "definition": f"{name}是本用例构造的一个抽象概念",
            "aliases": [],
            "evidence": evidence,
            "location": "test",
        }],
        "claims": [],
    }


class ObservationChunkLimitTests(unittest.TestCase):
    @patch("kg.observations.split_text", return_value=["第一块证据", "第二块证据"])
    @patch("kg.observations.llm.pmap", side_effect=lambda fn, items: [fn(item) for item in items])
    @patch("kg.observations.llm.chat_json")
    def test_entity_limit_applies_per_chunk_not_per_document(
            self, chat_json, _pmap, _split_text):
        chat_json.side_effect = [
            payload("实体一", "第一块证据"),
            payload("实体二", "第二块证据"),
        ]

        batch = extract("任意原文", "ai", max_entities=1, max_claims=1)

        self.assertEqual(["实体一", "实体二"], [item.name for item in batch.entities])
        prompts = [call.args[0][0]["content"] for call in chat_json.call_args_list]
        self.assertTrue(all("本文本块最多 1 个实体、1 个 Claim" in prompt for prompt in prompts))


if __name__ == "__main__":
    unittest.main()
