#!/usr/bin/env python3
"""门禁回归测试：覆盖率口径、70% 边界、Git 读失败不当放行。

这三条都是外部复现包实测出来的判定错误。脚本本身是 PowerShell，
所以用例通过子进程跑真实门禁，而不是复制一份逻辑来互相印证。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
GATE = REPO / "scripts" / "check-coverage.ps1"
MARKERS = REPO / "scripts" / "check-conflict-markers.ps1"
PWSH = shutil.which("pwsh") or shutil.which("powershell")


def run_pwsh(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PWSH, "-NoLogo", "-NoProfile", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        **kwargs,
    )


@unittest.skipUnless(PWSH, "本机没有 PowerShell")
class TestCoverageGate(unittest.TestCase):
    def _gate(self, covered_lines: int, covered_branches: int) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory(prefix="gate-fixture-") as temp:
            root = Path(temp)
            fixture = root / "fixture.json"
            fixture.write_text(
                json.dumps(
                    {
                        "totals": {
                            "num_statements": 100,
                            "covered_lines": covered_lines,
                            "num_branches": 300,
                            "covered_branches": covered_branches,
                            "percent_covered": 89.5,
                        }
                    }
                ),
                encoding="utf-8",
            )
            producer = root / "producer.ps1"
            producer.write_text(
                "$i=[Array]::IndexOf($args,'-o')\n"
                "Copy-Item -LiteralPath $env:RTC_TEST_FIXTURE -Destination $args[$i+1]\n"
                "exit 0\n",
                encoding="utf-8",
            )
            env = dict(os.environ, RTC_TEST_FIXTURE=str(fixture))
            return run_pwsh(["-File", str(GATE), "-Python", str(producer)], env=env)

    def test_statement_below_threshold_is_rejected_even_if_combined_score_is_high(self):
        result = self._gate(79, 300)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)

    def test_exact_branch_boundary_passes(self):
        result = self._gate(100, 210)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


@unittest.skipUnless(PWSH and shutil.which("git"), "本机没有 PowerShell 或 Git")
class TestConflictMarkerGate(unittest.TestCase):
    def test_git_read_error_is_not_treated_as_clean(self):
        with tempfile.TemporaryDirectory(prefix="gate-error-") as temp:
            wrapper = Path(temp) / "run.ps1"
            wrapper.write_text(
                "function global:git { $global:LASTEXITCODE = 128 }\n"
                f"& '{MARKERS}' @args\n"
                "exit $LASTEXITCODE\n",
                encoding="utf-8",
            )
            result = run_pwsh(["-File", str(wrapper)])
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)

    def test_real_marker_still_blocks(self):
        with tempfile.TemporaryDirectory(prefix="gate-marker-") as temp:
            root = Path(temp)
            (root / "scripts").mkdir()
            shutil.copy2(MARKERS, root / "scripts" / "check-conflict-markers.ps1")
            (root / "README.md").write_text("<<<<<<< synthetic-marker\n", encoding="utf-8")
            for args in (["git", "init", str(root)], ["git", "-C", str(root), "add", "README.md"]):
                self.assertEqual(subprocess.run(args, capture_output=True).returncode, 0)
            env = dict(
                os.environ,
                GIT_CONFIG_COUNT="1",
                GIT_CONFIG_KEY_0="safe.directory",
                GIT_CONFIG_VALUE_0=str(root),
            )
            result = run_pwsh(["-File", str(root / "scripts" / "check-conflict-markers.ps1")], env=env)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
