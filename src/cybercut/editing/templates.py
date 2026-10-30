"""内置模板清单（**纯数据**，不执行代码或任意表达式）。

三个首版模板：`product_highlight` / `event_promo` / `brand_story`。
版本固定 `1.0.0`；变更模板内容必须升版本。

模板经 `TemplateManifest` schema 校验后注册，可枚举、可查询。
"""
from __future__ import annotations

from cybercut.contracts.editing import FrameRate, TemplateManifest
from cybercut.contracts.errors import ValidationError

TEMPLATE_VERSION = "1.0.0"

_FPS_30 = FrameRate(num=30, den=1)
_FPS_25 = FrameRate(num=25, den=1)
_FPS_30K = FrameRate(num=30000, den=1001)

_COMMON_TRANSITIONS = ["cut", "fade", "dissolve"]

#: 模板清单按 `template_id` **升序**注册（确定性枚举顺序）
MANIFESTS: dict[str, TemplateManifest] = {
    m.template_id: m
    for m in sorted(
        [
            TemplateManifest.model_validate({
                "template_id": "brand_story",
                "version": TEMPLATE_VERSION,
                "title": "品牌故事",
                "description": "以叙述驱动的品牌片结构：开场建立语境，中段陈述事实，收尾呼吁行动。",
                "applicable_aspect_ratios": ["16:9", "9:16", "1:1"],
                "applicable_frame_rates": [
                    f.model_dump(mode="json") for f in (_FPS_25, _FPS_30, _FPS_30K)
                ],
                "slots": [
                    {"slot_id": "opening", "order": 0, "media_kind": "any",
                     "min_duration_frames": 1, "max_duration_frames": None,
                     "required": True},
                    {"slot_id": "body", "order": 1, "media_kind": "any",
                     "min_duration_frames": 1, "max_duration_frames": None,
                     "required": True},
                    {"slot_id": "closing", "order": 2, "media_kind": "any",
                     "min_duration_frames": 1, "max_duration_frames": None,
                     "required": False},
                ],
                "text_limits": [
                    {"kind": "subtitle", "max_items": 12, "max_chars_per_item": 40},
                    {"kind": "on_screen_text", "max_items": 6, "max_chars_per_item": 24},
                    {"kind": "narration", "max_items": 12, "max_chars_per_item": 120},
                ],
                "missing_slot_strategy": "warn",
                "allowed_transitions": _COMMON_TRANSITIONS,
                "layout": "single-video-track + text-track + audio-track",
            }),
            TemplateManifest.model_validate({
                "template_id": "event_promo",
                "version": TEMPLATE_VERSION,
                "title": "活动推广",
                "description": "节奏紧凑的活动宣传结构：强开场，密集信息，短促收尾。",
                "applicable_aspect_ratios": ["9:16", "1:1"],
                "applicable_frame_rates": [
                    f.model_dump(mode="json") for f in (_FPS_30, _FPS_30K)
                ],
                "slots": [
                    {"slot_id": "hook", "order": 0, "media_kind": "any",
                     "min_duration_frames": 1, "max_duration_frames": None,
                     "required": True},
                    {"slot_id": "highlight", "order": 1, "media_kind": "any",
                     "min_duration_frames": 1, "max_duration_frames": None,
                     "required": True},
                    {"slot_id": "detail", "order": 2, "media_kind": "any",
                     "min_duration_frames": 1, "max_duration_frames": None,
                     "required": True},
                    {"slot_id": "cta", "order": 3, "media_kind": "any",
                     "min_duration_frames": 1, "max_duration_frames": None,
                     "required": False},
                ],
                "text_limits": [
                    {"kind": "subtitle", "max_items": 8, "max_chars_per_item": 30},
                    {"kind": "on_screen_text", "max_items": 10, "max_chars_per_item": 16},
                    {"kind": "narration", "max_items": 8, "max_chars_per_item": 80},
                ],
                "missing_slot_strategy": "fail",
                "allowed_transitions": _COMMON_TRANSITIONS,
                "layout": "single-video-track + text-track + audio-track",
            }),
            TemplateManifest.model_validate({
                "template_id": "product_highlight",
                "version": TEMPLATE_VERSION,
                "title": "产品展示",
                "description": "产品卖点逐条呈现：一个镜头一个卖点，结尾强调行动号召。",
                "applicable_aspect_ratios": ["16:9", "9:16"],
                "applicable_frame_rates": [
                    f.model_dump(mode="json") for f in (_FPS_25, _FPS_30, _FPS_30K)
                ],
                "slots": [
                    {"slot_id": "hero", "order": 0, "media_kind": "any",
                     "min_duration_frames": 1, "max_duration_frames": None,
                     "required": True},
                    {"slot_id": "feature", "order": 1, "media_kind": "any",
                     "min_duration_frames": 1, "max_duration_frames": None,
                     "required": True},
                    {"slot_id": "cta", "order": 2, "media_kind": "any",
                     "min_duration_frames": 1, "max_duration_frames": None,
                     "required": True},
                ],
                "text_limits": [
                    {"kind": "subtitle", "max_items": 10, "max_chars_per_item": 36},
                    {"kind": "on_screen_text", "max_items": 8, "max_chars_per_item": 20},
                    {"kind": "narration", "max_items": 10, "max_chars_per_item": 100},
                ],
                "missing_slot_strategy": "warn",
                "allowed_transitions": _COMMON_TRANSITIONS,
                "layout": "single-video-track + text-track + audio-track",
            }),
        ],
        key=lambda m: m.template_id,
    )
}


def get_manifest(template_id: str) -> TemplateManifest:
    """按 `template_id` 取模板清单；不存在抛 `ValidationError`。"""
    manifest = MANIFESTS.get(template_id)
    if manifest is None:
        raise ValidationError(
            f"模板不存在：{template_id!r}",
            details={"available": sorted(MANIFESTS)},
        )
    return manifest


def list_manifests() -> list[TemplateManifest]:
    """按 `template_id` 升序枚举全部模板清单（确定性）。"""
    return [MANIFESTS[k] for k in sorted(MANIFESTS)]


def select_default_manifest(aspect_ratio: str,
                            frame_rate: FrameRate) -> tuple[TemplateManifest, str]:
    """确定性默认模板策略（ADR-0005 §3）。

    在支持当前**画幅**的清单中按 `template_id` 升序取第一个；
    帧率不匹配不作为筛选条件（由绑定阶段以 `FRAME_RATE_UNSUPPORTED` 报告）。
    """
    candidates = [
        m for m in list_manifests() if aspect_ratio in m.applicable_aspect_ratios
    ]
    if not candidates:
        raise ValidationError(
            f"没有支持画幅 {aspect_ratio!r} 的模板",
            details={"available": {m.template_id: m.applicable_aspect_ratios
                                   for m in list_manifests()}},
        )
    chosen = candidates[0]
    return chosen, "default:first_by_template_id"


__all__ = [
    "MANIFESTS",
    "TEMPLATE_VERSION",
    "get_manifest",
    "list_manifests",
    "select_default_manifest",
]
