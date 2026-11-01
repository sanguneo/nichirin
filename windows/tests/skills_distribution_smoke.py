# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Build and install both wheels, then exercise their CLI outside the checkout.

Run: python windows/tests/skills_distribution_smoke.py
Requires uv for the existing setuptools build-system dependencies; no runtime
dependencies are added. All build outputs and installations are temporary.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class WheelTests(unittest.TestCase):
    def test_both_wheels_ship_the_canonical_skill_and_shared_runtime(self) -> None:
        for platform, package in (("windows", "kanao"), ("macos", "kamado")):
            with self.subTest(platform=platform), \
                    tempfile.TemporaryDirectory(prefix=f"{package}-wheel-") as directory:
                # Given: a built wheel installed outside the repository.
                work = Path(directory)
                env = dict(os.environ)
                env.pop("PYTHONPATH", None)
                env["PYTHONIOENCODING"] = "utf-8"
                built = subprocess.run(
                    ["uv", "build", "--wheel", str(ROOT / platform),
                     "--out-dir", str(work / "wheels")],
                    cwd=work, env=env, capture_output=True, timeout=180, check=False,
                )
                self.assertEqual(built.returncode, 0, built.stderr.decode("utf-8"))
                wheel, = (work / "wheels").glob("*.whl")
                installed = subprocess.run(
                    ["uv", "pip", "install", "--no-deps", "--target", str(work / "site"),
                     str(wheel)],
                    cwd=work, env=env, capture_output=True, timeout=120, check=False,
                )
                self.assertEqual(installed.returncode, 0, installed.stderr.decode("utf-8"))
                env["PYTHONPATH"] = str(work / "site")
                version = subprocess.run(
                    [sys.executable, "-S", "-m", package, "--version"],
                    cwd=work, env=env, capture_output=True, timeout=30, check=False,
                )
                self.assertEqual(version.returncode, 0, version.stderr)
                runtime_version = version.stdout.decode("utf-8").strip().split()[-1]
                canonical = (
                    ROOT / platform / package / "agent_docs/SKILL.md"
                ).read_text(encoding="utf-8")
                bundled = work / "site" / package / "agent_docs/SKILL.md"
                self.assertEqual(bundled.read_text(encoding="utf-8"), canonical)
                self.assertTrue((work / "site/kakaocommon/skills.py").is_file())
                for topic in ("send", "read", "serve", None):
                    with self.subTest(topic=topic):
                        args = [sys.executable, "-S", "-m", package, "skills", "get"]
                        if topic is not None:
                            args.append(topic)
                        # When: the wheel's CLI serves its bundled document.
                        result = subprocess.run(
                            args, cwd=work, env=env, capture_output=True,
                            timeout=30, check=False,
                        )
                        # Then: all topics carry this wheel's runtime version.
                        self.assertEqual(result.returncode, 0, result.stderr)
                        answer = json.loads(result.stdout)
                        self.assertEqual(set(answer), {"ok", "topic", "version", "content"})
                        self.assertTrue(answer["ok"])
                        self.assertEqual(answer["topic"], topic or "all")
                        self.assertEqual(answer["version"], runtime_version)
                        header = dict(
                            line.split(": ", 1)
                            for line in answer["content"].split("---", 2)[1].strip().splitlines()
                        )
                        self.assertEqual(header["version"], runtime_version)
                        if topic is None:
                            self.assertEqual(answer["content"], canonical)


if __name__ == "__main__":
    unittest.main()
