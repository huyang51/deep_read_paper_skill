<p align="center">
  <h1 align="center">📖 Deep Read Paper Skill</h1>
  <p align="center">
    A <a href="https://claude.ai/claude-code">Claude Code</a> Skill for deep academic paper reading with persistent knowledge management.
    <br/>
    <strong>Read once. Remember forever. Discover connections.</strong>
  </p>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.10+-blue.svg" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/Claude%20Code-compatible-green.svg" alt="Claude Code compatible">
  <img src="https://img.shields.io/badge/license-MIT-purple.svg" alt="License: MIT">
  <img src="https://img.shields.io/badge/status-stable-brightgreen.svg" alt="Status: stable">
</p>

---

## What Is This?

**Deep Read Paper Skill** transforms Claude Code into a **personal AI research assistant** that reads, analyzes, and remembers academic papers. It's not just a PDF summarizer — it's a complete paper knowledge management system:

- 📄 **Read** any academic paper PDF page-by-page (never skips content)
- 🧠 **Analyze** papers across 5 dimensions: problem genealogy, method lineage, intuitive interpretation, experiment design, and limitations
- 📝 **Generate** structured Chinese-language interpretation reports with LaTeX formulas, data tables, and claim-evidence mapping
- 💾 **Remember** papers in an Obsidian-compatible knowledge vault with YAML frontmatter, wikilinks, and ChromaDB vector embeddings
- 🔗 **Connect** papers across your knowledge base — automatically discovers methodological, topical, and complementary relationships
- 💡 **Innovate** by proposing cross-paper research directions with concrete technical feasibility analysis

> **TL;DR**: Just point to a PDF and say "read this paper." The skill handles everything else — from extraction to analysis to long-term memory.

---

## Why This Exists

| Problem | Solution |
|---------|----------|
| Reading papers is time-consuming; you forget details days later | Structured "2-deliverable" output: detailed report + persistent memory entry |
| Papers exist in isolation; it's hard to see the bigger picture | Cross-paper linking with knowledge graph visualization in Obsidian |
| LLM summaries are shallow and miss nuance | 5-dimension deep analysis: problem → methods → experiments → limitations |
| Knowledge lost when you switch projects | Portable Obsidian vault that works independently of Claude Code |
| Can't find that one paper you read 3 months ago | ChromaDB semantic search + MCP tools for querying by keyword, method, or domain |

---

## How It Works

```mermaid
graph TD
    A[User: "Read this paper" + PDF] --> B[Phase 1: PDF Extraction]
    B --> C[Phase 2: 5-Dimension Analysis]
    C --> D[Phase 3: Report Generation]
    C --> E[Phase 4: Memory Entry]
    D --> F{Existing papers?}
    E --> F
    F -->|Yes| G[Phase 5: Cross-Paper Comparison]
    F -->|No| H[Done]
    G --> I[Insight File + Backlinks]

    style A fill:#e1f5fe
    style H fill:#c8e6c9
    style I fill:#fff9c4
```

### The 5 Analysis Dimensions

| # | Dimension | Core Questions Answered |
|---|-----------|------------------------|
| 1 | **Problem Genealogy** | What problem did the authors find? Why couldn't prior work solve it? What insight unlocked the solution? |
| 2 | **Method Genealogy** | Original or derived? Base methods? How changed and why? Technical difficulty of the change? |
| 3 | **Intuitive Interpretation** | Plain-language explanation with analogies. Input/output specifications. Key formula interpretation. |
| 4 | **Experiment Analysis** | Claim-evidence mapping. Baseline rationale. Key result interpretation. Reproducibility assessment. |
| 5 | **Limitation Analysis** | Method scope, experiment gaps, theoretical guarantees. Honest improvement directions. |

---

## Quick Start

### Prerequisites

- **Claude Code** (with skills enabled)
- **Python 3.10+**
- **Obsidian** (optional — for graph visualization)
- **PyMuPDF** works on Linux/macOS/Windows

### Installation

```bash
# 1. Clone and install
git clone https://github.com/YOUR_USER/deep_read_paper_skill.git
cd deep_read_paper_skill
pip install -r requirements.txt

# 2. Edit ONE file: settings.json
# Fill in vault_dir, project_dir, python_cmd (3 required fields)
# See configuration guide below

# 3. Deploy to your project
python setup.py

# 4. (Optional) Initialize Obsidian vault
cp -r vault-template/ /your/knowledge-base/path/

# 5. Restart Claude Code
```

### Configuration (`settings.json`)

