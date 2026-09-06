# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for the deterministic anti-pattern lint gate (SDLC Gap 3:
docs/ARCHITECTURE_REVIEW_ACTIONS.md, Tier 3; docs/adr/ADR-0005)."""
from app.agents.lint_gate import lint_files


def _file(path: str, content: str) -> dict:
    return {"path": path, "content": content}


class TestHardcodedSecret:
    def test_detects_hardcoded_api_key(self):
        out = lint_files([_file("a.py", 'api_key = "sk-abcdefghijklmnop"')])
        titles = [f["title"] for f in out["findings"]]
        assert "Possible hardcoded credential" in titles

    def test_ignores_short_values(self):
        out = lint_files([_file("a.py", 'api_key = "short"')])
        assert out["findings"] == []

    def test_case_insensitive_variable_name(self):
        out = lint_files([_file("a.py", 'API_KEY = "sk-abcdefghijklmnop"')])
        assert len(out["findings"]) == 1


class TestSelectStar:
    def test_detects_select_star(self):
        out = lint_files([_file("a.sql", "SELECT * FROM users WHERE id = 1")])
        titles = [f["title"] for f in out["findings"]]
        assert any("SELECT *" in t for t in titles)

    def test_does_not_flag_specific_columns(self):
        out = lint_files([_file("a.sql", "SELECT id, name FROM users")])
        assert out["findings"] == []


class TestBareExcept:
    def test_detects_bare_except(self):
        out = lint_files([_file("a.py", "try:\n    x()\nexcept:\n    pass\n")])
        titles = [f["title"] for f in out["findings"]]
        assert any("Bare except" in t for t in titles)

    def test_does_not_flag_typed_except(self):
        out = lint_files([_file("a.py", "try:\n    x()\nexcept ValueError:\n    pass\n")])
        assert out["findings"] == []


class TestRequestsNoTimeout:
    def test_detects_missing_timeout(self):
        out = lint_files([_file("a.py", 'resp = requests.get("http://example.com")')])
        titles = [f["title"] for f in out["findings"]]
        assert any("timeout" in t.lower() for t in titles)

    def test_does_not_flag_when_timeout_present(self):
        out = lint_files([_file("a.py", 'resp = requests.get("http://example.com", timeout=10)')])
        assert out["findings"] == []


class TestFindingShape:
    def test_findings_have_required_fields(self):
        out = lint_files([_file("a.py", 'api_key = "sk-abcdefghijklmnop"')])
        f = out["findings"][0]
        for key in ("severity", "category", "file", "line", "title", "detail", "suggested_fix"):
            assert key in f
        assert f["category"] == "anti_pattern"
        assert f["file"] == "a.py"

    def test_findings_carry_root_cause_and_principle_ref(self):
        out = lint_files([_file("a.py", 'api_key = "sk-abcdefghijklmnop"')])
        f = out["findings"][0]
        assert f["root_cause"]
        assert "EA_Skills.md" in f["principle_ref"]

    def test_line_number_is_correct(self):
        content = "line1\nline2\napi_key = \"sk-abcdefghijklmnop\"\nline4"
        out = lint_files([_file("a.py", content)])
        assert out["findings"][0]["line"] == 3

    def test_summary_reflects_finding_count(self):
        out = lint_files([_file("a.py", 'api_key = "sk-abcdefghijklmnop"')])
        assert "1 finding" in out["summary"]


class TestMultipleFilesAndFindings:
    def test_scans_all_files(self):
        out = lint_files([
            _file("a.py", 'api_key = "sk-abcdefghijklmnop"'),
            _file("b.py", "except:\n    pass"),
        ])
        files_with_findings = {f["file"] for f in out["findings"]}
        assert files_with_findings == {"a.py", "b.py"}

    def test_clean_files_produce_zero_findings(self):
        out = lint_files([_file("a.py", "x = 1\ny = 2\n")])
        assert out["findings"] == []
        assert "0 finding" in out["summary"]


class TestRobustness:
    def test_empty_file_list(self):
        out = lint_files([])
        assert out["findings"] == []

    def test_none_input_does_not_raise(self):
        out = lint_files(None)
        assert out["findings"] == []

    def test_file_with_missing_content_key(self):
        out = lint_files([{"path": "a.py"}])
        assert out["findings"] == []
