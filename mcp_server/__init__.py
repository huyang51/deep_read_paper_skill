"""paper_kb_mcp — MCP server for the deep_read_paper_skill.

Exposes 7 tools for managing a personal academic paper knowledge base:
  - paper_search, paper_get, paper_find_related
  - paper_search_by_method, paper_index, paper_remove
  - paper_index_stats

Storage layer: ChromaDB (vector) + YAML-frontmatter Markdown (file).
"""
__version__ = "1.1.0"
