"""Tests for the server-side Robie Call phone directory."""

from src.voice.call_directory import (
    HARDCODED_CARRIER_PHONES,
    EzlynxExtractedDirectorySource,
    KnownCarrierPhonesSeedSource,
    StreetSmartDirectoryHook,
    VoiceCallDirectory,
    lookup_carrier_phone,
    main as directory_main,
)


def test_lookup_prefers_runtime_store_over_seed_and_hardcoded(tmp_path):
    seed = tmp_path / "voice_call_directory.seed.json"
    seed.write_text(
        '{"version": 1, "phones": {"the hartford": "+18005551234", "travelers": "+18002386225"}}\n',
        encoding="utf-8",
    )
    runtime = tmp_path / "voice_call_directory.json"
    runtime.write_text(
        '{"version": 1, "phones": {"the hartford": "+18009991111", "obscure mutual": "+18001234567"}}\n',
        encoding="utf-8",
    )
    store = VoiceCallDirectory(
        runtime_path=runtime,
        seed_path=seed,
        hardcoded={"the hartford": "+18005550000"},
    )

    assert store.lookup("The Hartford") == "+18009991111"
    assert store.lookup("Travelers") == "+18002386225"
    assert store.lookup("Obscure Mutual") == "+18001234567"
    assert store.lookup("Unknown Carrier LLC") is None


def test_lookup_falls_back_to_hardcoded_when_files_missing(tmp_path):
    store = VoiceCallDirectory(
        runtime_path=tmp_path / "missing-runtime.json",
        seed_path=tmp_path / "missing-seed.json",
    )
    assert store.lookup("Utica First") == HARDCODED_CARRIER_PHONES["utica first"]
    assert store.lookup("Carlo Ferrara") == "+17329953409"


def test_dispatcher_lookup_uses_directory_module(tmp_path, monkeypatch):
    runtime = tmp_path / "voice_call_directory.json"
    runtime.write_text(
        '{"phones": {"tip national": "+18000000001"}}\n',
        encoding="utf-8",
    )
    store = VoiceCallDirectory(
        runtime_path=runtime,
        seed_path=tmp_path / "no-seed.json",
        hardcoded=HARDCODED_CARRIER_PHONES,
    )
    from src.voice import call_directory as cd
    from src.voice.ezlynx_label_dispatcher import lookup_known_carrier_phone

    monkeypatch.setattr(cd, "_DEFAULT_DIRECTORY", store)
    assert lookup_known_carrier_phone("TIP National") == "+18000000001"
    assert lookup_carrier_phone("Progressive", directory=store) == HARDCODED_CARRIER_PHONES["progressive"]


def test_seed_source_reads_committed_shape(tmp_path):
    seed = tmp_path / "seed.json"
    seed.write_text('{"phones": {"coterie": "+18555673421"}}\n', encoding="utf-8")
    assert KnownCarrierPhonesSeedSource(seed_path=seed).scrape()["coterie"] == "+18555673421"


def test_ezlynx_extracted_source_reads_hydrator_shape(tmp_path):
    extracted = tmp_path / "ezlynx_full_extracted_directory.json"
    extracted.write_text(
        '{"Utica First": {"directory": {"phone": "800-556-5376", "extension": "1"}}}\n',
        encoding="utf-8",
    )
    phones = EzlynxExtractedDirectorySource(search_paths=[extracted]).scrape()
    assert phones["utica first"] == "+18005565376"


def test_streetsmart_hook_is_noop_without_export(tmp_path):
    hook = StreetSmartDirectoryHook(export_path=tmp_path / "missing.json")
    assert hook.scrape() == {}


def test_refresh_writes_runtime_store(tmp_path):
    runtime = tmp_path / "voice_call_directory.json"
    seed = tmp_path / "seed.json"
    seed.write_text('{"phones": {"hartford": "+18005551234"}}\n', encoding="utf-8")
    store = VoiceCallDirectory(runtime_path=runtime, seed_path=seed)
    result = store.refresh(sources=[KnownCarrierPhonesSeedSource(seed_path=seed)])
    assert result["count"] == 1
    assert runtime.exists()
    assert store.lookup("Hartford") == "+18005551234"


def test_directory_cli_dry_run_does_not_write(tmp_path, capsys):
    runtime = tmp_path / "voice_call_directory.json"
    seed = tmp_path / "seed.json"
    seed.write_text('{"phones": {"hartford": "+18005551234"}}\n', encoding="utf-8")
    rc = directory_main([
        "--bootstrap",
        "--dry-run",
        "--runtime-path",
        str(runtime),
        "--seed-path",
        str(seed),
    ])
    assert rc == 0
    assert not runtime.exists()
    out = capsys.readouterr().out
    assert "dry_run" in out
    assert "hartford" in out.lower() or '"count": 1' in out
