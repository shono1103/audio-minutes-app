from minutes_api.routers.health import _worker_ready


def test_transcription_worker_requires_backend_and_every_model_ready() -> None:
    base = {
        "kind": "transcription",
        "backend": {"effective_backend": "cpu", "ready": True},
        "models": [
            {"profile": "multilingual", "ready": True},
            {"profile": "ja", "ready": True},
        ],
    }
    assert _worker_ready(base, True) is True
    assert _worker_ready(base, False) is False
    assert _worker_ready({**base, "backend": {"effective_backend": "cpu"}}, True) is False
    assert _worker_ready({**base, "models": [{"profile": "multilingual", "ready": True}]}, True) is False
    assert _worker_ready({**base, "models": [{"profile": "multilingual", "ready": False}]}, True) is False


def test_minutes_worker_only_requires_a_live_heartbeat() -> None:
    assert _worker_ready({"kind": "minutes", "backend": {}, "models": []}, True) is True
