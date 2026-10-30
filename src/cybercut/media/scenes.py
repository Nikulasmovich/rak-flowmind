"""WB-02 镜头切分：PySceneDetect 适配器（不自行发明镜头检测算法）。

- 协议 `SceneDetector` 可注入（单元测试用可控 fake）；
- `PySceneDetectAdapter` **延迟导入** scenedetect，缺失时抛 `SceneDetectDependencyMissing`；
- `normalize_segments`：有序、不重叠、不越过媒体时长、半开区间 `[start_us, end_us)`。
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol

from cybercut.contracts.errors import CyberCutError

SCENE_BACKEND = "pyscenedetect"


class SceneDetectDependencyMissing(CyberCutError):
    code = "SCENE_DEPENDENCY_MISSING"


class SceneDetector(Protocol):
    """镜头切分器协议（仅处理视频；图片由节点生成单段非时间型 segment）。"""

    backend: str

    def detect_segments(
        self, media_path: Path, *, duration_us: int | None
    ) -> list[tuple[int, int]]:
        """返回有序、不重叠的半开区间列表（微秒）。"""
        ...


def normalize_segments(
    segments: list[tuple[int, int]], *, duration_us: int | None
) -> list[tuple[int, int]]:
    """规整镜头区间：升序、去重叠、去除空段、裁剪到媒体时长。"""
    limit = duration_us if duration_us is not None else None
    cleaned: list[tuple[int, int]] = []
    cursor = 0
    for start, end in sorted((int(a), int(b)) for a, b in segments):
        if limit is not None:
            start, end = min(start, limit), min(end, limit)
        start, end = max(start, cursor), max(end, start)
        if end <= start:
            continue
        cleaned.append((start, end))
        cursor = end
        if limit is not None and cursor >= limit:
            break
    return cleaned


class PySceneDetectAdapter:
    """基于 PySceneDetect `ContentDetector` 的镜头切分器（延迟导入）。"""

    backend = SCENE_BACKEND

    def __init__(self, *, threshold: float = 27.0,
                 min_scene_len_frames: int = 2) -> None:
        self.threshold = float(threshold)
        self.min_scene_len_frames = int(min_scene_len_frames)

    def detect_segments(
        self, media_path: Path, *, duration_us: int | None
    ) -> list[tuple[int, int]]:
        try:
            import scenedetect  # 延迟导入：缺失时明确失败，不自动下载
        except ImportError as exc:  # pragma: no cover - 取决于环境
            raise SceneDetectDependencyMissing(
                "PySceneDetect 不可用（未安装）；请安装 scenedetect + opencv-python-headless"
            ) from exc

        scene_list = scenedetect.detect(
            str(media_path),
            scenedetect.ContentDetector(
                threshold=self.threshold, min_scene_len=self.min_scene_len_frames
            ),
        )
        segments: list[tuple[int, int]] = []
        for start, end in scene_list:
            start_us = int(round(start.get_seconds() * 1_000_000))
            end_us = int(round(end.get_seconds() * 1_000_000))
            segments.append((start_us, end_us))
        return normalize_segments(segments, duration_us=duration_us)


__all__ = [
    "SCENE_BACKEND",
    "PySceneDetectAdapter",
    "SceneDetectDependencyMissing",
    "SceneDetector",
    "normalize_segments",
]
