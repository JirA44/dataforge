from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class RepositoryReadinessTests(unittest.TestCase):
    def test_git_ready_files_and_ci_matrix_exist(self) -> None:
        workflow = (ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8")
        self.assertIn('"3.10"', workflow)
        self.assertIn('"3.12"', workflow)
        self.assertIn("python -m pytest -q", workflow)
        self.assertIn("1.0.7", workflow)

    def test_french_usage_examples_cover_v105_and_powershell(self) -> None:
        usage = (ROOT / "docs/USAGE_EXAMPLES.md").read_text(encoding="utf-8")
        self.assertIn("PowerShell", usage)
        self.assertIn("/v1/provenance-closure-reports", usage)
        self.assertIn("version_ids", usage)
        self.assertIn("qualification", usage)

    def test_gitignore_excludes_generated_and_secret_files(self) -> None:
        ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
        for pattern in (".venv/", "*.egg-info/", ".env", "*.sqlite3", ".pytest_cache/"):
            self.assertIn(pattern, ignored)


if __name__ == "__main__":
    unittest.main()
