"""测试引擎：asyncio 并发调度、思考流感知计时、指标统计。

指标口径：
- TTFT：请求发出 → 首个「正文」token（reasoning_content 之后的 content），跳过仅含 role 的空 chunk
- 思考时长：首正文 token − 首思考 token（无思考流则为空）
- 输出速度 tps：completion_tokens ÷ (结束 − 首个任意 token)，token 数以 usage 为准
- 缓存命中率：命中 tokens ÷ prompt tokens，字段不存在则为 None（前端显示 N/A）
"""
from __future__ import annotations

import asyncio
import json
import math
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from profiles import build_request_body_extras


@dataclass
class EngineConfig:
    concurrency: int = 1
    rounds: int = 1
    stream: bool = True
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None
    include_usage: bool = True
    timeout_s: float = 180.0


# ---------------------------------------------------------------- usage 解析（纯函数）

def parse_usage(usage: Any) -> dict[str, Any]:
    """从 OpenAI 兼容 usage 里抽取 token 计数与缓存命中字段，兼容 DeepSeek / OpenAI 两种风格。"""
    out = {
        "prompt_tokens": None, "completion_tokens": None, "reasoning_tokens": None,
        "cached_tokens": None, "cache_field": None,
    }
    if not isinstance(usage, dict):
        return out
    out["prompt_tokens"] = usage.get("prompt_tokens")
    out["completion_tokens"] = usage.get("completion_tokens")
    ctd = usage.get("completion_tokens_details")
    if isinstance(ctd, dict):
        out["reasoning_tokens"] = ctd.get("reasoning_tokens")
    if usage.get("prompt_cache_hit_tokens") is not None:  # DeepSeek 风格
        out["cached_tokens"] = usage["prompt_cache_hit_tokens"]
        out["cache_field"] = "prompt_cache_hit_tokens"
    else:
        ptd = usage.get("prompt_tokens_details")  # OpenAI 风格
        if isinstance(ptd, dict) and ptd.get("cached_tokens") is not None:
            out["cached_tokens"] = ptd["cached_tokens"]
            out["cache_field"] = "prompt_tokens_details.cached_tokens"
    return out


def extract_first_token_flags(delta: dict[str, Any]) -> tuple[bool, bool]:
    """返回 (是否思考 chunk, 是否正文 chunk)，用于首 token 计时。"""
    reasoning = delta.get("reasoning_content") or delta.get("reasoning")
    content = delta.get("content")
    return bool(reasoning), bool(content)


# ---------------------------------------------------------------- 统计（纯函数）

def _percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return float("nan")
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * p
    f, c = math.floor(k), math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def _stats(vals: list[float]) -> Optional[dict[str, float]]:
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    s = sorted(vals)
    return {
        "mean": round(sum(s) / len(s), 1),
        "p50": round(_percentile(s, 0.50), 1),
        "p95": round(_percentile(s, 0.95), 1),
        "p99": round(_percentile(s, 0.99), 1),
        "min": round(s[0], 1),
        "max": round(s[-1], 1),
    }


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [r for r in records if r.get("ok")]
    errs = len(records) - len(ok)

    def col(name: str) -> list[float]:
        return [r[name] for r in ok if r.get(name) is not None]

    prompt_sum = sum(r["prompt_tokens"] for r in ok if r.get("prompt_tokens"))
    cached_sum = sum(r["cached_tokens"] for r in ok if r.get("cached_tokens"))
    completion_sum = sum(r["completion_tokens"] for r in ok if r.get("completion_tokens"))
    reasoning_sum = sum(r["reasoning_tokens"] for r in ok if r.get("reasoning_tokens"))

    starts = [r["start_epoch"] for r in records if r.get("start_epoch") is not None]
    ends = [r["start_epoch"] + (r.get("total_ms") or 0) / 1000
            for r in records if r.get("start_epoch") is not None]
    wall_s = round(max(ends) - min(starts), 2) if starts and ends else None

    cache_supported = any(r.get("cached_tokens") is not None for r in ok)
    cache_hit_rate = round(cached_sum / prompt_sum, 4) if cache_supported and prompt_sum else None
    throughput = round(completion_sum / wall_s, 1) if wall_s and wall_s > 0 and completion_sum else None

    return {
        "n": len(records), "ok": len(ok), "err": errs,
        "error_rate": round(errs / len(records), 4) if records else None,
        "ttft_ms": _stats(col("ttft_ms")),
        "tps": _stats(col("tps")),
        "total_ms": _stats(col("total_ms")),
        "think_ms": _stats(col("think_ms")),
        "prompt_tokens_sum": prompt_sum or None,
        "completion_tokens_sum": completion_sum or None,
        "reasoning_tokens_sum": reasoning_sum or None,
        "cache_supported": cache_supported,
        "cache_hit_rate": cache_hit_rate,
        "wall_s": wall_s,
        "throughput_tps": throughput,
    }


