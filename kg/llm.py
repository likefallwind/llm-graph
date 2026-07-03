"""MiniMax API 客户端：M3 对话 + embo-01 embedding。"""
import json
import math
import os
import re

import requests

API_KEY = os.environ.get("MINIMAX_API_KEY", "")
CHAT_URL = "https://api.minimaxi.com/v1/text/chatcompletion_v2"
EMBED_URL = "https://api.minimaxi.com/v1/embeddings"
CHAT_MODEL = "MiniMax-M3"
EMBED_MODEL = "embo-01"

_HEADERS = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}


def chat(messages, temperature=0.7) -> str:
    """M3 是 reasoning 模型，思考可以很长，不设 max_tokens 限制。"""
    resp = requests.post(CHAT_URL, headers=_HEADERS, timeout=600, json={
        "model": CHAT_MODEL,
        "messages": messages,
        "temperature": temperature,
    })
    resp.raise_for_status()
    data = resp.json()
    base = data.get("base_resp", {})
    if base.get("status_code", 0) != 0:
        raise RuntimeError(f"MiniMax 错误: {base}")
    choice = data["choices"][0]
    content = choice["message"]["content"]
    if not content and choice.get("finish_reason") == "length":
        raise RuntimeError("M3 输出被截断（思考未结束）")
    return content


def chat_json(messages):
    """要求模型输出 JSON 并解析；容忍 markdown 代码块包裹；解析失败自动请模型修复一次。"""
    text = chat(messages)
    try:
        return _parse_json(text)
    except (ValueError, json.JSONDecodeError):
        fixed = chat([
            {"role": "system", "content": "你是 JSON 修复器。把用户给的内容修复为语法合法的 JSON（转义字符串内的引号、补齐分隔符），不改动数据内容，只输出 JSON 本身。"},
            {"role": "user", "content": text},
        ], temperature=0.1)
        return _parse_json(fixed)


def _parse_json(text: str):
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1)
    start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
    if start < 0:
        raise ValueError(f"模型输出中没有 JSON: {text[:200]}")
    return json.loads(text[start:_json_end(text, start)])


def _json_end(text: str, start: int) -> int:
    open_ch = text[start]
    close_ch = "}" if open_ch == "{" else "]"
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return i + 1
    return len(text)


def embed(texts, kind="db"):
    """kind: 'db' 入库向量，'query' 查询向量。返回向量列表。"""
    vectors = []
    for i in range(0, len(texts), 32):
        batch = texts[i:i + 32]
        resp = requests.post(EMBED_URL, headers=_HEADERS, timeout=120, json={
            "model": EMBED_MODEL, "texts": batch, "type": kind})
        resp.raise_for_status()
        data = resp.json()
        base = data.get("base_resp", {})
        if base.get("status_code", 0) != 0:
            raise RuntimeError(f"MiniMax embedding 错误: {base}")
        vectors.extend(data["vectors"])
    return vectors


def cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0
