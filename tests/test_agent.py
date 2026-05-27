import unittest
import tempfile
from pathlib import Path

from pop_agent.agent import (
    State,
    Task,
    clean_pop_marker,
    deploy_matches_sha,
    describe_netlify_deploy,
    extract_response_markers,
    has_pop_marker,
    netlify_site_matches,
    python_checks_discovered,
)


class MarkerTests(unittest.TestCase):
    def test_detects_pop_marker(self):
        self.assertTrue(has_pop_marker("please do this\n\n#pop\n"))
        self.assertTrue(has_pop_marker("ship it #POP"))

    def test_does_not_detect_embedded_marker(self):
        self.assertFalse(has_pop_marker("this is #popular"))
        self.assertFalse(has_pop_marker("this is not tagged"))

    def test_clean_pop_marker(self):
        self.assertEqual(clean_pop_marker("Do it\n#pop\n"), "Do it")


class ResponseMarkerTests(unittest.TestCase):
    def test_extracts_processed_comment_ids(self):
        comments = [
            {"body": "<!-- pop-agent:source-comment-id=123 -->\nDone"},
            {"body": "nothing"},
            {"body": "<!-- pop-agent:source-comment-id=456 -->"},
        ]
        self.assertEqual(extract_response_markers(comments), {123, 456})


class StateTests(unittest.TestCase):
    def test_in_memory_shape(self):
        state = State.__new__(State)
        state.path = None
        state.data = {"schema_version": 1, "processed": {"1": {"status": "success"}}}
        self.assertTrue(state.is_processed(1))
        self.assertFalse(state.is_processed(2))


class TaskTests(unittest.TestCase):
    def test_task_spec_removes_marker(self):
        task = Task(
            repo="jmtdev0/example",
            default_branch="main",
            ssh_url="git@github.com:jmtdev0/example.git",
            issue_number=1,
            issue_title="Issue",
            comment_id=42,
            comment_url="https://example.invalid",
            comment_created_at="2026-01-01T00:00:00Z",
            comment_updated_at="2026-01-01T00:00:00Z",
            body="Implement X\n\n#pop",
        )
        self.assertEqual(task.spec, "Implement X")


class DiscoveryTests(unittest.TestCase):
    def test_agent_response_comments_are_not_tasks(self):
        # Regression guard for the agent mentioning #pop in its own replies.
        body = "<!-- pop-agent:source-comment-id=123 -->\nDone with this `#pop`."
        self.assertTrue(has_pop_marker(body))
        self.assertEqual(extract_response_markers([{"body": body}]), {123})


class PythonCheckDiscoveryTests(unittest.TestCase):
    def test_ignores_non_python_tests_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tests").mkdir()
            (root / "tests" / "app.spec.ts").write_text("test('works')\n", encoding="utf-8")

            self.assertFalse(python_checks_discovered(root))

    def test_detects_python_test_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tests").mkdir()
            (root / "tests" / "test_app.py").write_text("def test_works():\n    assert True\n", encoding="utf-8")

            self.assertTrue(python_checks_discovered(root))


class NetlifyTests(unittest.TestCase):
    def task(self):
        return Task(
            repo="jmtdev0/example",
            default_branch="main",
            ssh_url="git@github.com:jmtdev0/example.git",
            issue_number=1,
            issue_title="Issue",
            comment_id=42,
            comment_url="https://example.invalid",
            comment_created_at="2026-01-01T00:00:00Z",
            comment_updated_at="2026-01-01T00:00:00Z",
            body="Implement X\n\n#pop",
        )

    def test_netlify_site_matches_repo_and_branch(self):
        site = {
            "build_settings": {
                "repo_path": "jmtdev0/example",
                "repo_branch": "main",
            }
        }

        self.assertTrue(netlify_site_matches(self.task(), site))

    def test_netlify_site_rejects_wrong_branch(self):
        site = {
            "build_settings": {
                "repo_path": "jmtdev0/example",
                "repo_branch": "develop",
            }
        }

        self.assertFalse(netlify_site_matches(self.task(), site))

    def test_deploy_matches_commit_sha(self):
        sha = "abc123def456"
        self.assertTrue(deploy_matches_sha({"commit_ref": sha}, sha))
        self.assertTrue(deploy_matches_sha({"commit_url": f"https://github.com/x/y/commit/{sha}"}, sha))

    def test_describes_netlify_deploy(self):
        deploy = {
            "state": "ready",
            "links": {"permalink": "https://deploy.example"},
        }

        self.assertEqual(describe_netlify_deploy(deploy), "ready: https://deploy.example")


if __name__ == "__main__":
    unittest.main()
