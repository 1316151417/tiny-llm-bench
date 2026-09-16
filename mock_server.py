"""本地 mock 服务：OpenAI 兼容的 /v1/chat/completions，用于联调和无 Key 体验。

特性：
- SSE 流式与非流式
- 模拟 TTFT 抖动与逐 token 延迟
- 模拟 prompt 前缀缓存：相同前缀再次出现时按 DeepSeek 风格返回 prompt_cache_hit_tokens
- 模型名含 "think" 或请求带思考参数时，先流式输出 reasoning_content 再输出正文
- 模型名含 "fail" 时返回 429，用于验证错误路径

启动：.venv/bin/python mock_server.py  （默认 http://127.0.0.1:8766）
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI(docs_url=None, redoc_url=None)

_PREFIXES: list[list[dict]] = []  # 已见过的 messages 列表，用于模拟前缀缓存


def _prefix_hit_ratio(messages: list[dict]) -> float:
    """与历史请求的最长「整条消息粒度」公共前缀占比（模拟 KV cache 命中）。

    只有完整匹配到第 k 条消息才算命中 k 条，避免 JSON 结构性前缀造成的假命中。
    """
    total_chars = sum(len(str(m.get("content") or "")) for m in messages)
    if not total_chars:
        return 0.0
    best_chars = 0
    for prev in _PREFIXES:
        k = 0
        while k < len(prev) and k < len(messages) and prev[k] == messages[k]:
            k += 1
        if k:
            best_chars = max(best_chars, sum(len(str(prev[i].get("content") or "")) for i in range(k)))
    return best_chars / total_chars


@app.post("/v1/chat/completions")
async def chat_completions(req: dict, request: Request):
    model = str(req.get("model") or "mock")
    messages = req.get("messages") or []
    stream = bool(req.get("stream"))
    body_extra = {k: v for k, v in req.items() if k not in ("messages", "model", "stream")}

    if "fail" in model:
        return JSONResponse(status_code=429, content={"error": {"message": "mock rate limited"}})

    # 是否输出思考流：看请求里思考参数的「值」，而不是「有没有这个参数」
    # （客户端关闭思考时会发 thinking.type=disabled，不能据此认为要思考）
    thinking = "think" in model
    think_param = body_extra.get("thinking")
    if isinstance(think_param, dict):
        thinking = think_param.get("type") != "disabled"
    elif "enable_thinking" in body_extra:
        thinking = bool(body_extra["enable_thinking"])
    elif "reasoning_effort" in body_extra:
        thinking = True

    prompt_chars = sum(len(str(m.get("content") or "")) for m in messages)
    prompt_tokens = max(1, prompt_chars // 2)
    cached_tokens = int(prompt_tokens * _prefix_hit_ratio(messages) * 0.9) \
        if _PREFIXES else 0
    _PREFIXES.append([dict(m) for m in messages])
    cached_tokens = min(cached_tokens, max(0, prompt_tokens - 1))  # 至少 1 个 token miss

    n_chunks = min(req.get("max_tokens") or 64, 96)
    rand = random.Random(hashlib.md5(json.dumps(messages, ensure_ascii=False).encode()).hexdigest())
    ttft_delay = 0.12 + prompt_tokens * 0.0004 + rand.random() * 0.15
    per_chunk = 0.008 + rand.random() * 0.006

    def usage_dict(completion_tokens: int, reasoning_tokens: int) -> dict:
        u = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens + reasoning_tokens,
            "prompt_cache_hit_tokens": cached_tokens,
            "prompt_cache_miss_tokens": prompt_tokens - cached_tokens,
        }
        if reasoning_tokens:
            u["completion_tokens_details"] = {"reasoning_tokens": reasoning_tokens}
        return u

    if not stream:
        await asyncio.sleep(ttft_delay + n_chunks * per_chunk)
        content = "这是 mock 的非流式回复，用于验证整体延迟指标。" * 3
        return {
            "id": "mock-1", "object": "chat.completion", "model": model,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": content}}],
            "usage": usage_dict(48, 12 if thinking else 0),
        }

    async def gen():
        now = lambda: int(time.time())
        await asyncio.sleep(ttft_delay)
        first = {"id": "mock-2", "object": "chat.completion.chunk", "created": now(),
                 "model": model, "choices": [{"index": 0, "delta": {"role": "assistant"}}]}
        yield f"data: {json.dumps(first, ensure_ascii=False)}\n\n"
        if thinking:
            for _ in range(16):
                await asyncio.sleep(per_chunk)
                c = {"object": "chat.completion.chunk", "model": model,
                     "choices": [{"index": 0, "delta": {"reasoning_content": "思考片段内容。"}}]}
                yield f"data: {json.dumps(c, ensure_ascii=False)}\n\n"
        for i in range(n_chunks):
            await asyncio.sleep(per_chunk)
            c = {"object": "chat.completion.chunk", "model": model,
                 "choices": [{"index": 0, "delta": {"content": f"回复第{i + 1}段，"}}]}
            yield f"data: {json.dumps(c, ensure_ascii=False)}\n\n"
        last = {"object": "chat.completion.chunk", "model": model, "choices": [{"index": 0, "delta": {}}],
                "usage": usage_dict(n_chunks, 16 if thinking else 0)}
        yield f"data: {json.dumps(last, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8766, log_level="warning")
