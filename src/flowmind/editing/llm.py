"""创意层 LLM 客户端：Anthropic 兼容 /v1/messages（httpx 直调）。

与 _llm_translate 同协议（LongCat），但面向创意阶段（copy/script/storyboard）：
- 每阶段独立系统提示词，要求只输出 JSON；parse 后经 pydantic 模型校验；
- 解析失败显式抛 EditingLLMError（错误永不静默），调用方映射 degraded；
- feedback（revise 反馈）与 extra_brief 注入 user 内容，驱动定向重生成；
- mock=True（config.editing.mock / env FLOWMIND_EDIT_MOCK）返回内置桩产物，
  供 demo 与无 key 环境冒烟——桩数据结构与真实输出同 schema。
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Callable

import httpx
from pydantic import BaseModel

from flowmind.editing.models import (
    CopyDoc,
    ScriptDoc,
    StoryboardDoc,
)

logger = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class EditingLLMError(Exception):
    """创意层 LLM 调用/解析失败。category 语义与 errors.py 一致。"""

    def __init__(self, message: str, category: str = "unknown",
                 retriable: bool = False) -> None:
        super().__init__(message)
        self.category = category
        self.retriable = retriable


# ── 各阶段系统提示词（中文；结构化 JSON 输出契约写死在提示词里）──

_COPY_SYSTEM = (
    "你是资深短视频编导。根据素材概览与创作要求，产出一条成片的文案。"
    "严格要求：1) 只输出 JSON，不要解释/前缀/代码块；"
    "2) JSON 结构：{\"title\":标题,\"hook\":开头钩子一句话,"
    "\"body_sections\":[{\"purpose\":\"段落职能\",\"text\":\"文案\"}],"
    "\"cta\":\"结尾行动号召\",\"full_narration\":\"完整口播稿（连贯成文）\","
    "\"style_notes\":\"风格说明\"}；"
    "3) 口语化、有节奏感，钩子 3 秒内抓人；4) 时长预算内可承载，别灌水。"
)

_SCRIPT_SYSTEM = (
    "你是分镜脚本编剧。把文案拆成结构化脚本段落。"
    "严格要求：1) 只输出 JSON；"
    "2) 结构：{\"sections\":[{\"id\":\"s1\",\"purpose\":\"hook\","
    "\"narration\":\"该段口播\",\"visual_brief\":\"画面需求描述（供分镜选材）\","
    "\"duration_s\":5.0,\"subtitle_text\":\"该段字幕\"}]}；"
    "3) 段落数 3~8，duration_s 之和不超过时长预算；"
    "4) visual_brief 具体可执行（景别/主体/运动），不要空话。"
)

_STORYBOARD_SYSTEM = (
    "你是视频剪辑师。根据脚本段落与素材内容目录（content_notes：每个抽帧"
    "时刻的真实画面描述与画面字幕文本）挑选素材镜头编排分镜。严格要求："
    "1) 只输出 JSON；"
    "2) 结构：{\"shots\":[{\"id\":\"sh1\",\"section_id\":\"s1\","
    "\"source_asset\":\"素材id\",\"source_in\":0.0,\"duration_s\":4.0,"
    "\"mode\":\"video_clip\",\"pan\":\"none\",\"transition\":\"fade\","
    "\"speed\":1.0,\"freeze_s\":0.0,\"subtitle_text\":\"字幕\","
    "\"note\":\"选材理由\"}],\"bgm_note\":\"配乐意向\"}；"
    "3) 效果白名单（只许用这些，不要许诺渲染器做不到的东西）：mode 只能取 "
    "video_clip / image_kenburns；transition 只能取 fade / none；"
    "speed 变速（1=常速，2=两倍速压缩冗余段，0.5=慢放强调，范围0.25~4）；"
    "freeze_s 尾帧定格（0~3秒，强调关键画面）；"
    "4) 叙事硬约束：镜头按 content_notes 的时间顺序推进（source_in 单调不减，"
    "允许结尾一次回到开头做呼应）；把与段落文案语义最贴合的字幕/画面内容点"
    "选进对应段落；禁止与内容无关的乱序跳跃；"
    "5) 只能引用素材目录里真实存在的素材 id，source_in 优先落在 content_notes"
    " 标出的内容点附近；单镜 duration_s 不小于 0.5，总时长贴近预算；"
    "6) note 只写一句中文选材理由（引用具体画面或字幕内容），供人工审批参考。"
)

_VISION_SYSTEM = (
    "你是视频内容分析员。用户给出一段视频在若干时刻的抽帧（按时间顺序）。"
    "输出 JSON 数组，每帧一个元素：{\"t_s\":<原样返回该帧时刻秒数>,"
    "\"scene\":\"画面内容一句话（主体/场景/动作）\","
    "\"subtitle\":\"画面中烧录的字幕或文字（逐字读出；没有则空字符串）\","
    "\"importance\":<1~5 整数，该时刻的信息与情绪重要度>}。"
    "只输出 JSON 数组，不要解释。"
)

# LLM 常见转场/模式同义词 → schema 枚举（模型爱用自然词汇，做输入归一化
# 而不是苛求提示词；未识别值按最安全档处理并记日志，不静默改语义）
_TRANSITION_ALIAS = {
    "cut": "none", "hard_cut": "none", "hardcut": "none", "jump": "none",
    "jumpcut": "none", "straight": "none", "straight_cut": "none",
    "flash": "fade", "flash_cut": "fade", "crossfade": "fade",
    "cross_fade": "fade", "dissolve": "fade",
}
_MODE_ALIAS = {
    "clip": "video_clip", "video": "video_clip", "shot": "video_clip",
    "kenburns": "image_kenburns", "ken_burns": "image_kenburns",
    "image": "image_kenburns", "photo": "image_kenburns",
}


def _normalize_storyboard(data: dict) -> dict:
    """LLM 输出归一化：转场/模式同义词 + 镜头时长钳制。

    模型爱用自然词汇（cut/hard_cut）与超短闪切（0.2s）——做输入归一化
    而不是苛求提示词；未识别值按最安全档处理并记日志，不静默改语义。
    """
    import logging
    logger = logging.getLogger(__name__)
    for shot in data.get("shots") or []:
        if not isinstance(shot, dict):
            continue
        t = str(shot.get("transition", "fade")).strip().lower()
        if t in ("fade", "none"):
            pass
        else:
            mapped = _TRANSITION_ALIAS.get(t)
            if mapped is None:
                logger.info("未知转场 %r，归一化为 none（硬切）", t)
                mapped = "none"
            shot["transition"] = mapped
        m = str(shot.get("mode", "video_clip")).strip().lower()
        if m not in ("video_clip", "image_kenburns"):
            shot["mode"] = _MODE_ALIAS.get(m, "video_clip")
        try:
            dur = float(shot.get("duration_s"))
        except (TypeError, ValueError):
            dur = 3.0
        clamped = min(60.0, max(0.3, dur))
        if clamped != dur:
            logger.info("镜头 %s 时长 %.2fs 钳制为 %.2fs",
                        shot.get("id", "?"), dur, clamped)
        shot["duration_s"] = clamped
        try:
            speed = float(shot.get("speed", 1.0) or 1.0)
        except (TypeError, ValueError):
            speed = 1.0
        shot["speed"] = min(8.0, max(0.25, speed))
        try:
            freeze = float(shot.get("freeze_s", 0.0) or 0.0)
        except (TypeError, ValueError):
            freeze = 0.0
        shot["freeze_s"] = min(3.0, max(0.0, freeze))
    return data


def generate_json(
    stage: str, payload: dict, *, schema: type[BaseModel],
    api_base: str, api_key: str, model: str,
    timeout_s: float = 90.0, max_tokens: int = 16384,
    mock: bool = False, client: httpx.Client | None = None,
) -> BaseModel:
    """按阶段生成并校验 JSON 产物。stage ∈ copy/script/storyboard。"""
    if not api_key and not mock:
        raise EditingLLMError(
            "创意层缺少 API key（环境变量 AI_LLM_API_KEY）。"
            "云优先原则：不做本地降级，显式报错。", category="environment")
    system, user_payload = _stage_prompt(stage, payload)
    if mock:
        raw = json.dumps(_mock_payload(stage, payload), ensure_ascii=False)
    else:
        # 空 content 重试：thinking 模型偶发把预算全花在思考块上，重采样
        # 通常即可恢复；非瞬时错误（HTTP 4xx/解析失败）不在此重试
        last: EditingLLMError | None = None
        for attempt in range(3):
            try:
                raw = _call_llm(
                    system=system, user_payload=user_payload,
                    api_base=api_base, api_key=api_key, model=model,
                    timeout_s=timeout_s, max_tokens=max_tokens, client=client)
                break
            except EditingLLMError as exc:
                if not exc.retriable or attempt == 2:
                    raise
                last = exc
                logger.warning("阶段 %s LLM 调用失败（可重试，第 %d 次）：%s",
                               stage, attempt + 1, exc)
                time.sleep(1.5 * (attempt + 1))
        else:  # pragma: no cover - range(3) 循环必 break 或 raise
            raise last or EditingLLMError("LLM 调用失败")
    data = _parse_json(raw)
    if stage == "storyboard":
        data = _normalize_storyboard(data)
    try:
        return schema.model_validate(data)
    except Exception as exc:  # noqa: BLE001 LLM 输出不合 schema 显式报错
        raise EditingLLMError(
            f"阶段 {stage} 的 LLM 输出不合 schema：{exc}；原始输出片段："
            f"{raw[:500]}", category="unknown") from exc


def _stage_prompt(stage: str, payload: dict) -> tuple[str, dict]:
    if stage == "copy":
        return _COPY_SYSTEM, payload
    if stage == "script":
        return _SCRIPT_SYSTEM, payload
    if stage == "storyboard":
        return _STORYBOARD_SYSTEM, payload
    raise EditingLLMError(f"未知创意阶段：{stage}", category="unknown")


def _post_messages(*, system: str, content: list[dict], api_base: str,
                   api_key: str, model: str, timeout_s: float,
                   max_tokens: int, client: httpx.Client | None) -> str:
    """Anthropic /v1/messages 底层调用（content 为块列表，兼容 vision）。"""
    url = f"{api_base.rstrip('/')}/v1/messages"
    headers = {
        # 双头兼容：标准 Anthropic 用 x-api-key；Claude Code 风格 token
        # （ANTHROPIC_AUTH_TOKEN，如 MiniMax 兼容端点）用 Authorization
        "x-api-key": api_key,
        "Authorization": f"Bearer {api_key}",
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": content}],
    }
    owns = client is None
    cli = client or httpx.Client(timeout=timeout_s)
    try:
        resp = cli.post(url, headers=headers, json=body)
        if resp.status_code >= 400:
            # 错误消息不回显完整响应体（可能含上游内部信息），状态码+截断片段足够定位
            raise EditingLLMError(
                f"LLM HTTP {resp.status_code}: {resp.text[:200]}",
                category="transient" if resp.status_code >= 500 else "unknown",
                retriable=resp.status_code >= 500)
        data = resp.json()
        parts = data.get("content") or []
        text = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
        if not text.strip():
            # 思考型模型（MiniMax M 系）偶发把 token 预算全花在 thinking 块上，
            # content 为空——这是可重试的瞬时失败，交由调用方重试
            raise EditingLLMError(
                "LLM 返回空 content（可能被 max_tokens 截断或内容被过滤）",
                category="transient", retriable=True)
        return text
    except httpx.HTTPError as exc:
        raise EditingLLMError(f"LLM 网络错误：{exc}",
                              category="transient", retriable=True) from exc
    finally:
        if owns:
            cli.close()


def _call_llm(*, system: str, user_payload: dict, api_base: str,
              api_key: str, model: str, timeout_s: float,
              max_tokens: int, client: httpx.Client | None) -> str:
    """文本调用：payload 序列化为单个 text 块。"""
    content = [{"type": "text",
                "text": json.dumps(user_payload, ensure_ascii=False)}]
    return _post_messages(
        system=system, content=content, api_base=api_base, api_key=api_key,
        model=model, timeout_s=timeout_s, max_tokens=max_tokens, client=client)


def _parse_json(raw: str) -> dict:
    """剥代码栅栏后 json.loads；失败带原文片段显式报错。"""
    text = raw.strip()
    m = _FENCE_RE.search(text)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        # 兜底：截取首个 { 到末个 }（部分模型会带前后缀废话）
        start, end = text.find("{"), text.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass
        raise EditingLLMError(
            f"LLM 输出不是合法 JSON：{exc}；片段：{text[:300]}") from exc


def describe_frames(
    frames: list[tuple[float, str]], *,
    api_base: str, api_key: str, model: str,
    timeout_s: float = 180.0, max_tokens: int = 4096,
    mock: bool = False, client: httpx.Client | None = None,
) -> list[dict]:
    """v1.1 视觉理解：抽帧 → MiniMax vision → 每帧 {t_s,scene,subtitle,
    importance}。

    frames 为 [(时刻秒, jpeg路径)]；返回 dict 列表（调用方过 ContentNote
    校验）。无 key 显式报错；mock 返回桩笔记。失败抛 EditingLLMError，
    由 understand 阶段捕获并显式记 vision_degraded（盲剪降级不静默）。
    """
    import base64
    import logging
    logger = logging.getLogger(__name__)

    if mock:
        return [{"t_s": round(t, 2),
                 "scene": f"mock 画面（{t:.1f}s）",
                 "subtitle": "", "importance": 3} for t, _ in frames]
    if not api_key:
        raise EditingLLMError(
            "视觉理解缺少 API key（环境变量 AI_LLM_API_KEY）",
            category="environment")

    content: list[dict] = [{
        "type": "text",
        "text": f"以下是同一段视频按时间顺序抽取的 {len(frames)} 帧。"
                "请按要求输出每帧的 JSON 分析。",
    }]
    for t, path in frames:
        try:
            data = base64.b64encode(Path(path).read_bytes()).decode()
        except OSError as exc:
            raise EditingLLMError(f"抽帧文件不可读：{path}：{exc}",
                                  category="environment") from exc
        content.append({"type": "text", "text": f"t={t:.2f}s 的帧："})
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg",
                       "data": data},
        })
    # vision 同样可能吃到空 content（thinking 预算耗尽），重试语义同 generate_json
    raw: str | None = None
    for attempt in range(3):
        try:
            raw = _post_messages(
                system=_VISION_SYSTEM, content=content, api_base=api_base,
                api_key=api_key, model=model, timeout_s=timeout_s,
                max_tokens=max_tokens, client=client)
            break
        except EditingLLMError as exc:
            if not exc.retriable or attempt == 2:
                raise
            logger.warning("vision 调用失败（可重试，第 %d 次）：%s",
                           attempt + 1, exc)
            time.sleep(1.5 * (attempt + 1))
    if raw is None:  # pragma: no cover
        raise EditingLLMError("vision 调用失败")
    parsed = _parse_json(raw)
    if isinstance(parsed, dict):   # 模型偶尔包一层 {"frames": [...]}
        parsed = parsed.get("frames") or parsed.get("notes") or []
    if not isinstance(parsed, list):
        raise EditingLLMError(
            f"视觉理解输出不是 JSON 数组：{type(parsed).__name__}")
    out: list[dict] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        item["importance"] = min(5, max(1, int(item.get("importance") or 3)))
        out.append(item)
    if not out:
        raise EditingLLMError("视觉理解返回空数组")
    logger.info("vision 看图完成：%d 帧笔记", len(out))
    return out


# ── mock 桩（schema 与真实一致；演示审批/revise/渲染全流程）──

def _mock_payload(stage: str, payload: dict) -> dict:
    brief = str(payload.get("extra_brief", "") or "")
    feedback = payload.get("_feedback") or []   # revise 反馈注入展示
    suffix = f"【修订{len(feedback)}】" if feedback else ""
    if stage == "copy":
        return {
            "title": f"《{payload.get('theme', '未命名')}》{suffix}",
            "hook": f"开头钩子：{brief or '三秒抓人'}",
            "body_sections": [
                {"purpose": "body", "text": "正文段落一：核心卖点展开"},
                {"purpose": "body", "text": "正文段落二：场景与细节"},
            ],
            "cta": "行动号召：点赞关注不迷路",
            "full_narration": "完整口播稿：钩子 → 正文 → CTA 连贯成文。",
            "style_notes": payload.get("style") or "明快节奏",
        }
    if stage == "script":
        return {"sections": [
            {"id": "s1", "purpose": "hook", "narration": "你知道吗？",
             "visual_brief": "特写开场，主体居中", "duration_s": 3.0,
             "subtitle_text": "你知道吗？"},
            {"id": "s2", "purpose": "body", "narration": "这就是本期主角。",
             "visual_brief": "中景推进，展示细节", "duration_s": 5.0,
             "subtitle_text": "这就是本期主角。"},
            {"id": "s3", "purpose": "cta", "narration": "点个赞再走。",
             "visual_brief": "定格收尾", "duration_s": 2.0,
             "subtitle_text": "点个赞再走。"},
        ], }
    if stage == "storyboard":
        # catalog 在 _stage_storyboard 已序列化为 dict（LLM 载荷形态）
        assets: list[dict] = payload.get("assets") or []
        video = next((a for a in assets if a.get("kind") == "video"), None)
        image = next((a for a in assets if a.get("kind") == "image"), None)
        shots: list[dict] = []
        sid = 1
        for sec in payload.get("sections", []):
            src = video
            mode = "video_clip"
            if src is None and image is not None:
                src, mode = image, "image_kenburns"
            if src is None:
                continue
            shots.append({
                "id": f"sh{sid}", "section_id": sec["id"],
                "source_asset": src["id"], "source_in": 0.0,
                "duration_s": sec.get("duration_s", 3.0),
                "mode": mode, "pan": "in" if mode == "image_kenburns" else "none",
                "transition": "fade",
                "subtitle_text": sec.get("subtitle_text") or sec.get("narration", ""),
                "note": f"mock 选材：{sec.get('visual_brief', '')}{suffix}",
            })
            sid += 1
        return {"shots": shots, "bgm_note": "轻快电子乐"}
    raise EditingLLMError(f"未知 mock 阶段：{stage}")


# 类型别名方便调用方标注（copy/script/storyboard 三产物）
StageDoc = CopyDoc | ScriptDoc | StoryboardDoc
StageGenerator = Callable[..., StageDoc]
