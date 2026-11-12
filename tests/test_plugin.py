"""The plugin manifest and hook registrations point at files that exist."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Plugin(unittest.TestCase):
    def test_manifests_parse_and_agree_on_name(self) -> None:
        plugin = json.loads((ROOT / ".claude-plugin/plugin.json").read_text(encoding="utf-8"))
        market = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text(encoding="utf-8"))
        self.assertEqual(plugin["name"], "fleet-kit")
        self.assertIn("fleet-kit", [p["name"] for p in market["plugins"]])

    def test_every_hook_command_names_an_existing_script(self) -> None:
        hooks = json.loads((ROOT / "hooks/hooks.json").read_text(encoding="utf-8"))
        commands = [
            h["command"]
            for groups in hooks["hooks"].values()
            for group in groups
            for h in group["hooks"]
        ]
        self.assertEqual(len(commands), 2)
        for command in commands:
            script = re.search(r"\$\{CLAUDE_PLUGIN_ROOT\}/(\S+?\.py)", command)
            self.assertIsNotNone(script, command)
            assert script is not None
            self.assertTrue((ROOT / script.group(1)).is_file(), command)

    def test_vendored_hooks_carry_a_uv_script_header(self) -> None:
        for name in ("context_ceiling.py", "wait_cap.py"):
            text = (ROOT / "hooks" / name).read_text(encoding="utf-8")
            self.assertIn("# /// script", text.splitlines()[1])


if __name__ == "__main__":
    unittest.main()
