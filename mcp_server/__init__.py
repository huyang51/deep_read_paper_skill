"""paper_kb_mcp — MCP server for the deep_read_paper_skill.

Exposes 9 tools for managing a personal academic paper knowledge base:
  - paper_search, paper_get, paper_find_related
  - paper_search_by_method, paper_index, paper_remove
  - paper_index_stats
  - cite_verify, paper_citations  (external fact-checking, see cite_api)

Storage layer: ChromaDB (vector) + YAML-frontmatter Markdown (file).

This file must stay import-free: `bootstrap.py` reads these constants on a
machine where none of the dependencies below exist yet, and pyproject.toml
reads __version__ before the package is installed.
"""
__version__ = "1.1.0"

# What the MCP server imports at startup. Single source on purpose — deploy.py
# probes these, bootstrap.py installs them, and both used to keep a private copy
# held together by a "keep in sync" comment.
SERVER_IMPORTS = ("chromadb", "frontmatter", "watchfiles", "pydantic")
# Needed only for non-default embedding models; ChromaDB's built-in ONNX
# embedder (deploy.ONNX_EMBEDDER) runs without any of it.
MODEL_IMPORTS = ("sentence_transformers",)
