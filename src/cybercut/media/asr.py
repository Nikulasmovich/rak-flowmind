"""WB-02 ASR 适配器边界。

- `AsrAdapter` 协议：`backend` / `model` / `language` / `transcribe(audio_path, duration_us)`；
- `FunASRAdapter` **延迟导入** funasr，禁止自动下载模型；
- 依赖/模型不可用时抛 `AsrDependencyMissing`（不能伪造空结果）；
- 无音轨返回空 transcript 是**节点层**行为（见 `nodes/media_nodes.py`）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from cybercut.contracts.errors import CyberCutError
from cybercut.contracts.media import TranscriptSegment

FUNASR_BACKEND = "funasr"


class AsrDependencyMissing(CyberCutError):
    code = "ASR_DEPENDENCY_MISSING"


class AsrAdapter(Protocol):
    backend: str
    model: str
    language: str | None

    def transcribe(
        self, audio_path: Path, *, duration_us: int | None
    ) -> list[TranscriptSegment]:
        """返回有序、半开区间的文本片段（微秒）。"""
        ...


class FunASRAdapter:
    """FunASR 适配器（延迟导入；不自动下载模型）。"""

    backend = FUNASR_BACKEND

    def __init__(
        self, *, model_path: str,
        language: str | None = "zh",
        vad_model_path: str | None = None,
        device: str = "cpu",
    ) -> None:
        """WB-02R §四.7：只接受**明确存在**的本地模型路径。

        禁止默认远程模型别名（如 "paraformer-zh"）与自动下载。
        """
        from pathlib import Path as _Path

        if not model_path or not _Path(model_path).is_dir():
            raise ValueError(f"FunASR 本地模型路径必须存在：{model_path!r}")
        if vad_model_path and not _Path(vad_model_path).is_dir():
            raise ValueError(f"VAD 模型路径必须存在：{vad_model_path!r}")
        self.model = model_path
        self.language = language
        self._vad_model = vad_model_path
        self._device = device
        self._model_obj: Any = None

    def _load(self) -> Any:
        if self._model_obj is not None:
            return self._model_obj
        try:
            from funasr import AutoModel  # 延迟导入
        except ImportError as exc:  # pragma: no cover - 取决于环境
            raise AsrDependencyMissing(
                "FunASR 不可用（未安装 funasr）；本阶段禁止自动下载模型"
            ) from exc
        kwargs: dict = {"model": self.model, "device": self._device,
                        "disable_update": True}
        if self._vad_model:
            kwargs["vad_model"] = self._vad_model
        self._model_obj = AutoModel(**kwargs)
        return self._model_obj

    def transcribe(
        self, audio_path: Path, *, duration_us: int | None
    ) -> list[TranscriptSegment]:
        model = self._load()
        result = model.generate(input=str(audio_path))
        segments: list[TranscriptSegment] = []
        for item in result or []:
            for sent in item.get("sentence_info", []) or []:
                start_us = int(round(float(sent.get("start", 0)) * 1000))  # ms -> us
                end_us = int(round(float(sent.get("end", 0)) * 1000))
                text = str(sent.get("text", "")).strip()
                if not text:
                    continue
                segments.append(
                    TranscriptSegment(
                        start_us=start_us, end_us=max(end_us, start_us), text=text
                    )
                )
        return normalize_transcript(segments, duration_us=duration_us)


def normalize_transcript(
    segments: list[TranscriptSegment], *, duration_us: int | None
) -> list[TranscriptSegment]:
    """按 start 排序、裁剪到媒体时长、保证半开区间有效。"""
    limit = duration_us
    cleaned: list[TranscriptSegment] = []
    for seg in sorted(segments, key=lambda s: (s.start_us, s.end_us)):
        start, end = seg.start_us, seg.end_us
        if limit is not None:
            start, end = min(start, limit), min(end, limit)
        if end <= start or not seg.text:
            continue
        cleaned.append(seg.model_copy(update={"start_us": start, "end_us": end}))
    return cleaned


__all__ = [
    "FUNASR_BACKEND",
    "AsrAdapter",
    "AsrDependencyMissing",
    "FunASRAdapter",
    "normalize_transcript",
]