```json
{
  "vault_dir": "D:/my-papers/knowledge-base",
  "project_dir": "D:/my-papers",
  "python_cmd": "D:/Anaconda/python.exe",
  "embedding_model": "all-MiniLM-L6-v2"
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `vault_dir` | ✅ | Where paper reports, memory entries, and ChromaDB index are stored |
| `project_dir` | ✅ | Your Claude Code project root — `setup.py` auto-deploys config here |
| `python_cmd` | ✅ | Python interpreter path (absolute recommended on Windows) |
| `embedding_model` | No | For Chinese papers, use `paraphrase-multilingual-MiniLM-L12-v2` |
| `trigger_keywords_cn/en` | No | Customize auto-trigger keywords (defaults cover common patterns) |

> **Path format**: Use forward slashes `/` even on Windows (e.g., `D:/path/to/dir`).
>
> **Environment variable override**: Set `PAPER_KB_VAULT_DIR` to override `vault_dir`. Useful when sharing one skill installation across multiple projects. Env var takes priority over `settings.json`.

---

## Usage

### Reading a Paper

Just point Claude Code to a PDF:

```
Read this paper: "D:/papers/SayPlan - 2023 - Grounding LLMs using 3D Scene Graphs.pdf"
```

The skill automatically:
1. Extracts all pages via PyMuPDF (never skips — even appendices)
2. Performs 5-dimension deep analysis
3. Generates a Chinese interpretation report → `reports/<short_name>_解读报告.md`
4. Creates a structured memory entry → `papers/<short_name>.md`
5. Indexes into ChromaDB for semantic search
6. If your vault has related papers, runs cross-paper comparison and creates insight files

### Searching Your Knowledge Base

Ask Claude Code questions directly:

```
"What papers in my knowledge base are about 3D scene graphs?"
"Search for papers related to retrieval-augmented generation"
"Compare the SayPlan and EmbodiedRAG papers"
"Find all papers from CVPR about robot task planning"
```

Available MCP tools powering these queries:

| Tool | Description |
|------|-------------|
| `paper_search` | Semantic search via ChromaDB embeddings (supports Chinese & English) |
| `paper_get` | Retrieve full paper details by ID |
| `paper_find_related` | Find papers with methodological/topical/complementary relationships |
| `paper_search_by_method` | Filter by method category |
| `paper_index_stats` | Get knowledge base statistics (total papers, domains, methods) |

### Viewing Your Knowledge Graph

Open the vault directory in Obsidian:
- **Graph View** (`Ctrl+G`): See papers as nodes connected by wikilinks (arrows show academic influence: old → new)
- **Properties Panel**: Browse YAML frontmatter fields (keywords, venue, authors)
- **Dataview Plugin**: `index.md` provides a dynamic sortable table of all papers

---

## Architecture

```
deep_read_paper_skill/
├── SKILL.md                     # Skill definition (Claude Code reads this)
├── settings.json                # ⭐ The ONLY file you need to edit
├── setup.py                     # One-click deployment to your project
├── requirements.txt             # Python deps (chromadb, pymupdf, watchfiles, pydantic)
│
├── mcp_server/                  # MCP Server (ChromaDB vector index + 7 tools)
│   ├── server.py                #   JSON-RPC main loop + tool dispatch
│   ├── chroma_store.py          #   Vector index management (create, search, update, delete)
│   ├── markdown_parser.py       #   YAML frontmatter parser + wikilink backlink auto-creation
│   ├── cross_refs.py            #   Cross-paper relationship discovery
│   ├── config.py                #   Reads settings.json
│   └── models.py                #   Pydantic input/output models for all tools
│
├── hooks/                       # Claude Code Hooks
│   ├── session_start.py         #   Injects recent paper summaries on new sessions
│   └── user_prompt_submit.py    #   Detects paper-related keywords → triggers search hints
│
├── tools/
│   └── index_paper.py           #   CLI for indexing papers (avoids Windows pipe encoding issues)
│
├── vault-template/              # Obsidian vault starter kit
│   ├── .obsidian/               #   Pre-configured graph view + properties + Dataview
│   ├── index.md                 #   Dataview-powered dynamic index
│   └── templates/               #   Paper memory and insight entry templates
│
├── templates/                   # Project config templates (rendered by setup.py)
│   ├── .mcp.json
│   └── .claude-settings.json
│
├── references/                  # Report/memory entry templates used by Phase 3-4
│   ├── report_template.md
│   └── memory_entry_template.md
│
└── output/                      # setup.py output (auto-deployed or manually copied)
```

### Vault Structure (Generated User Data)

```
<vault_dir>/
├── papers/          # Structured paper summaries (.md with YAML frontmatter + wikilinks)
├── reports/         # Full Chinese interpretation reports
├── insights/        # Cross-paper innovation insights (auto-generated)
├── index.md         # Dataview dynamic index table
└── .chromadb/       # Vector database (auto-managed by MCP server)
```

### Knowledge Graph Conventions

- **Arrows**: Old paper → New paper (academic influence flow)
- **Forward references** (new paper body): Use **bold text** (`**SayPlan**`), NOT wikilinks
- **Backlinks** (old paper body): System auto-creates `## 后续引用` section with `[[wikilink]]`
- **Unindexed papers**: Also use bold text — prevents ghost nodes in the graph

