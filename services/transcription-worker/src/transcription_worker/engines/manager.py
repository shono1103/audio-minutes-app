"""モデルの常駐管理。原則 1 モデル常駐、メモリに余裕がある場合だけ 2 モデル。窓ごとの再ロードはしない。"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable

from transcription_worker.engines.base import ModelRef, Profile, TranscriptionEngine

EngineFactory = Callable[[ModelRef], TranscriptionEngine]


class ModelManager:
    def __init__(self, factory: EngineFactory, models: dict[Profile, ModelRef], *, max_resident: int = 1) -> None:
        self._factory = factory
        self._models = models
        self._max_resident = max(1, max_resident)
        self._engines: dict[Profile, TranscriptionEngine] = {}
        self._resident: OrderedDict[Profile, None] = OrderedDict()
        self.load_events: list[tuple[str, Profile]] = []  # テスト・計測用 (load / unload)

    def ref(self, profile: Profile) -> ModelRef:
        return self._models[profile]

    def available(self, profile: Profile) -> bool:
        return profile in self._models

    def get(self, profile: Profile) -> TranscriptionEngine:
        """必要なら他のモデルを unload してから load する。"""
        if profile not in self._models:
            raise KeyError(profile)
        engine = self._engines.get(profile)
        if engine is None:
            engine = self._factory(self._models[profile])
            self._engines[profile] = engine
        if not engine.loaded:
            while len(self._resident) >= self._max_resident:
                oldest, _ = self._resident.popitem(last=False)
                self._engines[oldest].unload()
                self.load_events.append(("unload", oldest))
            engine.load()
            self.load_events.append(("load", profile))
        self._resident.pop(profile, None)
        self._resident[profile] = None
        return engine

    def unload_all(self) -> None:
        for profile, engine in self._engines.items():
            if engine.loaded:
                engine.unload()
                self.load_events.append(("unload", profile))
        self._resident.clear()

    def engines(self) -> dict[Profile, TranscriptionEngine]:
        return dict(self._engines)
