"""Immutable release-profile regression tests. No network or browser access."""

from robie_job_engine.release_profile import verify_non_ascend_release


def test_bundled_ascend_sources_remain_environment_gated(monkeypatch):
    monkeypatch.delenv("ROBIE_ASCEND_API_ENABLED", raising=False)

    result = verify_non_ascend_release()

    assert result["ok"] is True
    assert result["profile"] == "ENV_GATED_ASCEND"
    assert result["source_presence_allowed"] is True
    assert result["ascend_source_paths_present"]
    assert result["ascend_registrations"] == []
    assert all(
        item == {"action_type": "hermes.unavailable", "hold_status": "FAILED"}
        for item in result["disabled_routing"].values()
    )
    assert all(
        item == {"action_type": "hermes.google_chat_task", "hold_status": None}
        for item in result["enabled_routing"].values()
    )
