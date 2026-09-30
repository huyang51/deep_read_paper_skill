import logging
import re
import chromadb
from chromadb.utils import embedding_functions
from pathlib import Path
from mcp_server.config import CHROMA_DIR, COLLECTION_NAME, EMBEDDING_MODEL
from mcp_server.hf_offline import prefer_cached_model
from mcp_server.markdown_parser import coerce_id, get_all_papers, parse_paper


def _as_year(value):
    """A plausible 4-digit publication year, or None.

    Frontmatter is hand-editable, so this field arrives as 2023, "2023",
    "2023a", "2023-06", "preprint", or a YAML list depending on the author.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        # Trailing `(?!\d)`, not `\b`: "2023a" is how a preprint revision gets
        # written, and there is no word boundary between "3" and "a" — the
        # boundary form silently returned None for exactly the case that
        # motivated the coercion.
        match = re.search(r"\b(1[89]\d{2}|20\d{2})(?!\d)", value)
        return int(match.group(1)) if match else None
    return None


class ChromaStore:
    def __init__(self):
        self.client = chromadb.PersistentClient(path=str(CHROMA_DIR))
        # Use ChromaDB's default (lightweight) embedder only for the specific
        # default model it ships with. For everything else (including
        # multilingual models), use SentenceTransformer so users can pick
        # any compatible model name from HuggingFace.
        if not EMBEDDING_MODEL or EMBEDDING_MODEL == "all-MiniLM-L6-v2":
            self.embedder = embedding_functions.DefaultEmbeddingFunction()
        else:
            # Must come first: these are environment variables, and
            # huggingface_hub reads them once, when it is first imported — which
            # the constructor below does. Without this the model load waits out
            # a Hub check even when the model is already on disk; see hf_offline.
            decision = prefer_cached_model(EMBEDDING_MODEL)
            logging.getLogger("paper_kb_mcp").info(f"Embedder: {decision.reason}")
            self.embedder = embedding_functions.SentenceTransformerEmbeddingFunction(
                model_name=EMBEDDING_MODEL
            )
        self.collection = None

    def init_collection(self):
        """Get or create the ChromaDB collection.

        A collection records the embedding function that built it, and ChromaDB
        refuses to hand it back to a different one — correct, since vectors from
        two models are not comparable. But the raw failure chain is unreadable:
        the get raises "Embedding function conflict", then the create fallback
        raises "Collection [paper_memories] already exists". That is what a user
        sees after changing `embedding_model`, so name the cause and the fix.
        """
        try:
            self.collection = self.client.get_collection(
                name=COLLECTION_NAME,
                embedding_function=self.embedder,
            )
            return
        except Exception:
            pass

        try:
            self.collection = self.client.create_collection(
                name=COLLECTION_NAME,
                embedding_function=self.embedder,
                metadata={"hnsw:space": "cosine"}
            )
        except Exception as e:
            # The create failed *and* the name is taken → the collection exists
            # but was built by a different embedder (1.x returns collection
            # objects from list_collections; plain names are tolerated).
            names = [getattr(c, "name", c) for c in self.client.list_collections()]
            if COLLECTION_NAME in names:
                raise RuntimeError(
                    f"向量索引 {CHROMA_DIR} 里的 collection「{COLLECTION_NAME}」是用"
                    f"别的嵌入模型建的，当前配置是「{EMBEDDING_MODEL}」。换模型等于换"
                    f"向量空间，旧向量不可复用——删掉该目录后重跑，索引会按当前模型"
                    f"重建（papers/ 与 relations 不受影响）。"
                ) from e
            raise

    def _make_embedding_text(self, paper: dict) -> str:
        """Create text for embedding from paper metadata."""
        title = paper.get("title", "")
        core = paper.get("core_contribution", "")
        keywords = " ".join(paper.get("keywords", []))
        method = paper.get("method_category", "")
        domain = paper.get("problem_domain", "")
        return f"{title}. {core}. {method}. {domain}. {keywords}"

    def index_all_papers(self):
        """Scan all papers and index them in ChromaDB. Only clears existing
        entries AFTER successfully scanning papers, preventing data loss on
        transient I/O failures."""
        if self.collection is None:
            self.init_collection()

        papers = get_all_papers()
        if not papers:
            return

        ids = []
        metadatas = []
        documents = []

        seen_ids = set()
        skipped = []
        for paper in papers:
            # Ids are the index's primary key, and chromadb rejects the entire
            # upsert if two of them collide. Ids come from hand-editable
            # frontmatter, so `id:` missing (or a duplicate) is a realistic
            # state — and both used to collapse to "" and raise
            # "Expected IDs to be unique". That matters more than it looks:
            # this runs before the handshake and again from the watcher on every
            # vault change, so one stray .md meant the server never answered
            # initialize (no tools registered at all) or the watcher died
            # silently for the rest of the session. Skipping the file is
            # recoverable and says so; refusing to start is not.
            paper_id = coerce_id(paper.get("id"))
            if paper_id is None or paper_id in seen_ids:
                skipped.append(f"{paper.get('file') or '?'}"
                               + ("" if paper_id is None else f" (id {paper_id} 重复)"))
                continue
            seen_ids.add(paper_id)
            paper_id = str(paper_id)
            emb_text = self._make_embedding_text(paper)

            ids.append(paper_id)
            documents.append(emb_text)
            metadatas.append({
                "id": paper_id,
                "title": str(paper.get("title", "")),
                "short_name": str(paper.get("short_name", "")),
                "year": str(paper.get("year", "")),
                "venue": str(paper.get("venue", "")),
                "authors": ", ".join(paper.get("authors", [])),
                "method_category": str(paper.get("method_category", "")),
                "problem_domain": str(paper.get("problem_domain", "")),
                "core_contribution": str(paper.get("core_contribution", "")),
                "novelty_level": str(paper.get("novelty_level", "")),
                "date_read": str(paper.get("date_read", "")),
                "keywords": ", ".join(paper.get("keywords", [])),
                "aliases": ", ".join(paper.get("aliases", [])),
                "tags": ", ".join(paper.get("tags", [])),
                "related_papers": ",".join(str(r) for r in paper.get("related_papers", [])),
                "file": str(paper.get("file", "")),
            })

        if skipped:
            logging.getLogger("paper_kb_mcp").warning(
                "以下论文文件未建立索引（papers/ 里的每个 .md 都需要唯一的整数 id: 字段）："
                + ", ".join(skipped)
            )

        if ids:
            # Delete orphans that no longer exist on disk, then upsert
            existing = self.collection.get()
            if existing["ids"]:
                ids_to_delete = set(existing["ids"]) - set(ids)
                if ids_to_delete:
                    self.collection.delete(ids=list(ids_to_delete))
            self.collection.upsert(
                ids=ids,
                documents=documents,
                metadatas=metadatas,
            )

    def search(self, query: str, n_results: int = 5) -> list[dict]:
        """Semantic search over papers."""
        if self.collection is None:
            self.init_collection()

        results = self.collection.query(
            query_texts=[query],
            n_results=min(n_results, max(1, self.collection.count())),
        )

        output = []
        if results["ids"] and results["ids"][0]:
            for i, paper_id in enumerate(results["ids"][0]):
                meta = results["metadatas"][0][i] if results["metadatas"][0] else {}
                dist = results["distances"][0][i] if results["distances"] else None
                output.append({
                    "paper_id": paper_id,
                    "title": meta.get("title", ""),
                    "year": meta.get("year", ""),
                    "venue": meta.get("venue", ""),
                    "method_category": meta.get("method_category", ""),
                    "problem_domain": meta.get("problem_domain", ""),
                    "core_contribution": meta.get("core_contribution", ""),
                    "novelty_level": meta.get("novelty_level", ""),
                    "keywords": meta.get("keywords", ""),
                    "similarity": round(1 - dist, 4) if dist is not None else None,
                })

        return output

    def upsert_paper(self, paper_id: str, paper_data: dict):
        """Insert or update a single paper in the index."""
        if self.collection is None:
            self.init_collection()

        emb_text = self._make_embedding_text(paper_data)

        metadata = {
            "id": str(paper_data.get("id", "")),
            "title": str(paper_data.get("title", "")),
            "short_name": str(paper_data.get("short_name", "")),
            "year": str(paper_data.get("year", "")),
            "venue": str(paper_data.get("venue", "")),
            "authors": ", ".join(paper_data.get("authors", [])),
            "method_category": str(paper_data.get("method_category", "")),
            "problem_domain": str(paper_data.get("problem_domain", "")),
            "core_contribution": str(paper_data.get("core_contribution", "")),
            "novelty_level": str(paper_data.get("novelty_level", "")),
            "date_read": str(paper_data.get("date_read", "")),
            "keywords": ", ".join(paper_data.get("keywords", [])),
            "aliases": ", ".join(paper_data.get("aliases", [])),
            "tags": ", ".join(paper_data.get("tags", [])),
            "related_papers": ",".join(str(r) for r in paper_data.get("related_papers", [])),
            "file": str(paper_data.get("file", "")),
        }

        self.collection.upsert(
            ids=[paper_id],
            documents=[emb_text],
            metadatas=[metadata],
        )

    def upsert_paper_by_file(self, filepath: Path):
        """Parse a paper markdown file and upsert it into the index."""
        paper = parse_paper(filepath)
        paper_id = coerce_id((paper or {}).get("id"))
        if paper_id is not None:
            self.upsert_paper(str(paper_id), paper)

    def delete_paper(self, paper_id: str):
        """Delete a paper from the index."""
        if self.collection is None:
            self.init_collection()
        self.collection.delete(ids=[paper_id])

    def get_stats(self) -> dict:
        """Get index statistics."""
        if self.collection is None:
            self.init_collection()

        count = self.collection.count()
        if count == 0:
            return {"total_papers": 0}

        all_data = self.collection.get()

        years = []
        keywords_counter = {}
        methods = {}
        for meta in all_data.get("metadatas", []):
            # Coerced, not appended raw: years come from hand-written frontmatter,
            # and a single "2023a"/"preprint"/list value used to raise inside
            # min()/max() and fail paper_index_stats for the entire vault.
            year = _as_year(meta.get("year"))
            if year is not None:
                years.append(year)
            for kw in meta.get("keywords", "").split(", "):
                kw = kw.strip()
                if kw:
                    keywords_counter[kw] = keywords_counter.get(kw, 0) + 1
            mc = meta.get("method_category", "")
            if mc:
                methods[mc] = methods.get(mc, 0) + 1

        top_keywords = sorted(keywords_counter.items(), key=lambda x: -x[1])[:10]

        return {
            "total_papers": count,
            "earliest_year": min(years) if years else None,
            "latest_year": max(years) if years else None,
            "date_range": f"{min(years)}-{max(years)}" if years else "N/A",
            "top_keywords": [{"keyword": k, "count": v} for k, v in top_keywords],
            "method_distribution": methods,
        }
