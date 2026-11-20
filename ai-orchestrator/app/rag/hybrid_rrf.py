from dataclasses import dataclass, replace


@dataclass(frozen=True)
class RetrievedChunk:
    doc_id: str
    title: str
    content: str
    score: float = 0


def reciprocal_rank_fusion(
    vector_ranked: list[RetrievedChunk], bm25_ranked: list[RetrievedChunk],
    k: int = 60, top_n: int = 3,
) -> list[RetrievedChunk]:
    if k < 1 or top_n < 1:
        raise ValueError("k and top_n must be positive")
    scores: dict[str, float] = {}
    chunks: dict[str, RetrievedChunk] = {}
    for ranking in (vector_ranked, bm25_ranked):
        seen: set[str] = set()
        for rank, chunk in enumerate(ranking, 1):
            if chunk.doc_id in seen:
                continue
            seen.add(chunk.doc_id)
            scores[chunk.doc_id] = scores.get(chunk.doc_id, 0) + 1 / (k + rank)
            chunks[chunk.doc_id] = chunk
    return [
        replace(chunks[key], score=scores[key])
        for key in sorted(scores, key=lambda key: scores[key], reverse=True)[:top_n]
    ]
