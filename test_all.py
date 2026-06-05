#!/usr/bin/env python3
"""
test_all.py — Unit tests for llm-ontology-miner.

Run:
    python -m pytest test_all.py -v
    python -m unittest test_all -v
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent))


# ---------------------------------------------------------------------------
# providers.py
# ---------------------------------------------------------------------------

class TestFmtRequestError(unittest.TestCase):

    def setUp(self):
        from providers import _fmt_request_error
        import requests
        self.fmt = _fmt_request_error
        self.requests = requests

    def _http_error(self, status: int):
        import requests
        resp = MagicMock()
        resp.status_code = status
        return requests.HTTPError(response=resp)

    def test_localhost_connection_error_suggests_ollama(self):
        import requests
        msg = self.fmt(requests.ConnectionError("refused"), "http://localhost:11434")
        self.assertIn("is Ollama running?", msg)

    def test_remote_connection_error_no_ollama_hint(self):
        import requests
        msg = self.fmt(requests.ConnectionError("refused"), "https://api.openai.com")
        self.assertNotIn("Ollama", msg)
        self.assertIn("network", msg.lower())

    def test_http_401_suggests_api_key(self):
        msg = self.fmt(self._http_error(401), "Anthropic")
        self.assertIn("API key", msg)

    def test_http_403_suggests_api_key(self):
        msg = self.fmt(self._http_error(403), "Gemini")
        self.assertIn("authentication failed", msg.lower())

    def test_http_429_rate_limit(self):
        msg = self.fmt(self._http_error(429), "OpenAI")
        self.assertIn("rate limit", msg.lower())

    def test_http_500_includes_status(self):
        msg = self.fmt(self._http_error(500), "OpenAI")
        self.assertIn("500", msg)

    def test_timeout_message(self):
        import requests
        msg = self.fmt(requests.Timeout("timed out"), "Anthropic")
        self.assertIn("timed out", msg.lower())

    def test_generic_error_no_multiline(self):
        import requests
        msg = self.fmt(requests.RequestException("line1\nline2\nline3"), "endpoint")
        self.assertNotIn("\n", msg)


class TestValidateProvider(unittest.TestCase):

    def setUp(self):
        from providers import validate_provider
        self.validate = validate_provider

    def test_unknown_provider_exits(self):
        with self.assertRaises(SystemExit):
            self.validate("not-a-real-provider")

    def test_missing_api_key_exits(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(SystemExit):
                self.validate("anthropic")

    def test_valid_provider_returns_config(self):
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}):
            cfg = self.validate("anthropic")
            self.assertEqual(cfg["default_model"], "claude-sonnet-4-6")

    def test_ollama_needs_no_key(self):
        with patch.dict("os.environ", {}, clear=True):
            cfg = self.validate("ollama")
            self.assertIn("llama", cfg["default_model"])


class TestResolveProviderBase(unittest.TestCase):

    def setUp(self):
        from providers import resolve_provider_base
        self.resolve = resolve_provider_base

    def test_openai_url_and_key(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}):
            base, key = self.resolve("openai")
        self.assertEqual(base, "https://api.openai.com")
        self.assertEqual(key, "sk-test")

    def test_ollama_localhost_no_key(self):
        base, key = self.resolve("ollama")
        self.assertEqual(base, "http://localhost:11434")
        self.assertIsNone(key)

    def test_openai_compat_reads_env(self):
        env = {
            "OPENAI_COMPAT_BASE_URL": "https://my-endpoint.com",
            "OPENAI_COMPAT_API_KEY": "my-key",
        }
        with patch.dict("os.environ", env):
            base, key = self.resolve("openai-compat")
        self.assertEqual(base, "https://my-endpoint.com")
        self.assertEqual(key, "my-key")

    def test_unknown_provider_raises(self):
        with self.assertRaises(KeyError):
            self.resolve("unknown-provider")


class TestCallOpenaiCompatPlain(unittest.TestCase):

    def setUp(self):
        from providers import _call_openai_compat_plain
        self.call = _call_openai_compat_plain

    def _mock_response(self, payload: dict):
        r = MagicMock()
        r.json.return_value = payload
        return r

    def test_success_returns_content(self):
        r = self._mock_response({"choices": [{"message": {"content": "hello"}}]})
        with patch("providers.requests.post", return_value=r):
            self.assertEqual(self.call("http://localhost:11434", None, "sys", "usr", "llama3.2"), "hello")

    def test_empty_choices_returns_empty_string(self):
        r = self._mock_response({"choices": []})
        with patch("providers.requests.post", return_value=r):
            self.assertEqual(self.call("http://localhost:11434", None, "sys", "usr", "llama3.2"), "")

    def test_api_error_response_no_choices_returns_empty_string(self):
        r = self._mock_response({"error": {"message": "quota exceeded"}})
        with patch("providers.requests.post", return_value=r):
            self.assertEqual(self.call("http://localhost:11434", None, "sys", "usr", "llama3.2"), "")

    def test_connection_error_raises_runtime_error_with_ollama_hint(self):
        import requests
        with patch("providers.requests.post", side_effect=requests.ConnectionError("refused")):
            with self.assertRaises(RuntimeError) as ctx:
                self.call("http://localhost:11434", None, "sys", "usr", "llama3.2")
        self.assertIn("Ollama", str(ctx.exception))


# ---------------------------------------------------------------------------
# ontology_miner.py
# ---------------------------------------------------------------------------

class TestMinerExtractJson(unittest.TestCase):

    def setUp(self):
        from ontology_miner import extract_json
        self.extract = extract_json

    def test_plain_json_array(self):
        raw = '[{"term": "leaf rolling", "ontology": "TO"}]'
        result = self.extract(raw)
        self.assertEqual(result[0]["term"], "leaf rolling")

    def test_json_embedded_in_prose(self):
        raw = 'Here are the results:\n[{"term": "root cap"}]\nEnd.'
        self.assertEqual(self.extract(raw)[0]["term"], "root cap")

    def test_fenced_code_block(self):
        raw = '```json\n[{"term": "coleoptile"}]\n```'
        self.assertEqual(self.extract(raw)[0]["term"], "coleoptile")

    def test_no_json_returns_none(self):
        self.assertIsNone(self.extract("No candidates were found in this abstract."))

    def test_malformed_json_returns_none(self):
        self.assertIsNone(self.extract('[{"term": "broken"'))

    def test_empty_array(self):
        self.assertEqual(self.extract("[]"), [])


class TestMinerSlugify(unittest.TestCase):

    def setUp(self):
        from ontology_miner import slugify
        self.slugify = slugify

    def test_doi_becomes_slug(self):
        self.assertEqual(self.slugify("10.1093/jxb/eraa002"), "10-1093-jxb-eraa002")

    def test_lowercase(self):
        self.assertEqual(self.slugify("LeafRolling"), "leafrolling")

    def test_no_leading_trailing_dashes(self):
        s = self.slugify("  hello world  ")
        self.assertFalse(s.startswith("-"))
        self.assertFalse(s.endswith("-"))

    def test_max_40_chars(self):
        self.assertLessEqual(len(self.slugify("a" * 100)), 40)


class TestMinerFetchByDoi(unittest.TestCase):

    def setUp(self):
        from ontology_miner import fetch_by_doi
        self.fetch = fetch_by_doi

    def _crossref_payload(self, abstract="Test abstract text."):
        return {"message": {
            "title": ["Drying times: plant water use efficiency"],
            "author": [{"given": "Jane", "family": "Doe"}],
            "container-title": ["Plant Physiology"],
            "published": {"date-parts": [[2024]]},
            "abstract": abstract,
        }}

    def test_success_returns_pub_dict(self):
        r = MagicMock()
        r.json.return_value = self._crossref_payload()
        with patch("ontology_miner.requests.get", return_value=r):
            pub = self.fetch("10.xxxx/test")
        self.assertEqual(pub["source"], "doi")
        self.assertIn("Drying times", pub["title"])
        self.assertIn("Test abstract", pub["text"])

    def test_no_abstract_falls_back_to_europepmc(self):
        crossref_r = MagicMock()
        crossref_r.json.return_value = self._crossref_payload(abstract="")
        pmc_r = MagicMock()
        pmc_r.ok = True
        pmc_r.json.return_value = {"resultList": {"result": [{"abstractText": "PMC abstract"}]}}
        with patch("ontology_miner.requests.get", side_effect=[crossref_r, pmc_r]):
            pub = self.fetch("10.xxxx/test")
        self.assertIn("PMC abstract", pub["text"])

    def test_http_429_exits_cleanly(self):
        import requests
        r = MagicMock()
        r.raise_for_status.side_effect = requests.HTTPError(response=MagicMock(status_code=429))
        with patch("ontology_miner.requests.get", return_value=r):
            with self.assertRaises(SystemExit):
                self.fetch("10.xxxx/test")

    def test_connection_error_exits_cleanly(self):
        import requests
        with patch("ontology_miner.requests.get", side_effect=requests.ConnectionError("refused")):
            with self.assertRaises(SystemExit):
                self.fetch("10.xxxx/test")


# ---------------------------------------------------------------------------
# ontology_agent.py
# ---------------------------------------------------------------------------

class TestAgentExtractJson(unittest.TestCase):

    def setUp(self):
        from ontology_agent import extract_json
        self.extract = extract_json

    def test_valid_returns_list(self):
        self.assertEqual(self.extract('[{"term": "coleoptile zone"}]')[0]["term"], "coleoptile zone")

    def test_fenced_block(self):
        self.assertEqual(self.extract('```\n[{"term": "root tip"}]\n```')[0]["term"], "root tip")

    def test_invalid_returns_none(self):
        self.assertIsNone(self.extract("no json here"))

    def test_empty_array(self):
        self.assertEqual(self.extract("[]"), [])


class TestAgentSlugify(unittest.TestCase):

    def setUp(self):
        from ontology_agent import slugify
        self.slugify = slugify

    def test_doi(self):
        self.assertEqual(self.slugify("10.1234/abc"), "10-1234-abc")

    def test_max_length(self):
        self.assertLessEqual(len(self.slugify("x" * 100)), 40)


class TestSearchOntology(unittest.TestCase):
    """Regression tests for Fix 3: network errors must return found=None, not found=False."""

    def setUp(self):
        from ontology_agent import search_ontology
        self.search = search_ontology

    def test_network_error_returns_found_none(self):
        import requests
        with patch("ontology_agent.requests.get", side_effect=requests.ConnectionError("down")):
            result = self.search("leaf rolling", "to")
        self.assertIsNone(result["found"], "network error must return found=None, not False")
        self.assertIn("error", result)

    def test_ols_429_returns_found_none(self):
        r = MagicMock()
        r.status_code = 429
        r.ok = False
        with patch("ontology_agent.requests.get", return_value=r):
            result = self.search("leaf rolling", "to")
        self.assertIsNone(result["found"])

    def test_term_found_returns_found_true(self):
        r = MagicMock()
        r.status_code = 200
        r.ok = True
        r.json.return_value = {"response": {"docs": [
            {"obo_id": "TO:0000123", "label": "leaf rolling", "ontology_name": "to", "description": ["desc"]}
        ]}}
        with patch("ontology_agent.requests.get", return_value=r):
            result = self.search("leaf rolling", "to")
        self.assertTrue(result["found"])
        self.assertEqual(result["matches"][0]["id"], "TO:0000123")

    def test_term_not_found_returns_found_false(self):
        r = MagicMock()
        r.status_code = 200
        r.ok = True
        r.json.return_value = {"response": {"docs": []}}
        with patch("ontology_agent.requests.get", return_value=r):
            result = self.search("xyzzy nonexistent term", "po")
        self.assertFalse(result["found"])


class TestExecuteTool(unittest.TestCase):

    def setUp(self):
        from ontology_agent import execute_tool
        self.execute = execute_tool

    def test_unknown_tool_returns_error(self):
        result = self.execute("not_a_real_tool", {})
        self.assertIn("error", result)

    def test_search_ontology_delegates(self):
        with patch("ontology_agent.search_ontology", return_value={"found": False}) as mock:
            self.execute("search_ontology", {"term": "leaf rolling", "ontology": "to"})
            mock.assert_called_once_with("leaf rolling", "to")


# ---------------------------------------------------------------------------
# export_candidates.py
# ---------------------------------------------------------------------------

class TestTermPrefix(unittest.TestCase):

    def setUp(self):
        from export_candidates import term_prefix
        self.prefix = term_prefix

    def test_plant_anatomy(self):
        self.assertEqual(self.prefix({"namespace": "plant_anatomy"}), "PO")

    def test_plant_morphology(self):
        self.assertEqual(self.prefix({"namespace": "plant_morphology"}), "PO")

    def test_developmental_stage(self):
        self.assertEqual(self.prefix({"namespace": "plant_developmental_stage"}), "PO")

    def test_trait_namespace(self):
        self.assertEqual(self.prefix({"namespace": "trait"}), "TO")

    def test_ontology_field_to(self):
        self.assertEqual(self.prefix({"ontology": "TO"}), "TO")

    def test_ontology_field_po(self):
        self.assertEqual(self.prefix({"ontology": "PO"}), "PO")

    def test_unknown_returns_none(self):
        self.assertIsNone(self.prefix({}))

    def test_either_returns_none(self):
        self.assertIsNone(self.prefix({"ontology": "either"}))


class TestPlaceholderId(unittest.TestCase):

    def setUp(self):
        from export_candidates import placeholder_id
        self.pid = placeholder_id

    def test_po_prefix(self):
        self.assertTrue(self.pid({"namespace": "plant_anatomy"}, 0).startswith("PO:NEWTERM_"))

    def test_to_prefix(self):
        self.assertTrue(self.pid({"namespace": "trait"}, 0).startswith("TO:NEWTERM_"))

    def test_unknown_prefix(self):
        self.assertTrue(self.pid({}, 0).startswith("??:NEWTERM_"))

    def test_zero_padded_first(self):
        self.assertIn("001", self.pid({"namespace": "plant_anatomy"}, 0))

    def test_zero_padded_third(self):
        self.assertIn("003", self.pid({"namespace": "plant_anatomy"}, 2))


_SAMPLE_CANDIDATE = {
    "term": "leaf rolling",
    "ontology": "TO",
    "namespace": "trait",
    "definition_draft": "A trait where leaves curl inward under drought stress.",
    "suggested_parent": "leaf trait",
    "synonyms": ["leaf curl", "leaf inrolling"],
    "source_sentence": "Leaf rolling was observed in drought-stressed maize plants.",
    "rationale": "Consistently used term not currently in TO.",
    "confidence": "high",
}


class TestBuildObo(unittest.TestCase):

    def setUp(self):
        from export_candidates import build_obo
        self.build = build_obo

    def test_contains_term_block(self):
        out = self.build([_SAMPLE_CANDIDATE], "10.x/test", "Test Paper", "2026-06-05", Path("test.json"))
        self.assertIn("[Term]", out)
        self.assertIn("leaf rolling", out)

    def test_definition_present(self):
        out = self.build([_SAMPLE_CANDIDATE], "10.x/test", "Test Paper", "2026-06-05", Path("test.json"))
        self.assertIn("def:", out)
        self.assertIn("drought stress", out)

    def test_synonyms_present(self):
        out = self.build([_SAMPLE_CANDIDATE], None, None, "2026-06-05", Path("test.json"))
        self.assertIn('synonym: "leaf curl"', out)

    def test_placeholder_id(self):
        out = self.build([_SAMPLE_CANDIDATE], None, None, "2026-06-05", Path("test.json"))
        self.assertIn("TO:NEWTERM_001", out)

    def test_multiple_terms(self):
        out = self.build([_SAMPLE_CANDIDATE, _SAMPLE_CANDIDATE], None, None, "2026-06-05", Path("test.json"))
        self.assertEqual(out.count("[Term]"), 2)


class TestBuildRobot(unittest.TestCase):

    def setUp(self):
        from export_candidates import build_robot
        self.build = build_robot

    def test_tsv_has_header_rows_plus_data(self):
        lines = self.build([_SAMPLE_CANDIDATE], "10.x/test").strip().splitlines()
        self.assertGreaterEqual(len(lines), 3)  # header + robot-header + 1 data row

    def test_tab_separated(self):
        line = self.build([_SAMPLE_CANDIDATE], None).splitlines()[2]
        self.assertGreater(line.count("\t"), 3)

    def test_term_in_output(self):
        self.assertIn("leaf rolling", self.build([_SAMPLE_CANDIDATE], None))

    def test_synonyms_pipe_separated(self):
        out = self.build([_SAMPLE_CANDIDATE], None)
        self.assertIn("leaf curl|leaf inrolling", out)


class TestBuildCsv(unittest.TestCase):

    def setUp(self):
        from export_candidates import build_csv
        self.build = build_csv

    def test_has_header_and_data(self):
        lines = self.build([_SAMPLE_CANDIDATE], "10.x/test").strip().splitlines()
        self.assertGreaterEqual(len(lines), 2)

    def test_term_in_output(self):
        self.assertIn("leaf rolling", self.build([_SAMPLE_CANDIDATE], None))

    def test_doi_in_output(self):
        self.assertIn("10.x/test", self.build([_SAMPLE_CANDIDATE], "10.x/test"))


class TestBuildGithubIssue(unittest.TestCase):

    def setUp(self):
        from export_candidates import build_github_issue
        self.build = build_github_issue

    def test_po_issue_links_to_planteome(self):
        c = {**_SAMPLE_CANDIDATE, "ontology": "PO", "namespace": "plant_anatomy"}
        self.assertIn("Planteome/plant-ontology", self.build(c, "10.x/test", "Test"))

    def test_to_issue_links_to_trait_ontology(self):
        self.assertIn("Planteome/plant-trait-ontology", self.build(_SAMPLE_CANDIDATE, "10.x/test", "Test"))

    def test_ambiguous_ontology_warns_curator(self):
        c = {**_SAMPLE_CANDIDATE, "ontology": "either"}
        self.assertIn("Ambiguous", self.build(c, None, None))

    def test_source_sentence_quoted(self):
        out = self.build(_SAMPLE_CANDIDATE, None, None)
        self.assertIn("Leaf rolling was observed", out)

    def test_missing_doi_no_crash(self):
        out = self.build(_SAMPLE_CANDIDATE, None, None)
        self.assertIsInstance(out, str)


# ---------------------------------------------------------------------------
# journal_watcher.py
# ---------------------------------------------------------------------------

class TestHistoryHelpers(unittest.TestCase):

    def setUp(self):
        from journal_watcher import is_processed, is_blocked, mark_processed, mark_failed
        self.is_processed  = is_processed
        self.is_blocked    = is_blocked
        self.mark_processed = mark_processed
        self.mark_failed   = mark_failed
        self.h = {"last_scan": None, "processed_dois": {}, "failed_dois": {}}

    def test_new_doi_not_processed(self):
        self.assertFalse(self.is_processed(self.h, "10.xxxx/new"))

    def test_mark_processed(self):
        self.mark_processed(self.h, "10.xxxx/test")
        self.assertTrue(self.is_processed(self.h, "10.xxxx/test"))

    def test_doi_lookup_is_case_insensitive(self):
        self.mark_processed(self.h, "10.XXXX/TEST")
        self.assertTrue(self.is_processed(self.h, "10.xxxx/test"))

    def test_not_blocked_initially(self):
        self.assertFalse(self.is_blocked(self.h, "10.xxxx/new"))

    def test_blocked_after_max_failures(self):
        for _ in range(3):
            self.mark_failed(self.h, "10.xxxx/bad")
        self.assertTrue(self.is_blocked(self.h, "10.xxxx/bad"))

    def test_not_blocked_before_max_failures(self):
        for _ in range(2):
            self.mark_failed(self.h, "10.xxxx/retry")
        self.assertFalse(self.is_blocked(self.h, "10.xxxx/retry"))

    def test_failure_count_accumulates(self):
        self.mark_failed(self.h, "10.xxxx/doi")
        self.mark_failed(self.h, "10.xxxx/doi")
        self.assertEqual(self.h["failed_dois"]["10.xxxx/doi"], 2)


class TestLoadSaveHistory(unittest.TestCase):

    def setUp(self):
        from journal_watcher import load_history, save_history
        self.load = load_history
        self.save = save_history

    def test_missing_file_returns_empty(self):
        with patch("journal_watcher.HISTORY_FILE", Path("/nonexistent/path/history.json")):
            h = self.load()
        self.assertEqual(h["processed_dois"], {})
        self.assertEqual(h["failed_dois"], {})

    def test_malformed_json_returns_empty(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("not valid json {{{")
            tmp = Path(f.name)
        try:
            with patch("journal_watcher.HISTORY_FILE", tmp):
                h = self.load()
            self.assertEqual(h["processed_dois"], {})
        finally:
            tmp.unlink()

    def test_old_history_without_failed_dois_gets_default(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"last_scan": "2026-01-01", "processed_dois": {"10.x/1": "ts"}}, f)
            tmp = Path(f.name)
        try:
            with patch("journal_watcher.HISTORY_FILE", tmp):
                h = self.load()
            self.assertIn("failed_dois", h)
            self.assertEqual(h["failed_dois"], {})
        finally:
            tmp.unlink()

    def test_roundtrip_save_then_load(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d) / "history.json"
            with patch("journal_watcher.HISTORY_FILE", tmp):
                original = {"last_scan": "2026-06-05", "processed_dois": {"10.x/1": "ts"}, "failed_dois": {}}
                self.save(original)
                loaded = self.load()
            self.assertEqual(loaded["processed_dois"]["10.x/1"], "ts")
            self.assertEqual(loaded["last_scan"], "2026-06-05")


if __name__ == "__main__":
    unittest.main(verbosity=2)