---

## Example: Interpretations Generated

This skill has been used to build a knowledge base covering:

| Domain | Papers | Connected Through |
|--------|--------|-------------------|
| 3D Scene Graph + LLM Planning | SayPlan (CoRL 2023), EmbodiedRAG (2024), Open3DSG (CVPR 2024), Text-Scene (2025), BrainBody-LLM (2025) | Method genealogy: 3DSG construction → retrieval → planning. Cross-paper insight files in `insights/` |
| Multimodal Retrieval | FLMR, PreFLMR, ReT, UniIR, AgentKB | Late-interaction retrieval paradigm evolution |

Each paper gets:
- A **30-second flash card** at the top of every report
- **Method genealogy table** tracing which elements came from which prior work
- **Claim-evidence mapping** — every paper claim checked against experimental support
- **Cross-paper comparison** showing method/problem/experiment differences in a single table
- **Innovation proposals** with concrete feasibility analysis

---

## Hooks

| Hook | When | What It Does |
|------|------|-------------|
| `SessionStart` | New Claude Code session | Injects your 3 most recently read papers + list of available MCP tools |
| `UserPromptSubmit` | Every user message | Scans for paper-related keywords → if matched, injects retrieval tips |

Both hooks read trigger keywords from `settings.json` — no code changes needed to customize.

---

## FAQ

<details>
<summary><b>Q: Obsidian graph shows no nodes?</b></summary>

1. Verify Obsidian vault path matches `vault_dir`
2. Graph settings gear → Ensure "Existing files" is ON
3. Check no path filter excludes `papers/`
</details>

<details>
<summary><b>Q: MCP Server won't start?</b></summary>

```bash
# Quick dependency check
python -c "import chromadb, watchfiles, frontmatter, pydantic; print('OK')"

# Test MCP server manually
cd deep_read_paper_skill
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' | python -m mcp_server
```
</details>

<details>
<summary><b>Q: Chinese search results are poor?</b></summary>

Default embedding model (`all-MiniLM-L6-v2`) is optimized for English. For better Chinese support, change `embedding_model` in `settings.json` to `paraphrase-multilingual-MiniLM-L12-v2`. Re-indexing is required after the switch.
</details>

<details>
<summary><b>Q: PDF text extraction fails (scanned PDF)?</b></summary>

PyMuPDF cannot extract text from scanned/image-based PDFs. You'll need an OCR tool (e.g., Tesseract) to pre-process the PDF first. The skill will report when text extraction fails.
</details>

<details>
<summary><b>Q: How do I use this across multiple machines?</b></summary>

1. Copy the skill folder to each machine
2. Update `settings.json` paths for each machine
3. Run `python setup.py`
4. Use Git to sync the vault directory (or store it on a shared drive)
</details>

<details>
<summary><b>Q: Can I customize the analysis dimensions?</b></summary>

Yes — the analysis workflow is defined in `SKILL.md`. Modify any Phase (1-5) to add, remove, or reorder analysis dimensions. The report template in `references/report_template.md` should be updated accordingly.
</details>

---

## Dependencies

| Package | Why |
|---------|-----|
| `chromadb` (≥0.4) | Vector store for semantic paper search |
| `python-frontmatter` (≥1.0) | YAML frontmatter parsing in paper memory files |
| `pydantic` (≥2.0) | Input validation for MCP tool schemas |
| `watchfiles` (≥0.20) | File watcher for auto-indexing new/updated papers |
| `PyMuPDF` (≥1.23) | PDF text extraction (used by Claude Code directly) |

All are pure Python and install cleanly on Linux, macOS, and Windows.

---

## Contributing

Contributions are welcome! Areas particularly open for improvement:

- **Better PDF handling**: Support for 2-column papers, scanned PDF fallback
- **Additional languages**: Report templates in English, Japanese, etc.
- **New MCP tools**: Citation graph export, BibTeX generation, custom analysis dimensions
- **LLM backend flexibility**: Support for models beyond Claude

Please open an issue before submitting a PR for significant changes.

---

## License

MIT — see [LICENSE](LICENSE) for details.

---

## Acknowledgments

- **Obsidian** for the graph-based knowledge management paradigm
- **ChromaDB** for lightweight local vector search
- **PyMuPDF** for reliable PDF text extraction
- Built for and powered by **Claude Code**

---

<p align="center">
  <sub>Made with ❤️ for researchers who read too many papers and remember too few.</sub>
</p>
