import chromadb
from chromadb.utils import embedding_functions
from pathlib import Path
from mcp_server.config import CHROMA_DIR, COLLECTION_NAME, EMBEDDING_MODEL
from mcp_server.markdown_parser import get_all_papers, parse_paper


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
            self.embedder = embedding_functions.SentenceTransformerEmbeddingFunction(
                model_name=EMBEDDING_MODEL
            )
        self.collection = None

    def init_collection(self):
        """Get or create the ChromaDB collection."""
        try:
            self.collection = self.client.get_collection(
                name=COLLECTION_NAME,
                embedding_function=self.embedder,
            )
        except Exception:
            self.collection = self.client.create_collection(
                name=COLLECTION_NAME,
                embedding_function=self.embedder,
                metadata={"hnsw:space": "cosine"}
            )

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

        for paper in papers:
            paper_id = str(paper.get("id", ""))
            emb_text = self._make_embedding_text(paper)

            ids.append(paper_id)
            documents.append(emb_text)
            metadatas.append({
                "id": str(paper.get("id", "")),
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
        if paper and paper.get("id") is not None:
            self.upsert_paper(str(paper["id"]), paper)

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
            y = meta.get("year")
            if y:
                years.append(int(y) if isinstance(y, str) else y)
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
