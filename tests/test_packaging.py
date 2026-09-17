from __future__ import annotations

import importlib
import importlib.resources
import os
import subprocess
import sys
import tomllib
import unittest
from pathlib import Path

from agent_trace_studio.cli import _is_source_checkout
from agent_trace_studio.supervisor import SupervisorLaunchSpec

ROOT = Path(__file__).resolve().parents[1]


class PackageLayoutTest(unittest.TestCase):
    def test_distribution_and_console_scripts_use_the_public_package(self) -> None:
        config = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))
        self.assertEqual(config['project']['name'], 'agent-trace-studio')
        expected = {
            'agent-trace-studio': 'agent_trace_studio.cli:main',
            'agent-trace-studio-hook': 'agent_trace_studio.live_hook:main',
            'codex-session-dashboard': 'agent_trace_studio.cli:main',
        }
        self.assertEqual(config['project']['scripts'], expected)
        for target in expected.values():
            module, attribute = target.split(':')
            self.assertTrue(callable(getattr(importlib.import_module(module), attribute)))
        packages = {entry.name for entry in (ROOT / 'src').iterdir() if (entry / '__init__.py').is_file()}
        self.assertEqual(packages, {'agent_trace_studio'})

    def test_report_and_agent_assets_are_available_as_package_resources(self) -> None:
        package = importlib.resources.files('agent_trace_studio')
        for relative in (
            'assets/dashboard.css',
            'assets/dashboard.js',
            'assets/codex_turn.mjs',
            'assets/codex_thread_worker.mjs',
            'assets/codex_review.mjs',
            'agent_skills/parser-repair/SKILL.md',
        ):
            with self.subTest(asset=relative):
                self.assertTrue(package.joinpath(relative).is_file())
                self.assertTrue(package.joinpath(relative).read_bytes())

    def test_module_entry_point_runs_without_a_trace_or_provider(self) -> None:
        environment = {**os.environ, 'PYTHONPATH': str(ROOT / 'src'), 'PYTHONDONTWRITEBYTECODE': '1'}
        result = subprocess.run(
            [sys.executable, '-m', 'agent_trace_studio', '--help'],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('agent-trace-studio', result.stdout)
        self.assertIn('--source-workspace', result.stdout)

    def test_source_discovery_and_supervisor_use_the_renamed_layout(self) -> None:
        self.assertTrue(_is_source_checkout(ROOT))
        spec = SupervisorLaunchSpec(
            child_args=(),
            output_dir=ROOT / 'dashboard',
            state_dir=ROOT / '.test-supervisor',
            agent_state_dir=ROOT / '.test-agent-state',
            live_state_dir=ROOT / '.test-live-state',
            source_workspace=ROOT,
            cwd=ROOT,
        )
        command = spec.command(generation='synthetic', port=8766, output_dir=ROOT / 'dashboard')
        self.assertEqual(command[:3], [str(spec.python), '-m', 'agent_trace_studio'])
