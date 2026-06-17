import sys
import json
import asyncio
import logging
from typing import Any
from pathlib import Path

from watchfiles import awatch, Change

from mcp_server.config import PAPERS_DIR
from mcp_server.models import (
    SearchInput, GetPaperInput, FindRelatedInput, SearchByMethodInput,
    PaperIndexInput, PaperRemoveInput, ResponseFormat,
)
from mcp_server.markdown_parser import (
    get_paper_by_id, get_all_papers, extract_wikilinks,
    create_paper_file, delete_paper_file, parse_paper,
    invalidate_papers_cache, add_backlinks_to_referenced_papers,
)
from mcp_server.chroma_store import ChromaStore
from mcp_server.cross_refs import find_related

logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("paper_kb_mcp")

store = ChromaStore()

# ═══════════════════════════════════════════════════════════════════════════════
# IMPORTANT: The TOOLS list below must be manually kept in sync with the Pydantic
# models in models.py (SearchInput, PaperIndexInput, etc.). When adding/changing
# a field in the Pydantic model, update the corresponding inputSchema here too.
# ═══════════════════════════════════════════════════════════════════════════════

# ─── tool definitions ───────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "paper_search",
        "description": "语义搜索论文知识库。支持中英文查询，返回最相关的论文及其核心信息。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索查询文本（中英文均可）"},
                "n_results": {"type": "integer", "default": 5, "description": "返回结果数量"},
                "response_format": {"type": "string", "enum": ["json", "markdown"], "default": "markdown"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "paper_get",
        "description": "获取指定论文的完整摘要信息，包括核心问题、方法概述、实验结论、局限性和前人工作关系。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "paper_id": {"type": "integer", "description": "论文ID"}
            },
            "required": ["paper_id"]
        }
    },
    {
        "name": "paper_find_related",
        "description": "查找与指定论文相关的其他论文，从frontmatter引用、wikilinks和共享关键词三方面发现关联。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "paper_id": {"type": "integer", "description": "论文ID"},
                "relation_type": {
                    "type": "string",
                    "enum": ["method_similar", "problem_related", "complementary", "evolutionary"],
                    "description": "可选的关系类型过滤"
                }
            },
            "required": ["paper_id"]
        }
    },
    {
        "name": "paper_search_by_method",
        "description": "按方法类别搜索论文，如 Contrastive Learning, Diffusion, Transformer 等。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "method_category": {"type": "string", "description": "方法类别，如 Contrastive Learning, Diffusion, Transformer"}
            },
            "required": ["method_category"]
        }
    },
    {
        "name": "paper_index",
        "description": "创建或更新一篇论文的结构化摘要到知识库。提供论文元数据（标题、年份、方法类别等）和 body 内容，自动生成 YAML frontmatter 并索引到向量库。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "paper_id": {"type": "integer", "description": "论文ID（更新时指定，新建时留空自动分配）"},
                "title": {"type": "string", "description": "论文标题"},
                "short_name": {"type": "string", "default": "", "description": "模型/方法简称（如 ReT, PreFLMR），用于文件名和图谱节点名"},
                "year": {"type": "integer", "description": "发表年份"},
                "venue": {"type": "string", "default": "", "description": "会议/期刊"},
                "authors": {"type": "array", "items": {"type": "string"}, "default": [], "description": "作者列表"},
                "method_category": {"type": "string", "default": "", "description": "方法类别"},
                "problem_domain": {"type": "string", "default": "", "description": "问题领域"},
                "keywords": {"type": "array", "items": {"type": "string"}, "default": [], "description": "关键词列表"},
                "core_contribution": {"type": "string", "default": "", "description": "一句话核心贡献"},
                "novelty_level": {"type": "string", "default": "", "description": "新颖性定级: incremental | substantial | breakthrough"},
                "related_papers": {"type": "array", "items": {"type": "integer"}, "default": [], "description": "关联论文ID列表"},
                "date_read": {"type": "string", "default": "", "description": "阅读日期 YYYY-MM-DD"},
                "aliases": {"type": "array", "items": {"type": "string"}, "default": [], "description": "别名列表（用于 Obsidian 图谱显示和搜索）"},
                "tags": {"type": "array", "items": {"type": "string"}, "default": [], "description": "标签列表"},
                "body": {"type": "string", "default": "", "description": "论文结构化摘要正文（Markdown，含wikilinks）"}
            },
            "required": ["title", "year"]
        }
    },
    {
        "name": "paper_remove",
        "description": "从知识库中删除一篇论文（包括文件和向量索引）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "paper_id": {"type": "integer", "description": "要删除的论文ID"}
            },
            "required": ["paper_id"]
        }
    },
    {
        "name": "paper_index_stats",
        "description": "获取知识库的统计信息：论文总数、时间范围、高频关键词、方法分布等。",
        "inputSchema": {
            "type": "object",
            "properties": {}
        }
    }
]

# ─── tool handlers ──────────────────────────────────────────────────────────

