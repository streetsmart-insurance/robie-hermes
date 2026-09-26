import pytest

from scripts.magellan_empty_gate import (
    EMPTY_MAGELLAN_REASON,
    MagellanEmptyExtractError,
    magellan_verified_genuine_zero,
    refuse_unverified_empty_magellan,
    snapshot_magellan_blocks_delivery,
)


def _empty_table():
    return {
        "calls": [],
        "source_status": "available",
        "pages_complete": True,
        "older_boundary_reached": False,
        "pagination_exhausted": True,
        "rows_inspected": 0,
        "records_on_target_date": 0,
        "from_phone": "7325550100",
    }


def _verified_zero():
    return {
        "calls": [],
        "source_status": "available",
        "pages_complete": True,
        "older_boundary_reached": True,
        "pagination_exhausted": False,
        "rows_inspected": 12,
        "records_on_target_date": 0,
    }


def _calls_without_sad():
    return {
        "calls": [{"sentiment": "Satisfied", "date_time": "09/24/2026 10:00 AM"}],
        "source_status": "available",
        "pages_complete": True,
        "older_boundary_reached": True,
        "rows_inspected": 4,
        "records_on_target_date": 1,
    }


def test_empty_table_race_is_not_a_verified_zero():
    assert magellan_verified_genuine_zero(_empty_table()) is False


def test_walked_table_with_no_target_date_rows_is_a_verified_zero():
    assert magellan_verified_genuine_zero(_verified_zero()) is True


def test_publish_and_deliver_fail_closed_on_unverified_empty():
    for publish, deliver in ((True, False), (False, True), (True, True)):
        with pytest.raises(MagellanEmptyExtractError, match="Refusing to publish or deliver"):
            refuse_unverified_empty_magellan(_empty_table(), publish=publish, deliver=deliver)


def test_error_names_the_empty_table_evidence_and_not_a_phone():
    with pytest.raises(MagellanEmptyExtractError) as caught:
        refuse_unverified_empty_magellan(_empty_table(), publish=True, deliver=True)
    message = str(caught.value)
    assert "rows_inspected=0" in message
    assert "older_boundary_reached=False" in message
    assert "7325550100" not in message
    assert EMPTY_MAGELLAN_REASON in message


def test_collect_only_stays_soft():
    refuse_unverified_empty_magellan(_empty_table(), publish=False, deliver=False)


def test_verified_zero_and_non_sad_calls_may_publish():
    refuse_unverified_empty_magellan(_verified_zero(), publish=True, deliver=True)
    refuse_unverified_empty_magellan(_calls_without_sad(), publish=True, deliver=True)


def test_allow_empty_override_is_exact_and_default_off():
    refuse_unverified_empty_magellan(
        _empty_table(),
        publish=True,
        deliver=True,
        environ={"MAGELLAN_ALLOW_EMPTY": "1"},
    )
    refuse_unverified_empty_magellan(
        _empty_table(),
        publish=True,
        deliver=True,
        environ={"MAGELLAN_ALLOW_EMPTY": " 1 "},
    )
    for value in ("", "0", "true", "yes"):
        with pytest.raises(MagellanEmptyExtractError):
            refuse_unverified_empty_magellan(
                _empty_table(),
                publish=True,
                deliver=True,
                environ={"MAGELLAN_ALLOW_EMPTY": value},
            )


def test_reused_snapshot_without_a_stamp_blocks_delivery():
    departments = {
        "Personal Lines": {"magellan_sad": []},
        "Commercial Lines": {"magellan_sad": []},
    }
    assert snapshot_magellan_blocks_delivery(departments) is True


def test_reused_snapshot_with_calls_or_verified_zero_does_not_block():
    with_calls = {
        "Personal Lines": {
            "magellan_sad": [],
            "magellan_calls_on_target_date": 3,
            "magellan_extract_verified_empty": False,
        }
    }
    verified = {
        "Personal Lines": {
            "magellan_sad": [],
            "magellan_calls_on_target_date": 0,
            "magellan_extract_verified_empty": True,
        }
    }
    with_sad = {"Personal Lines": {"magellan_sad": [{"Sentiment": "Sad"}]}}
    assert snapshot_magellan_blocks_delivery(with_calls) is False
    assert snapshot_magellan_blocks_delivery(verified) is False
    assert snapshot_magellan_blocks_delivery(with_sad) is False
    assert snapshot_magellan_blocks_delivery(
        {"Personal Lines": {"magellan_sad": []}},
        environ={"MAGELLAN_ALLOW_EMPTY": "1"},
    ) is False


def test_refuse_stops_before_a_delivery_side_effect():
    sent = []

    def deliver():
        sent.append("leadership")

    with pytest.raises(MagellanEmptyExtractError):
        refuse_unverified_empty_magellan(_empty_table(), publish=True, deliver=True)
        deliver()
    assert sent == []
