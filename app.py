"""FastAPI 入口：页面托管、模型配置 CRUD、压测任务生命周期、结果导出。

启动：.venv/bin/python app.py  （默认 http://127.0.0.1:8765）
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import profiles as profiles_mod
from datasets import DATASETS, dataset_meta, get_dataset
from engine import BenchEngine, EngineConfig, summarize, summarize_grouped

BASE_DIR = Path(__file__).resolve().parent
RUNS_DIR = BASE_DIR / "data" / "runs"
HISTORY_LIMIT = 20

app = FastAPI(title="LLM Bench", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

# 运行注册表：run_id -> {"id","engine","config","created_at"}
RUNS: dict[str, dict[str, Any]] = {}


@app.get("/")
def index():
    return FileResponse(BASE_DIR / "static" / "index.html")


# ---------------------------------------------------------------- 模型配置

@app.get("/api/profiles")
def list_profiles():
    return profiles_mod.load_profiles()


@app.get("/api/meta")
def meta():
    return {
        "providers": [
            {"id": pid, "label": p["label"], "base_url": p["base_url"],
             "models": p["models"], "style_fixed": bool(p["thinking_style"])}
            for pid, p in profiles_mod.PROVIDERS.items()
        ],
        "thinking_levels": profiles_mod.THINKING_LEVELS,
        "level_labels": profiles_mod.LEVEL_LABELS,
        "thinking_styles": [
            {"id": s, "label": profiles_mod.STYLE_LABELS.get(s, s)}
            for s in profiles_mod.THINKING_STYLES
        ],
    }


@app.get("/api/capability")
def capability(provider: str = "", model: str = ""):
    """某个提供商+模型的思考能力，前端表单据此决定开关是否可关、档位有哪些。"""
    return profiles_mod.thinking_capability(provider, model)


@app.post("/api/profiles")
def create_profile(raw: dict = Body(...)):
    try:
        profile = profiles_mod.normalize_profile(raw)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    profiles_mod.upsert_profile(profile)
    return profiles_mod.load_profiles()


@app.put("/api/profiles/{profile_id}")
def update_profile(profile_id: str, raw: dict = Body(...)):
    existing = {p["id"] for p in profiles_mod.load_profiles()}
    if profile_id not in existing:
        raise HTTPException(status_code=404, detail="模型配置不存在")
    try:
        profile = profiles_mod.normalize_profile(raw, existing_id=profile_id)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    profiles_mod.upsert_profile(profile)
    return profiles_mod.load_profiles()


@app.delete("/api/profiles/{profile_id}")
def remove_profile(profile_id: str):
    profiles_mod.delete_profile(profile_id)
    return profiles_mod.load_profiles()


# ---------------------------------------------------------------- 数据集

@app.get("/api/datasets")
def list_datasets():
    return dataset_meta()


# ---------------------------------------------------------------- 压测任务

def _active_run() -> Optional[dict[str, Any]]:
    for run in RUNS.values():
        if run.get("engine") and run["engine"].status in ("pending", "running"):
            return run
    return None


@app.post("/api/run")
async def start_run(cfg: dict = Body(...)):
    if _active_run():
        raise HTTPException(status_code=409, detail="已有测试在运行，请先停止或等待完成")

    profile_ids = cfg.get("profile_ids") or []
    all_profiles = profiles_mod.load_profiles()
    id_map = {p["id"]: p for p in all_profiles}
    selected = [dict(id_map[i], name=profiles_mod.display_name(id_map[i]))
                for i in profile_ids if i in id_map]
    if not selected:
        raise HTTPException(status_code=422, detail="请至少选择一个有效的模型配置")
    dataset = get_dataset(str(cfg.get("dataset_id") or ""))
    if not dataset:
        raise HTTPException(status_code=422, detail="数据集不存在")

    try:
        engine_cfg = EngineConfig(
            concurrency=max(1, int(cfg.get("concurrency") or 1)),
            rounds=max(1, int(cfg.get("rounds") or 1)),
            stream=bool(cfg.get("stream", True)),
            max_tokens=int(cfg["max_tokens"]) if cfg.get("max_tokens") else None,
            temperature=float(cfg["temperature"]) if cfg.get("temperature") is not None else None,
            include_usage=bool(cfg.get("include_usage", True)),
            timeout_s=max(5.0, float(cfg.get("timeout_s") or 180.0)),
        )
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="参数格式错误（并发/轮数/max_tokens 需为数字）")

    engine = BenchEngine(selected, dataset, engine_cfg)
    run_id = uuid.uuid4().hex[:10]
    RUNS[run_id] = {"id": run_id, "engine": engine, "config": cfg, "created_at": time.time()}
    _prune_history()
    engine.start()
    # 跑完自动把完整结果落到 data/runs/
    engine._task.add_done_callback(lambda _t, rid=run_id: _save_run_file(rid))
    return {"run_id": run_id, "total_requests": engine.total}


def _save_run_file(run_id: str) -> None:
    try:
        payload = _run_payload(run_id)
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(payload["created_at"]))
        path = RUNS_DIR / f"bench-{stamp}-{run_id}.json"
        path.write_text(_json_dump(payload), encoding="utf-8")
        run = RUNS.get(run_id)
        if run is not None:
            run["file"] = str(path)
    except Exception:
        pass  # 落盘失败不影响测试结果本身


def _prune_history() -> None:
    finished = sorted(
        [(rid, r) for rid, r in RUNS.items()
         if r.get("engine") is None or r["engine"].status not in ("pending", "running")],
        key=lambda kv: kv[1]["created_at"],
    )
    for rid, _ in finished[:-HISTORY_LIMIT]:
        RUNS.pop(rid, None)


def _run_payload(run_id: str) -> dict[str, Any]:
    run = RUNS.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="运行不存在")
    engine = run["engine"]
    if engine is None:  # 导入的历史运行：直接返回存好的 payload
        payload = dict(run["payload"])
        payload["id"] = run_id
        _backfill_providers(payload)
        return payload
    snap = engine.snapshot()
    per_profile = []
    for p in engine.profiles:
        recs = [r for r in snap["records"] if r["profile_id"] == p["id"]]
        if not recs:
            continue
        per_profile.append({
            "profile_id": p["id"],
            "profile_name": p.get("name"),
            "profile_provider": p.get("provider"),
            "summary": summarize(recs),
            "by_case": summarize_grouped(recs, "case_id"),
            "by_round": summarize_grouped(recs, "round_idx"),
        })
    return {
        "id": run_id,
        "status": snap["status"],
        "error": snap["error"],
        "created_at": run["created_at"],
        "started_at": snap["started_at"],
        "finished_at": snap["finished_at"],
        "config": run["config"],
        "dataset": {"id": engine.dataset["id"], "name": engine.dataset["name"]},
        "engine_config": engine.cfg.__dict__,
        "progress": {
            **snap["progress"],
            "current_profile": snap["current_profile"],
        },
        "summary": {"per_profile": per_profile},
        "records": snap["records"],
    }


@app.get("/api/runs")
def list_runs():
    out = []
    for rid, run in sorted(RUNS.items(), key=lambda kv: -kv[1]["created_at"]):
        e = run["engine"]
        if e is None:
            p = run["payload"]
            out.append({
                "id": rid, "status": p.get("status"), "created_at": run["created_at"],
                "profiles": sorted({r.get("profile_name") for r in p.get("records") or []
                                    if r.get("profile_name")}),
                "dataset": (p.get("dataset") or {}).get("name", "?"),
                "progress": p.get("progress") or {},
                "imported": True,
            })
            continue
        out.append({
            "id": rid, "status": e.status, "created_at": run["created_at"],
            "profiles": [p.get("name") for p in e.profiles],
            "dataset": e.dataset["name"],
            "progress": {"total": e.total, "completed": e.completed, "failed": e.failed},
            "imported": False,
        })
    return out


@app.get("/api/run/{run_id}/status")
def run_status(run_id: str):
    return _run_payload(run_id)


@app.post("/api/run/{run_id}/stop")
def stop_run(run_id: str):
    run = RUNS.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="运行不存在")
    if run.get("engine") is None:
        raise HTTPException(status_code=400, detail="导入的历史运行无需停止")
    run["engine"].stop()
    return {"status": "stopping"}


def _register_payload(payload: dict) -> str:
    """把一份完整结果 payload 注册进内存（导入与文件加载共用），返回 run_id。"""
    run_id = str(payload.get("id") or "") or uuid.uuid4().hex[:10]
    if run_id in RUNS:  # 和内存中的运行撞 id 就换一个
        run_id = uuid.uuid4().hex[:10]
    payload["id"] = run_id
    RUNS[run_id] = {
        "id": run_id, "engine": None, "config": payload.get("config") or {},
        "created_at": payload.get("created_at") or time.time(),
        "payload": payload,
    }
    return run_id


# ---------------------------------------------------------------- 落盘文件

_THINK_SUFFIX = re.compile(r"·思考(低|中|高)$")


def _infer_provider(display_name: Optional[str]) -> Optional[str]:
    """旧落盘文件没有 provider 字段：按模型名（去掉思考后缀）从当前配置推断。"""
    try:
        from profiles import load_profiles
        by_model = {p["model"]: p.get("provider") for p in load_profiles()}
    except Exception:
        return None
    if not display_name:
        return None
    return by_model.get(_THINK_SUFFIX.sub("", display_name)) or by_model.get(display_name)


def _backfill_providers(payload: dict) -> None:
    for r in payload.get("records") or []:
        if r.get("profile_provider") is None:
            r["profile_provider"] = _infer_provider(r.get("profile_name"))
    for pp in (payload.get("summary") or {}).get("per_profile") or []:
        if pp.get("profile_provider") is None:
            pp["profile_provider"] = _infer_provider(pp.get("profile_name"))

_FILE_NAME_RE = re.compile(r"^bench-[\w.-]+\.json$")


@app.get("/api/files")
def list_files():
    from engine import summarize
    out = []
    if RUNS_DIR.exists():
        for p in RUNS_DIR.glob("bench-*.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                records = data.get("records") or []
                s = summarize(records)
            except (json.JSONDecodeError, OSError, ValueError):
                continue
            # 模型名 -> 提供商（按出现顺序）；旧文件没有 provider 字段则为 None
            seen: list[str] = []
            prov_of: dict[str, Any] = {}
            for r in records:
                n = r.get("profile_name")
                if n and n not in prov_of:
                    seen.append(n)
                    prov_of[n] = r.get("profile_provider") or _infer_provider(n)
            out.append({
                "file": p.name,
                "id": data.get("id"),
                "created_at": data.get("created_at") or p.stat().st_mtime,
                "profiles": sorted(prov_of),
                "profile_items": [{"name": n, "provider": prov_of[n]} for n in seen],
                "dataset": (data.get("dataset") or {}).get("name", "?"),
                "status": data.get("status"),
                "n": s["n"], "ok": s["ok"],
                "ttft_p50": (s.get("ttft_ms") or {}).get("p50"),
                "tps_mean": (s.get("tps") or {}).get("mean"),
                "cache_hit_rate": s.get("cache_hit_rate"),
                "cache_supported": s.get("cache_supported"),
            })
    out.sort(key=lambda x: -x["created_at"])
    return out


@app.post("/api/files/load")
def load_file(body: dict = Body(...)):
    name = str(body.get("file") or "")
    if not _FILE_NAME_RE.fullmatch(name):
        raise HTTPException(status_code=422, detail="文件名不合法")
    path = RUNS_DIR / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        raise HTTPException(status_code=422, detail="文件不是合法 JSON")
    if not payload.get("records") or not payload.get("summary"):
        raise HTTPException(status_code=422, detail="文件格式不对：缺少 records / summary")
    return {"run_id": _register_payload(payload), "n_records": len(payload["records"])}


@app.delete("/api/files")
def delete_file(file: str):
    if not _FILE_NAME_RE.fullmatch(file or ""):
        raise HTTPException(status_code=422, detail="文件名不合法")
    path = RUNS_DIR / file
    if not path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    path.unlink()
    return {"deleted": file}


def _json_dump(payload: dict) -> str:
    import json
    return json.dumps(payload, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