async def handle_paper_search(params: dict) -> str:
    try:
        input_data = SearchInput(**params)
    except Exception as e:
        return json.dumps({"error": f"Invalid parameters: {e}"})

    results = store.search(input_data.query, input_data.n_results)

    if input_data.response_format == ResponseFormat.markdown:
        if not results:
            return "**未找到相关论文**"
        md_lines = []
        for i, r in enumerate(results):
            sim = r.get("similarity")
            sim_str = f" (相似度: {sim})" if sim is not None else ""
            md_lines.append(
                f"### [{r['paper_id']}] {r['title']}{sim_str}\n"
                f"- **年份**: {r.get('year', 'N/A')} | **会议**: {r.get('venue', 'N/A')}\n"
                f"- **方法类别**: {r.get('method_category', 'N/A')}\n"
                f"- **问题领域**: {r.get('problem_domain', 'N/A')}\n"
                f"- **核心贡献**: {r.get('core_contribution', 'N/A')}\n"
                f"- **关键词**: {r.get('keywords', 'N/A')}"
            )
        return "\n\n".join(md_lines)
    else:
        return json.dumps(results, ensure_ascii=False, indent=2)


def _json_serializer(obj):
    """Custom JSON serializer for date and other non-serializable types."""
    from datetime import date
    if isinstance(obj, date):
        return str(obj)
    if hasattr(obj, '__str__'):
        return str(obj)
    return repr(obj)


async def handle_paper_get(params: dict) -> str:
    try:
        input_data = GetPaperInput(**params)
    except Exception as e:
        return json.dumps({"error": f"Invalid parameters: {e}"})

    paper = get_paper_by_id(input_data.paper_id)
    if not paper:
        return json.dumps({"error": f"论文 ID={input_data.paper_id} 未找到"})

    return json.dumps({
        "id": paper.get("id"),
        "title": paper.get("title"),
        "short_name": paper.get("short_name", ""),
        "year": paper.get("year"),
        "venue": paper.get("venue"),
        "authors": paper.get("authors"),
        "method_category": paper.get("method_category"),
        "problem_domain": paper.get("problem_domain"),
        "keywords": paper.get("keywords"),
        "core_contribution": paper.get("core_contribution"),
        "novelty_level": paper.get("novelty_level", ""),
        "related_papers": paper.get("related_papers"),
        "date_read": paper.get("date_read"),
        "aliases": paper.get("aliases", []),
        "tags": paper.get("tags"),
        "body": paper.get("body", ""),
        "wikilinks": extract_wikilinks(paper.get("body", "")),
        "file": paper.get("file"),
    }, ensure_ascii=False, indent=2, default=_json_serializer)


async def handle_find_related(params: dict) -> str:
    try:
        input_data = FindRelatedInput(**params)
    except Exception as e:
        return json.dumps({"error": f"Invalid parameters: {e}"})

    results = find_related(input_data.paper_id, input_data.relation_type)
    return json.dumps(results, ensure_ascii=False, indent=2)


async def handle_search_by_method(params: dict) -> str:
    try:
        input_data = SearchByMethodInput(**params)
    except Exception as e:
        return json.dumps({"error": f"Invalid parameters: {e}"})

    all_papers = get_all_papers()
    method_lower = input_data.method_category.lower()
    matches = []
    for paper in all_papers:
        paper_method = paper.get("method_category", "").lower()
        if method_lower in paper_method or any(method_lower in kw.lower() for kw in paper.get("keywords", [])):
            matches.append({
                "paper_id": paper["id"],
                "title": paper.get("title", ""),
                "year": paper.get("year", ""),
                "venue": paper.get("venue", ""),
                "method_category": paper.get("method_category", ""),
                "problem_domain": paper.get("problem_domain", ""),
                "core_contribution": paper.get("core_contribution", ""),
            })

    return json.dumps(matches, ensure_ascii=False, indent=2)


async def handle_paper_index(params: dict) -> str:
    try:
        input_data = PaperIndexInput(**params)
    except Exception as e:
        return json.dumps({"error": f"Invalid parameters: {e}"})

    paper_dict = input_data.model_dump()
    is_update = paper_dict.get("paper_id") is not None

    try:
        filepath = create_paper_file(paper_dict)
    except Exception as e:
        return json.dumps({"error": f"创建论文文件失败: {e}"})

    paper = parse_paper(filepath)
    if paper:
        store.upsert_paper(str(paper["id"]), paper)

        # Add reverse wikilinks from old papers → new paper so Obsidian graph
        # arrows show the direction of academic influence (old → new).
        new_id = paper["id"]
        short_name = paper.get("short_name", "")
        related = paper.get("related_papers", [])
        if not is_update and short_name and related:
            add_backlinks_to_referenced_papers(new_id, short_name, related)

        return json.dumps({
            "status": "ok",
            "paper_id": paper["id"],
            "file": paper.get("file", str(filepath)),
            "message": f"论文已{'更新' if is_update else '创建'}: {paper.get('title')}"
        }, ensure_ascii=False, indent=2)
    return json.dumps({"error": "创建论文文件失败"})


