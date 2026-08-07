"""Tests for the routine weight-unit setting and its persistence.

``routine._weight_fields`` has always been able to emit pounds, but nothing
could reach it: ``sync.weight_unit`` was absent from ``DEFAULT_CONFIG``, had no
settings control, and — the part that made it unfixable by hand on Vercel —
``load_config``/``save_config`` never included ``sync`` in the ``app_cache``
keys they round-trip, so a value written there was dropped on the next
stateless invocation. These tests pin all three.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from hevy2garmin.config import DEFAULT_CONFIG, load_config, save_config
from hevy2garmin.routine import routine_to_garmin_workout
from hevy2garmin.sync import _hash_inputs


def _routine() -> dict:
    return {
        "title": "Push Day",
        "exercises": [
            {
                "title": "Bench Press (Barbell)",
                "sets": [{"weight_kg": 100.0, "reps": 5}],
            }
        ],
    }


def _weight_steps(payload: dict) -> list[dict]:
    steps = payload["workoutSegments"][0]["workoutSteps"]
    return [s for s in steps if "weightValue" in s]


class TestDefaultConfig:
    def test_default_is_kilogram(self) -> None:
        """Absent an explicit choice, behaviour is unchanged from before the setting existed."""
        assert DEFAULT_CONFIG["sync"]["weight_unit"] == "kilogram"


class TestHashInputsResolution:
    """_hash_inputs is the single resolver every routine-payload producer uses."""

    def test_resolves_configured_unit(self) -> None:
        cfg = {"sync": {"weight_unit": "pound"}}
        weight_unit, _ = _hash_inputs(cfg)
        assert weight_unit == "pound"

    def test_falls_back_to_kilogram_when_sync_missing(self) -> None:
        weight_unit, _ = _hash_inputs({})
        assert weight_unit == "kilogram"

    def test_end_to_end_pound_payload(self) -> None:
        """The resolved unit must actually reach the Garmin payload.

        Only the number changes. Garmin labels weights with the account's
        measurement preference and ignores unitKey, so the unit object stays
        kilogram — sending the pound unit makes Garmin scale by the
        pound-to-gram factor (45 lb rendered as "20411 lbs").
        """
        cfg = {"sync": {"weight_unit": "pound"}}
        weight_unit, default_rest = _hash_inputs(cfg)
        payload = routine_to_garmin_workout(
            _routine(), weight_unit=weight_unit, default_rest_seconds=default_rest
        )
        step = _weight_steps(payload)[0]
        assert step["weightUnit"] == {"unitId": 8, "unitKey": "kilogram"}
        assert step["weightValue"] == 220.46  # 100 kg, Hevy always reports kg


class TestFilePersistence:
    """Local/Docker deployments: the value round-trips through config.json."""

    def test_pound_round_trips(self, tmp_path: Path) -> None:
        with patch("hevy2garmin.config.CONFIG_DIR", tmp_path), \
             patch("hevy2garmin.config.CONFIG_FILE", tmp_path / "config.json"):
            config = load_config()
            config["sync"]["weight_unit"] = "pound"
            save_config(config)

            assert load_config()["sync"]["weight_unit"] == "pound"

    def test_other_sync_keys_survive(self, tmp_path: Path) -> None:
        """Deep merge must not let a partial sync dict clobber sibling defaults."""
        with patch("hevy2garmin.config.CONFIG_DIR", tmp_path), \
             patch("hevy2garmin.config.CONFIG_FILE", tmp_path / "config.json"):
            config = load_config()
            config["sync"]["weight_unit"] = "pound"
            save_config(config)

            reloaded = load_config()
            assert reloaded["sync"]["grace_period_minutes"] == 120
            assert reloaded["sync"]["default_limit"] == 10


class TestCloudPersistence:
    """Vercel: config.json is read-only, so app_cache is the only durable store."""

    def test_save_persists_sync_to_db(self, tmp_path: Path) -> None:
        fake_db = MagicMock()
        cfg = {
            "user_profile": {"weight_kg": 82.0},
            "timing": {"working_set_seconds": 45},
            "hr_fusion": {"enabled": True},
            "sync": {"weight_unit": "pound", "grace_period_minutes": 120},
        }
        with patch("hevy2garmin.config.CONFIG_DIR", tmp_path), \
             patch("hevy2garmin.config.CONFIG_FILE", tmp_path / "config.json"), \
             patch("hevy2garmin.db.get_database_url", return_value="postgresql://x"), \
             patch("hevy2garmin.db.get_db", return_value=fake_db):
            save_config(cfg)

        written = {c.args[0]: c.args[1] for c in fake_db.set_app_config.call_args_list}
        assert "sync" in written, "sync must be persisted or the setting dies on redeploy"
        assert written["sync"]["weight_unit"] == "pound"

    def test_load_reads_sync_from_db(self, tmp_path: Path) -> None:
        """The read side of the same contract — the half that was missing."""
        fake_db = _fake_db([{"key": "sync", "value": {"weight_unit": "pound"}}])

        with patch("hevy2garmin.config.CONFIG_FILE", tmp_path / "missing.json"), \
             patch("hevy2garmin.db.get_database_url", return_value="postgres://fake"), \
             patch("hevy2garmin.db.get_db", return_value=fake_db):
            config = load_config()

        assert config["sync"]["weight_unit"] == "pound"
        # A partial row must update, not replace, the defaults.
        assert config["sync"]["grace_period_minutes"] == 120


def _fake_db(app_cache_rows: list[dict]):
    """Minimal stand-in for the Postgres cursor load_config queries.

    Unlike the fake in test_merge_mode_persist.py, this one honours the key list
    in the ``IN (...)`` clause. That matters here: the bug under test *is* an
    omission from that list, so a fake that returns rows for any query would
    pass with or without the fix and prove nothing.
    """

    class FakeCursor:
        def __init__(self, rows):
            self._rows = rows
            self._result: list[dict] = []

        def execute(self, sql, params=None):
            if "app_cache" not in sql or "key IN" not in sql:
                self._result = []
                return
            # Only hand back rows the query actually selected for.
            self._result = [r for r in self._rows if f"'{r['key']}'" in sql]

        def fetchall(self):
            return self._result

        def fetchone(self):
            return self._result[0] if self._result else None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    class FakeConn:
        def __init__(self, rows):
            self._rows = rows

        def cursor(self):
            return FakeCursor(self._rows)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    class FakeDb:
        def __init__(self, rows):
            self._rows = rows

        def _get_conn(self):
            return FakeConn(self._rows)

    return FakeDb(app_cache_rows)
