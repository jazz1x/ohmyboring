#!/usr/bin/env python3
"""boring.json `schedule` / `collect` through the Python parser, with a real process env.

Run: python3 -m unittest src/ohmyboring/test_config_settings.py (or pytest)

drudge/tests/settings_config.rs runs the same fixture through the engine binary, so a changed
value reads the same on both sides.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
FIXTURE = SRC.parent / "drudge" / "tests" / "fixtures" / "settings_cases.json"

_PROBE = """
import dataclasses, json
from ohmyboring import config
from ohmyboring.result import Ok
out = {}
for name, fn in (("schedule", config.schedule), ("collect", config.collect)):
    match fn():
        case Ok(v):
            out[name] = dataclasses.asdict(v)
        case err:
            out[name] = {"error": err.error}
print(json.dumps(out))
"""


def read_in_fresh_process(boring: dict, env: dict) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "boring.json"
        path.write_text(json.dumps({"schema_version": 2, **boring}), encoding="utf-8")
        proc_env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(SRC),
            "BORING_CONFIG": str(path),
            **env,
        }
        done = subprocess.run(
            [sys.executable, "-c", _PROBE], env=proc_env, capture_output=True, text=True, check=False
        )
    assert done.returncode == 0, done.stderr
    return {**json.loads(done.stdout), "_stderr": done.stderr}


class SettingsFixtureTest(unittest.TestCase):
    def test_every_fixture_case_reads_as_the_fixture_says(self):
        for case in json.loads(FIXTURE.read_text(encoding="utf-8")):
            got = read_in_fresh_process(case["boring"], case["env"])
            for block, fields in case["expect"].items():
                for field, want in fields.items():
                    with self.subTest(case=case["name"], field=f"{block}.{field}"):
                        self.assertEqual(got[block][field], want)

    def test_unknown_field_in_a_block_loads_with_one_warning(self):
        got = read_in_fresh_process({"schedule": {"sync_hour": 3, "sync_hours": 6}}, {})
        self.assertEqual(got["schedule"]["sync_hours"], 6)
        warnings = [line for line in got["_stderr"].splitlines() if "unknown field" in line]
        self.assertEqual(len(warnings), 1)
        self.assertIn("sync_hour", warnings[0])

    def test_brief_hour_outside_the_day_is_refused(self):
        got = read_in_fresh_process({"schedule": {"brief_hour": 24}}, {})
        self.assertIn("brief_hour", got["schedule"]["error"])


if __name__ == "__main__":
    unittest.main()