async def handle_paper_remove(params: dict) -> str:
    try:
        input_data = PaperRemoveInput(**params)
    except Exception as e:
        return json.dumps({"error": f"Invalid parameters: {e}"})

    paper = get_paper_by_id(input_data.paper_id)
    if not paper:
        return json.dumps({"error": f"论文 ID={input_data.paper_id} 未找到"})

    title = paper.get("title", "Unknown")
    # Delete file first, then index — if file deletion fails, index remains consistent
    file_deleted = delete_paper_file(input_data.paper_id)
    store.delete_paper(str(input_data.paper_id))

    return json.dumps({
        "status": "ok",
        "paper_id": input_data.paper_id,
        "message": f"论文已删除: {title}"
    }, ensure_ascii=False, indent=2)


async def handle_index_stats(params: dict = None) -> str:
    stats = store.get_stats()
    return json.dumps(stats, ensure_ascii=False, indent=2)


TOOL_DISPATCH = {
    "paper_search": handle_paper_search,
    "paper_get": handle_paper_get,
    "paper_find_related": handle_find_related,
    "paper_search_by_method": handle_search_by_method,
    "paper_index": handle_paper_index,
    "paper_remove": handle_paper_remove,
    "paper_index_stats": handle_index_stats,
}


# ─── JSON-RPC handler ───────────────────────────────────────────────────────

async def handle_request(method: str, request_id: Any, params: dict = None) -> dict:
    """Dispatch a JSON-RPC request."""
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {
                    "name": "paper_kb_mcp",
                    "version": "1.1.0"
                }
            }
        }
    elif method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"tools": TOOLS}
        }
    elif method == "tools/call":
        tool_name = params.get("name", "")
        tool_args = params.get("arguments", {})
        handler = TOOL_DISPATCH.get(tool_name)
        if not handler:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"}
            }
        try:
            result_text = await handler(tool_args)
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": result_text}]
                }
            }
        except Exception as e:
            logger.error(f"Tool {tool_name} error: {e}")
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32000, "message": str(e)}
            }
    elif method == "notifications/initialised":
        # No response for notifications
        return None
    elif method == "ping":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {}
        }
    else:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"}
        }


def write_response(response: dict):
    """Write a JSON-RPC response to stdout."""
    if response is None:
        return
    line = json.dumps(response, ensure_ascii=False)
    sys.stdout.buffer.write((line + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


# ─── watcher ────────────────────────────────────────────────────────────────

async def watch_vault():
    """Watch papers directory for changes and update index incrementally.
    If the papers directory does not exist at startup, retry every 10 seconds
    instead of permanently disabling the watcher."""
    while not PAPERS_DIR.exists():
        logger.warning(f"Papers dir not found: {PAPERS_DIR}, retrying in 10s...")
        await asyncio.sleep(10)
    invalidate_papers_cache()
    logger.info(f"Watching {PAPERS_DIR} for changes...")
    async for changes in awatch(PAPERS_DIR):
        logger.info(f"Detected changes: {changes}")
        for change_type, path_str in changes:
            filepath = Path(path_str)
            if not filepath.suffix == ".md":
                continue
            if change_type in (Change.added, Change.modified):
                try:
                    store.upsert_paper_by_file(filepath)
                    logger.info(f"  Indexed: {filepath.name}")
                except Exception as e:
                    logger.error(f"  Failed to index {filepath.name}: {e}")
            elif change_type == Change.deleted:
                # Can't get paper_id from deleted file, do full re-sync
                logger.info(f"  Deleted: {filepath.name}, re-syncing index")
                store.index_all_papers()
                break


# ─── main ───────────────────────────────────────────────────────────────────

async def read_stdin(loop):
    """Read a line from stdin using a thread executor.
    Note: On Windows, sharing stdin between main thread and executor thread can
    occasionally cause issues under heavy load. If hangs are observed, consider
    using asyncio.StreamReader wrapping sys.stdin.buffer instead."""
    return await loop.run_in_executor(None, sys.stdin.readline)


async def main():
    """Main MCP server loop."""
    # Ensure UTF-8 encoding for stdin/stdout on Windows (GBK default causes crashes)
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    logger.info("Starting paper_kb_mcp server...")

    # Initialize index
    store.init_collection()
    store.index_all_papers()
    logger.info(f"Indexed {store.collection.count()} papers.")

    # Start file watcher in background
    watcher_task = asyncio.create_task(watch_vault())
    loop = asyncio.get_event_loop()

    try:
        while True:
            try:
                line = await read_stdin(loop)
            except Exception as e:
                logger.error(f"Error reading stdin: {e}")
                break

            if not line:
                logger.info("stdin closed, exiting.")
                break

            line = line.strip()
            if not line:
                continue

            try:
                msg = json.loads(line)
            except json.JSONDecodeError as e:
                logger.error(f"Invalid JSON: {e}")
                continue

            method = msg.get("method", "")
            request_id = msg.get("id")
            params = msg.get("params", {})

            response = await handle_request(method, request_id, params)
            write_response(response)
    finally:
        watcher_task.cancel()
        try:
            await watcher_task
        except asyncio.CancelledError:
            pass


def run():
    asyncio.run(main())


if __name__ == "__main__":
    run()