def summarize_grouped(records: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    """按 records[key]（case_id / round_idx）分组汇总，供前端画「按样本」「按轮次」表。"""
    groups: dict[Any, list] = {}
    order: list[Any] = []
    for r in records:
        k = r.get(key)
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(r)
    out = []
    for k in order:
        rs = groups[k]
        s = summarize(rs)
        s[key] = k
        label = None
        if key == "case_id" and rs:
            label = rs[0].get("case_name")
        out.append({"key": k, "label": label, "summary": s})
    return out


# ---------------------------------------------------------------- 引擎

class BenchEngine:
    """一次压测运行。多模型顺序执行；模型内部按并发数并行。"""

    def __init__(self, profiles: list[dict[str, Any]], dataset: dict[str, Any], cfg: EngineConfig):
        self.profiles = profiles
        self.dataset = dataset
        self.cfg = cfg
        self.status = "pending"  # pending / running / done / stopped / error
        self.error: Optional[str] = None
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        self.current_profile: Optional[str] = None
        self.records: list[dict[str, Any]] = []
        self.completed = 0
        self.failed = 0
        reqs_per_profile = sum(
            1 if c["kind"] == "single" else len(c["turns"]) for c in dataset["cases"]
        )
        self.total = reqs_per_profile * cfg.rounds * len(profiles)
        self._stop = False
        self._task: Optional[asyncio.Task] = None

    # -- 生命周期 ----------------------------------------------------

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self.run())

    def stop(self) -> None:
        self._stop = True
        if self._task and not self._task.done():
            self._task.cancel()

    async def run(self) -> None:
        self.status = "running"
        self.started_at = time.time()
        try:
            for profile in self.profiles:
                if self._stop:
                    break
                self.current_profile = profile.get("name")
                await self._run_profile(profile)
        except asyncio.CancelledError:
            self.status = "stopped"
        except Exception as e:  # 引擎级意外错误（配置错、DNS 等）记录并结束
            self.status = "error"
            self.error = f"{type(e).__name__}: {e}"
        finally:
            if self.status == "running":
                self.status = "stopped" if self._stop else "done"
            self.finished_at = time.time()
            self.current_profile = None

    # -- 执行 --------------------------------------------------------

    async def _run_profile(self, profile: dict[str, Any]) -> None:
        sem = asyncio.Semaphore(self.cfg.concurrency)
        timeout = httpx.Timeout(self.cfg.timeout_s, connect=15.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            tasks = []
            for round_idx in range(1, self.cfg.rounds + 1):
                for case in self.dataset["cases"]:
                    if case["kind"] == "single":
                        tasks.append(self._run_single(sem, client, profile, case, round_idx,
                                                      case["messages"]))
                    else:
                        tasks.append(self._run_multi(sem, client, profile, case, round_idx,
                                                     case["turns"]))
            await asyncio.gather(*tasks)

    async def _run_single(self, sem, client, profile, case, round_idx, messages) -> None:
        async with sem:
            if self._stop:
                return
            await self._do_request(client, profile, case, round_idx, 1, 1, messages)

    async def _run_multi(self, sem, client, profile, case, round_idx, turns) -> None:
        # 多轮对话各轮必须串行（后一轮拼前面的剧本前缀），整体占一个并发槽
        async with sem:
            for turn_idx, messages in enumerate(turns, 1):
                if self._stop:
                    return
                await self._do_request(client, profile, case, round_idx, turn_idx,
                                       len(turns), messages)

    def _build_payload(self, profile: dict[str, Any], messages: list[dict]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": profile["model"],
            "messages": messages,
            "stream": self.cfg.stream,
        }
        if self.cfg.max_tokens:
            payload["max_tokens"] = self.cfg.max_tokens
        if self.cfg.temperature is not None:
            payload["temperature"] = self.cfg.temperature
        if self.cfg.stream and self.cfg.include_usage:
            payload["stream_options"] = {"include_usage": True}
        payload.update(build_request_body_extras(profile))
        return payload

    async def _do_request(self, client, profile, case, round_idx, turn_idx, turn_total,
                          messages) -> None:
        url = profile["base_url"].rstrip("/") + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if profile.get("api_key"):
            headers["Authorization"] = f"Bearer {profile['api_key']}"
        payload = self._build_payload(profile, messages)

        rec = {
            "profile_id": profile["id"], "profile_name": profile.get("name"),
            "profile_provider": profile.get("provider"),
            "case_id": case["id"], "case_name": case.get("name"), "kind": case["kind"],
            "round_idx": round_idx, "turn_idx": turn_idx, "turn_total": turn_total,
            "stream": self.cfg.stream,
            "ok": False, "http_status": None, "error": None,
            "start_epoch": None, "ttft_ms": None, "first_reason_ms": None,
            "think_ms": None, "gen_ms": None, "total_ms": None, "tps": None,
            "prompt_tokens": None, "completion_tokens": None, "reasoning_tokens": None,
            "cached_tokens": None, "cache_field": None,
        }

        t0 = time.perf_counter()
        rec["start_epoch"] = time.time()
        try:
            if self.cfg.stream:
                await self._stream_request(rec, client, url, headers, payload, t0)
            else:
                await self._plain_request(rec, client, url, headers, payload, t0)
        except asyncio.CancelledError:
            rec["total_ms"] = rec["total_ms"] or round((time.perf_counter() - t0) * 1000, 1)
            rec["error"] = rec["error"] or "cancelled"
            self._finish_record(rec)
            raise
        except Exception as e:
            rec["ok"] = False
            rec["error"] = f"{type(e).__name__}: {e}"[:300]
            rec["total_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        self._finish_record(rec)

    def _finish_record(self, rec: dict) -> None:
        self.records.append(rec)
        self.completed += 1
        if not rec["ok"]:
            self.failed += 1

    async def _stream_request(self, rec, client, url, headers, payload, t0: float) -> None:
        t_first_reason: Optional[float] = None
        t_first_content: Optional[float] = None
        usage = None
        async with client.stream("POST", url, json=payload, headers=headers) as resp:
            rec["http_status"] = resp.status_code
            if resp.status_code != 200:
                body = (await resp.aread())[:300].decode("utf-8", "replace").strip()
                rec["error"] = f"HTTP {resp.status_code}: {body}"
                rec["total_ms"] = round((time.perf_counter() - t0) * 1000, 1)
                return
            async for line in resp.aiter_lines():
                line = line.strip()
                if not line or line.startswith(":") or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if obj.get("usage"):
                    usage = obj["usage"]
                for choice in obj.get("choices") or []:
                    delta = choice.get("delta") or {}
                    is_reason, is_content = extract_first_token_flags(delta)
                    now = time.perf_counter()
                    if is_reason and t_first_reason is None:
                        t_first_reason = now
                    if is_content and t_first_content is None:
                        t_first_content = now
        t_end = time.perf_counter()
        self._fill_metrics(rec, t0, t_first_reason, t_first_content, t_end, usage)

    async def _plain_request(self, rec, client, url, headers, payload, t0: float) -> None:
        resp = await client.post(url, json=payload, headers=headers)
        rec["http_status"] = resp.status_code
        t_end = time.perf_counter()
        if resp.status_code != 200:
            rec["error"] = f"HTTP {resp.status_code}: {resp.text[:300]}"
            rec["total_ms"] = round((t_end - t0) * 1000, 1)
            return
        try:
            obj = resp.json()
        except ValueError:
            rec["error"] = "响应不是合法 JSON"
            rec["total_ms"] = round((t_end - t0) * 1000, 1)
            return
        usage = obj.get("usage")
        # 非流式没有中间 token 时间戳，只有整体延迟
        self._fill_metrics(rec, t0, None, None, t_end, usage)

    def _fill_metrics(self, rec, t0, t_first_reason, t_first_content, t_end, usage) -> None:
        rec["total_ms"] = round((t_end - t0) * 1000, 1)
        if t_first_content is not None:
            rec["ttft_ms"] = round((t_first_content - t0) * 1000, 1)
        if t_first_reason is not None:
            rec["first_reason_ms"] = round((t_first_reason - t0) * 1000, 1)
        if t_first_reason is not None and t_first_content is not None:
            rec["think_ms"] = round((t_first_content - t_first_reason) * 1000, 1)
        first_any = min(x for x in (t_first_reason, t_first_content) if x is not None) \
            if (t_first_reason is not None or t_first_content is not None) else None
        if first_any is not None:
            rec["gen_ms"] = round((t_end - first_any) * 1000, 1)

        u = parse_usage(usage)
        for k in ("prompt_tokens", "completion_tokens", "reasoning_tokens", "cached_tokens",
                  "cache_field"):
            rec[k] = u[k]

        # 没有任何输出 token 却也无错误（如空响应）视为可疑但不报错，靠 usage 判断
        if u["completion_tokens"] and rec["gen_ms"]:
            rec["tps"] = round(u["completion_tokens"] / (rec["gen_ms"] / 1000), 2)
        rec["ok"] = True

    # -- 状态快照 ----------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "error": self.error,
            "current_profile": self.current_profile,
            "progress": {"total": self.total, "completed": self.completed, "failed": self.failed},
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "records": list(self.records),
        }
