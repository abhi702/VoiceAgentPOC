import hashlib
import re
from pathlib import Path

from app.config import ROOT, settings
from app.rag.hybrid_rrf import RetrievedChunk, reciprocal_rank_fusion


def read_chunks(folder: Path) -> list[RetrievedChunk]:
    chunks: list[RetrievedChunk] = []
    for path in sorted(folder.glob("*.md")):
        title, buffer = path.stem, []
        sections: list[tuple[str, str]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("#"):
                if buffer:
                    sections.append((title, "\n".join(buffer)))
                title, buffer = line.lstrip("# ").strip(), []
            else:
                buffer.append(line)
        sections.append((title, "\n".join(buffer)))
        for heading, text in sections:
            for offset in range(0, len(text), 1000):
                content = text[offset:offset + 1200].strip()
                if content:
                    key = hashlib.sha256(f"{path.name}:{heading}:{offset}".encode()).hexdigest()
                    chunks.append(RetrievedChunk(key, heading, content))
    if not chunks:
        raise ValueError(f"No SOP Markdown content in {folder}")
    return chunks


class RagEngine:
    def __init__(self) -> None:
        self._ready = False

    def _initialize(self) -> None:
        from rank_bm25 import BM25Okapi
        from sentence_transformers import SentenceTransformer

        self.chunks = read_chunks(ROOT / "sops")
        self.model = SentenceTransformer(settings.EMBEDDING_MODEL)
        self.vectors = self.model.encode(
            [chunk.content for chunk in self.chunks], normalize_embeddings=True
        )
        self.bm25 = BM25Okapi([self._tokens(chunk.content) for chunk in self.chunks])
        self._ready = True

    @staticmethod
    def _tokens(text: str) -> list[str]:
        return re.findall(r"\w+", text.lower())

    def search(self, query: str) -> list[RetrievedChunk]:
        if settings.RAG_MODE == "off":
            return []
        if not self._ready:
            self._initialize()
        vector = self.model.encode(query, normalize_embeddings=True)
        if settings.RAG_MODE == "azure":
            return self._azure_search(query, vector.tolist())
        dense = self.vectors @ vector
        sparse = self.bm25.get_scores(self._tokens(query))
        vector_ranked = [
            self.chunks[int(i)] for i in dense.argsort()[::-1][:6] if dense[i] > 0.25
        ]
        bm25_ranked = [
            self.chunks[int(i)] for i in sparse.argsort()[::-1][:6] if sparse[i] > 0
        ]
        return reciprocal_rank_fusion(vector_ranked, bm25_ranked)

    def _azure_search(self, query: str, vector: list[float]) -> list[RetrievedChunk]:
        from azure.core.credentials import AzureKeyCredential
        from azure.search.documents import SearchClient
        from azure.search.documents.models import VectorizedQuery

        if not settings.AZURE_SEARCH_ENDPOINT or not settings.AZURE_SEARCH_API_KEY:
            raise ValueError("Azure Search mode requires endpoint and key")
        with SearchClient(
            settings.AZURE_SEARCH_ENDPOINT, settings.AZURE_SEARCH_INDEX_NAME,
            AzureKeyCredential(settings.AZURE_SEARCH_API_KEY),
        ) as client:
            sparse = list(client.search(search_text=query, top=6))
            dense = list(client.search(
                search_text=None, vector_queries=[
                    VectorizedQuery(vector=vector, k_nearest_neighbors=6, fields="embedding")
                ], top=6,
            ))
        def convert(rows: list) -> list[RetrievedChunk]:
            return [RetrievedChunk(row["id"], row["title"], row["content"]) for row in rows]
        return reciprocal_rank_fusion(convert(dense), convert(sparse))

    def upload(self) -> int:
        from azure.core.credentials import AzureKeyCredential
        from azure.search.documents import SearchClient
        from azure.search.documents.indexes import SearchIndexClient
        from azure.search.documents.indexes.models import (
            HnswAlgorithmConfiguration, SearchField, SearchFieldDataType, SearchIndex,
            SearchableField, SimpleField, VectorSearch, VectorSearchProfile,
        )

        if not settings.AZURE_SEARCH_ENDPOINT or not settings.AZURE_SEARCH_API_KEY:
            raise ValueError("Ingestion needs an Azure Search endpoint and ADMIN key")
        self._initialize()
        credential = AzureKeyCredential(settings.AZURE_SEARCH_API_KEY)
        index = SearchIndex(
            name=settings.AZURE_SEARCH_INDEX_NAME,
            fields=[
                SimpleField(name="id", type=SearchFieldDataType.String, key=True),
                SearchableField(name="title", type=SearchFieldDataType.String),
                SearchableField(name="content", type=SearchFieldDataType.String),
                SearchField(
                    name="embedding", type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
                    searchable=True, vector_search_dimensions=len(self.vectors[0]),
                    vector_search_profile_name="clinic-vector",
                ),
            ],
            vector_search=VectorSearch(
                algorithms=[HnswAlgorithmConfiguration(name="clinic-hnsw")],
                profiles=[VectorSearchProfile(
                    name="clinic-vector", algorithm_configuration_name="clinic-hnsw"
                )],
            ),
        )
        with SearchIndexClient(settings.AZURE_SEARCH_ENDPOINT, credential) as manager:
            manager.create_or_update_index(index)
        with SearchClient(settings.AZURE_SEARCH_ENDPOINT, settings.AZURE_SEARCH_INDEX_NAME, credential) as client:
            results = client.upload_documents([
                {"id": chunk.doc_id, "title": chunk.title, "content": chunk.content,
                 "embedding": vector.tolist()}
                for chunk, vector in zip(self.chunks, self.vectors, strict=True)
            ])
        failures = [result.key for result in results if not result.succeeded]
        if failures:
            raise RuntimeError(f"Search ingestion failed for {len(failures)} chunks")
        return len(results)


if __name__ == "__main__":
    print(f"Uploaded {RagEngine().upload()} synthetic SOP chunks")
