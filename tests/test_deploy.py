"""Offline tests for deploy.py's interpreter preflight.

The failure this guards against is silent: deploy renders a valid-looking
config for an interpreter that cannot import the server's dependencies, and
then the MCP server *and* both hooks die at startup with the traceback buried
in a log nobody reads. It happened for real — the vault pointed at an
interpreter without torch while `embedding_model` asked for a
SentenceTransformer model.

Run from repo root:  python -m unittest discover -s tests -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import deploy  # noqa: E402

BASE_MODULES = {"chromadb", "frontmatter", "watchfiles", "pydantic"}


class ProbePythonTest(unittest.TestCase):
    def test_missing_interpreter_reports_every_module(self):
        report = deploy.probe_python("D:/definitely/not/here/python.exe",
                                     "paraphrase-multilingual-MiniLM-L12-v2")
        self.assertEqual(set(report), BASE_MODULES | {"sentence_transformers"})
        self.assertTrue(all(v != "ok" for v in report.values()), report)

    def test_onnx_model_does_not_require_sentence_transformers(self):
        """all-MiniLM-L6-v2 goes through ChromaDB's bundled ONNX embedder, so
        probing it must not demand torch — that is exactly what keeps a
        torch-free install viable (and the multilingual default is what makes
        an install without torch a deploy-time warning instead of a mystery)."""
        report = deploy.probe_python(sys.executable, "all-MiniLM-L6-v2")
        self.assertNotIn("sentence_transformers", report)
        self.assertTrue(all(v == "ok" for v in report.values()), report)


if __name__ == "__main__":
    unittest.main()
