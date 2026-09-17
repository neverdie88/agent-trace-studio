from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


class DashboardConversationStateTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node.js is required for backend UI tests')
    def test_selected_backend_authentication_and_removed_selection_ui(self) -> None:
        completed = subprocess.run(
            [shutil.which('node') or 'node', str(Path(__file__).with_name('agent_backend_ui.test.cjs'))],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)

    @unittest.skipUnless(shutil.which('node'), 'Node.js is required for dashboard surface tests')
    def test_chat_layout_and_source_dialog_lifecycle(self) -> None:
        script = Path(__file__).with_name('dashboard_surfaces.test.cjs')
        completed = subprocess.run(
            [shutil.which('node') or 'node', str(script)],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)

    @unittest.skipUnless(shutil.which('node'), 'Node.js is required for dashboard summary action tests')
    def test_summary_action_uses_the_studio_conversation(self) -> None:
        script = Path(__file__).with_name('qa_summary_action.test.cjs')
        completed = subprocess.run(
            [shutil.which('node') or 'node', str(script)],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)

    @unittest.skipUnless(shutil.which('node'), 'Node.js is required for dashboard prompt menu tests')
    def test_floating_prompt_menu_interactions_and_draft_safety(self) -> None:
        script = Path(__file__).with_name('qa_prompt_menu.test.cjs')
        completed = subprocess.run(
            [shutil.which('node') or 'node', str(script)],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)

    @unittest.skipUnless(shutil.which('node'), 'Node.js is required for dashboard layout tests')
    def test_session_brief_expansion_preserves_sizing_and_saved_layout(self) -> None:
        script = Path(__file__).with_name('floating_panel_layout.test.cjs')
        completed = subprocess.run(
            [shutil.which('node') or 'node', str(script)],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)

    @unittest.skipUnless(shutil.which('node'), 'Node.js is required for dashboard activity tests')
    def test_activity_details_render_safely_and_preserve_expansion(self) -> None:
        script = Path(__file__).with_name('qa_activity_render.test.cjs')
        completed = subprocess.run(
            [shutil.which('node') or 'node', str(script)],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)

    @unittest.skipUnless(shutil.which('node'), 'Node.js is required for dashboard lifecycle tests')
    def test_workflow_completion_reload_and_authorization_contracts(self) -> None:
        script = Path(__file__).with_name('qa_conversation_state.test.cjs')
        completed = subprocess.run(
            [shutil.which('node') or 'node', str(script)],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
