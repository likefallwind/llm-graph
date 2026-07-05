"""MiniMax API 客户端：M3 对话 + embo-01 embedding。

全局并发上限 MAX_CONCURRENCY（默认 6，KG_LLM_CONCURRENCY 可覆盖）：
所有 chat/embed 请求过同一个信号量，无论多少调用方开线程都不会超。
并行入口是 pmap()——verify 复核批、翻译分块、ingest 分块都走它。
"""
import json
import math
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

API_KEY = os.environ.get("MINIMAX_API_KEY", "")
CHAT_URL = "https://api.minimaxi.com/v1/text/chatcompletion_v2"
EMBED_URL = "https://api.minimaxi.com/v1/embeddings"
CHAT_MODEL = "MiniMax-M3"
# 翻译专用模型（英文语料→中文入库），同 endpoint 同鉴权；提取/复核仍用 M3
TRANSLATE_MODEL = os.environ.get("KG_TRANSLATE_MODEL", "minimax-m2.7")
EMBED_MODEL = "embo-01"

MAX_CONCURRENCY = int(os.environ.get("KG_LLM_CONCURRENCY", "6"))
_SEM = threading.BoundedSemaphore(MAX_CONCURRENCY)

_HEADERS = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}


def pmap(fn, items: list) -> list:
    """对 items 并发执行 fn，保持顺序返回；单项即原地执行。
    并发额度由 _SEM 全局保证，线程数只是上限。fn 内不得触碰 sqlite 连接。"""
    if len(items) <= 1:
        return [fn(x) for x in items]
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENCY) as ex:
        return list(ex.map(fn, items))


def _post(url, body):
    """带全局并发闸门的请求；429/5xx 指数退避重试（并发后限流概率上升）。"""
    for attempt in range(4):
        with _SEM:
            resp = requests.post(url, headers=_HEADERS, timeout=600, json=body)
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt < 3:
                time.sleep(5 * 2 ** attempt)
                continue
        resp.raise_for_status()
        return resp
    resp.raise_for_status()
    return resp


def chat(messages, temperature=0.7, model=None) -> str:
    """M3 是 reasoning 模型，思考可以很长，不设 max_tokens 限制。"""
    resp = _post(CHAT_URL, {
        "model": model or CHAT_MODEL,
        "messages": messages,
        "temperature": temperature,
    })
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
        resp = _post(EMBED_URL, {"model": EMBED_MODEL, "texts": batch, "type": kind})
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
