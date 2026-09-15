"""模型配置（profile）的持久化、提供商预设与思考参数映射。

配置存放在 data/profiles.json，由页面管理，服务重启不丢。
每个配置只存：提供商、api_key、模型名（预设提供商可固定）、思考开关/程度/参数风格。
展示名由 模型名+思考状态 自动拼接，不再单独存储。
"""
from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from typing import Any, Optional

DATA_DIR = Path(__file__).resolve().parent / "data"
PROFILES_PATH = DATA_DIR / "profiles.json"

THINKING_LEVELS = ["low", "medium", "high"]
THINKING_STYLES = ["thinking-type", "openai", "qwen", "custom"]

LEVEL_LABELS = {"low": "低", "medium": "中", "high": "高"}

STYLE_LABELS = {
    "thinking-type": "DeepSeek / GLM（thinking.type）",
    "openai": "OpenAI（reasoning_effort）",
    "qwen": "Qwen（enable_thinking）",
    "custom": "不传思考参数",
}

# 提供商预设：base_url / 模型可选列表（None 表示手填）/ 思考参数风格（None 表示可选）
PROVIDERS: dict[str, dict[str, Any]] = {
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        "models": ["deepseek-flash"],
        "thinking_style": "thinking-type",
    },
    "beike": {
        "label": "贝壳",
        "base_url": "https://openapi-ait.ke.com/v1",
        "models": None,          # 模型名自定义
        "thinking_style": None,  # 风格可选（默认 thinking-type）
    },
    "custom": {
        "label": "自定义",
        "base_url": None,
        "models": None,
        "thinking_style": None,
    },
}

_lock = threading.Lock()


def display_name(p: dict[str, Any]) -> str:
    """模型名 + 思考状态拼接的展示名，如 deepseek-flash / glm-4.6·思考高。"""
    model = p.get("model") or "?"
    if p.get("thinking_enabled"):
        return f"{model}·思考{LEVEL_LABELS.get(p.get('thinking_level'), '中')}"
    return model


def _normalize(raw: dict[str, Any], existing_id: Optional[str] = None) -> dict[str, Any]:
    provider = str(raw.get("provider") or "")
    if provider not in PROVIDERS:
        raise ValueError("请选择提供商")

    if provider == "custom":
        base_url = str(raw.get("base_url", "")).strip().rstrip("/")
        if not base_url:
            raise ValueError("自定义提供商需要填写 Base URL")
        if base_url.endswith("/chat/completions"):
            raise ValueError("Base URL 只填到根地址（如 https://xxx/v1），不要带 /chat/completions")
    else:
        base_url = PROVIDERS[provider]["base_url"]

    if provider == "deepseek":
        model = PROVIDERS[provider]["models"][0]
    else:
        model = str(raw.get("model", "")).strip()
        if not model:
            raise ValueError("模型名不能为空")

    level = str(raw.get("thinking_level") or "medium")
    if level not in THINKING_LEVELS:
        raise ValueError(f"思考程度必须是 {THINKING_LEVELS} 之一")

    if PROVIDERS[provider]["thinking_style"]:
        style = PROVIDERS[provider]["thinking_style"]
    else:
        style = str(raw.get("thinking_style") or "thinking-type")
        if style not in THINKING_STYLES:
            raise ValueError(f"思考参数风格必须是 {THINKING_STYLES} 之一")

    return {
        "id": existing_id or str(raw.get("id") or "") or uuid.uuid4().hex[:12],
        "provider": provider,
        "base_url": base_url,
        "api_key": str(raw.get("api_key", "") or ""),
        "model": model,
        "thinking_enabled": bool(raw.get("thinking_enabled", False)),
        "thinking_level": level,
        "thinking_style": style,
    }


def normalize_profile(raw: dict[str, Any], *, existing_id: Optional[str] = None) -> dict[str, Any]:
    """把页面提交的数据整理成规范 profile；校验失败抛 ValueError。"""
    return _normalize(raw, existing_id)


def _migrate(p: dict[str, Any]) -> dict[str, Any]:
    """旧结构（带 name/thinking_budget/extra_body）→ 新结构。按 base_url 推断提供商。"""
    if "provider" in p:
        return p
    bu = str(p.get("base_url", ""))
    if "deepseek.com" in bu:
        provider = "deepseek"
    elif "openapi-ait.ke.com" in bu:
        provider = "beike"
    else:
        provider = "custom"
    return _normalize({
        "id": p.get("id"),
        "provider": provider,
        "base_url": bu,
        "api_key": p.get("api_key", ""),
        "model": p.get("model", ""),
        "thinking_enabled": p.get("thinking_enabled", False),
        "thinking_level": p.get("thinking_level") if p.get("thinking_level") in THINKING_LEVELS else "medium",
        "thinking_style": p.get("thinking_style") if p.get("thinking_style") in THINKING_STYLES else "thinking-type",
    }, existing_id=p.get("id"))


def load_profiles() -> list[dict[str, Any]]:
    with _lock:
        if not PROFILES_PATH.exists():
            return []
        try:
            data = json.loads(PROFILES_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        if not isinstance(data, list):
            return []
        migrated = []
        changed = False
        for p in data:
            if isinstance(p, dict) and "provider" not in p:
                try:
                    migrated.append(_migrate(p))
                    changed = True
                    continue
                except ValueError:
                    continue  # 迁移失败的旧配置直接丢弃
            if isinstance(p, dict) and "provider" in p:
                migrated.append(p)
        profiles = migrated
        if changed:
            PROFILES_PATH.write_text(
                json.dumps(profiles, ensure_ascii=False, indent=2), encoding="utf-8")
        return profiles


def save_profiles(profiles: list[dict[str, Any]]) -> None:
    with _lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        PROFILES_PATH.write_text(
            json.dumps(profiles, ensure_ascii=False, indent=2), encoding="utf-8")


def upsert_profile(profile: dict[str, Any]) -> list[dict[str, Any]]:
    profiles = load_profiles()
    for i, p in enumerate(profiles):
        if p["id"] == profile["id"]:
            profiles[i] = profile
            break
    else:
        profiles.append(profile)
    save_profiles(profiles)
    return profiles


def delete_profile(profile_id: str) -> list[dict[str, Any]]:
    profiles = [p for p in load_profiles() if p["id"] != profile_id]
    save_profiles(profiles)
    return profiles


def build_request_body_extras(profile: dict[str, Any]) -> dict[str, Any]:
    """根据思考配置生成要合并进请求体的额外字段（纯函数，便于测试）。

    - openai:       开 → {"reasoning_effort": level}；关 → 不加
    - thinking-type: 开 → {"thinking": {"type": "enabled"}}；关 → {"type": "disabled"}
    - qwen:         {"enable_thinking": bool}
    - custom:       不生成任何字段
    """
    style = profile.get("thinking_style") or "thinking-type"
    enabled = bool(profile.get("thinking_enabled"))
    level = profile.get("thinking_level") or "medium"

    if style == "openai":
        return {"reasoning_effort": level} if enabled else {}
    if style == "thinking-type":
        return {"thinking": {"type": "enabled" if enabled else "disabled"}}
    if style == "qwen":
        return {"enable_thinking": enabled}
    return {}
