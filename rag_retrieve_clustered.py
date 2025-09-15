#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
RAG Retriever (cross-platform) — features:
 - Cross-platform paths, no hard-coded /mnt/data
 - Auto-pick SentenceTransformer embedder to match FAISS index dimension
 - Multi-Query variants (+ optional HyDE)
 - Dense FAISS search
 - Optional Hybrid: BM25 (sparse) + RRF fusion with dense
 - Optional MMR diversification
 - Optional Cross-Encoder reranking
 - Parent-Child expansion (if chunk.meta.parent_id exists)
 - Context preview printing

Example (PowerShell):
  python .\\rag_retrieve_clustered.py `
    --chunks .\\out\\wstg_chunks.from_knowledge.jsonl `
    --faiss  .\\out\\wstg_faiss.index `
    --ids    .\\out\\wstg_faiss_ids.json `
    --backend sbert `
    --query "Insecure Direct Object References (IDOR) testing" `
    --multiquery 6 `
    --hyde `
    --k-per-branch 60 `
    --deliver-to-llm 12 `
    --hybrid --rrf-k 60 `
    --rerank --mmr `
    --print-context
"""
import argparse
import json
import sys
import math
import re
from pathlib import Path
from typing import Dict, List, Tuple, Any, Optional

import numpy as np
import faiss

# sentence-transformers for embeddings + reranker (optional)
try:
    from sentence_transformers import SentenceTransformer, CrossEncoder
except Exception:
    SentenceTransformer = None
    CrossEncoder = None


# =========================
# Utilities
# =========================
def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)


def read_json(path: Path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def read_chunks_jsonl(path: Path) -> Dict[str, Dict[str, Any]]:
    """
    Each line is a JSON object:
      {"id": "...", "text": "...", "meta": {...}}
    Returns dict[id] = obj
    """
    data: Dict[str, Dict[str, Any]] = {}
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            cid = obj.get("id")
            if cid:
                data[cid] = obj
    return data


def normalize(vecs: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-12
    return (vecs / norms).astype("float32")


# =========================
# Sparse/BM25 + RRF (generic safety net)
# =========================
WORD_RE = re.compile(r"[A-Za-z0-9_+#]+")


def tokenize(text: str) -> List[str]:
    return [t.lower() for t in WORD_RE.findall(text or "")]


def build_bm25_stats(chunks: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    N = len(chunks)
    df: Dict[str, int] = {}
    doc_len: Dict[str, int] = {}
    for cid, obj in chunks.items():
        tokens = tokenize(obj.get("text") or "")
        doc_len[cid] = len(tokens)
        seen = set(tokens)
        for t in seen:
            df[t] = df.get(t, 0) + 1
    avgdl = (sum(doc_len.values()) / max(1, N)) if N else 0.0
    return {"N": N, "df": df, "doc_len": doc_len, "avgdl": avgdl}


def bm25_rank(query: str, chunks: Dict[str, Dict[str, Any]], stats: Dict[str, Any],
              topn: int = 60, k1: float = 1.5, b: float = 0.75) -> List[str]:
    qtokens = tokenize(query)
    if not qtokens:
        return []
    N, df, doc_len, avgdl = stats["N"], stats["df"], stats["doc_len"], stats["avgdl"]
    scores: Dict[str, float] = {}
    for cid, obj in chunks.items():
        tokens = tokenize(obj.get("text") or "")
        if not tokens:
            continue
        # term freq per doc
        tf: Dict[str, int] = {}
        for t in tokens:
            tf[t] = tf.get(t, 0) + 1
        L = doc_len.get(cid, 0) or 1
        s = 0.0
        for t in qtokens:
            n = df.get(t, 0)
            if n == 0:
                continue
            idf = math.log(1 + (N - n + 0.5) / (n + 0.5))
            f = tf.get(t, 0)
            s += idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * L / max(1.0, avgdl)))
        if s > 0:
            scores[cid] = s
    ranked = sorted(scores, key=scores.get, reverse=True)
    return ranked[:topn]


def rrf_fuse(order_a: List[str], order_b: List[str], k: int = 60, topn: int = 60) -> List[str]:
    ranks_a = {cid: i for i, cid in enumerate(order_a)}
    ranks_b = {cid: i for i, cid in enumerate(order_b)}
    all_ids = list({*order_a, *order_b})

    def rrf_score(cid: str) -> float:
        s = 0.0
        if cid in ranks_a:
            s += 1.0 / (k + ranks_a[cid] + 1)
        if cid in ranks_b:
            s += 1.0 / (k + ranks_b[cid] + 1)
        return s

    fused = sorted(all_ids, key=rrf_score, reverse=True)
    return fused[:topn]


# =========================
# MMR Diversification
# =========================
def mmr(candidate_vecs: np.ndarray,
        query_vec: np.ndarray,
        lambda_mult: float = 0.5,
        topn: int = 12) -> List[int]:
    """
    Basic MMR on cosine-sim (assuming normalized vectors).
    Returns selected indices into candidate_vecs.
    """
    if candidate_vecs.size == 0:
        return []

    sim_to_query = candidate_vecs @ query_vec.reshape(-1, 1)  # (N,1)
    sim_to_query = sim_to_query.flatten()

    selected: List[int] = []
    rest: List[int] = list(range(candidate_vecs.shape[0]))

    while rest and len(selected) < topn:
        if not selected:
            # pick the best to query first
            best = int(np.argmax(sim_to_query[rest]))
            first = rest[best]
            selected.append(first)
            rest.remove(first)
            continue

        selected_vecs = candidate_vecs[selected]  # (k, d)
        sim_to_selected = candidate_vecs @ selected_vecs.T     # (N, k)
        max_sim_selected = sim_to_selected.max(axis=1)         # (N,)

        mmr_score = lambda_mult * sim_to_query + (1.0 - lambda_mult) * (1.0 - max_sim_selected)
        # choose best among remaining
        best_idx = max(rest, key=lambda r: mmr_score[r])
        selected.append(best_idx)
        rest.remove(best_idx)

    return selected


# =========================
# Multi-Query / HyDE (lightweight)
# =========================
def make_query_variants(q: str, n_variants: int = 3) -> List[str]:
    """
    Simple deterministic variants (replace with LLM-based expansion if you want).
    """
    base = [q.strip()]
    if n_variants <= 1:
        return base

    # some light rewrites
    v2 = q + " testing steps and checklist"
    v3 = q.replace("testing", "test").replace("vulnerability", "vuln")
    v4 = "definition and examples: " + q

    variants = [v for v in [q, v2, v3, v4] if v]
    # de-dup keeping order
    seen = set()
    uniq: List[str] = []
    for v in variants:
        if v not in seen:
            uniq.append(v)
            seen.add(v)
    return uniq[:max(1, n_variants)]


def hyde_hint(q: str, enabled: bool = False) -> str:
    """
    Placeholder HyDE text. If you want a real HyDE, call your LLM to
    generate a short hypothetical answer (100-150 tokens) and return it here.
    """
    if not enabled:
        return ""
    # naive pseudo-hint
    return (
        f"This is a short hypothetical answer related to '{q}'. "
        "It outlines typical goals, steps, and key indicators used in security testing. "
        "It mentions relevant checks, risks, preconditions, and expected outputs that may "
        "appear in authoritative guidelines. Keep it concise."
    )


# =========================
# Embedder / Reranker
# =========================
AUTO_MODEL_BY_DIM = {
    # common dims -> a popular, compatible model
    384: "sentence-transformers/all-MiniLM-L6-v2",
    768: "BAAI/bge-base-en-v1.5",
    1024: "BAAI/bge-large-en-v1.5",
    # add more if your index uses a different dim
}


class Embedder:
    def __init__(self, model_name: str):
        if SentenceTransformer is None:
            raise RuntimeError("sentence-transformers is required. Please install it.")
        self.model_name = model_name
        self.model = SentenceTransformer(self.model_name)

    def encode(self, texts: List[str]) -> np.ndarray:
        emb = self.model.encode(
            texts,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return emb.astype("float32")

    @property
    def dim(self) -> int:
        try:
            return int(self.model.get_sentence_embedding_dimension())
        except Exception:
            v = self.encode(["dim-probe"])[0]
            return int(v.shape[0])


class ReRanker:
    def __init__(self, model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"):
        if CrossEncoder is None:
            raise RuntimeError("sentence-transformers (CrossEncoder) is required for --rerank.")
        self.model_name = model_name
        self.model = CrossEncoder(self.model_name)

    def score(self, query: str, docs: List[str]) -> np.ndarray:
        pairs = [(query, d) for d in docs]
        scores = self.model.predict(pairs)
        return np.asarray(scores, dtype="float32")


def pick_embedder_model_name(index_dim: int, user_model: Optional[str]) -> str:
    """
    Decide which embedder to load:
    - If user specified one, return it (we'll verify dim later).
    - Else, auto-pick based on the FAISS index dimension.
    """
    if user_model:
        return user_model
    if index_dim in AUTO_MODEL_BY_DIM:
        return AUTO_MODEL_BY_DIM[index_dim]
    raise RuntimeError(
        f"Cannot auto-pick embedder for FAISS dim={index_dim}. "
        "Please pass --embedder with the exact model used to build the index "
        "(e.g., 'sentence-transformers/all-MiniLM-L6-v2' for 384, "
        "'BAAI/bge-base-en-v1.5' for 768, 'BAAI/bge-large-en-v1.5' for 1024)."
    )


# =========================
# Core retrieval
# =========================
def retrieve(
    query: str,
    index: faiss.Index,
    all_ids: List[str],
    chunks: Dict[str, Dict[str, Any]],
    embedder: Embedder,
    k_per_branch: int = 20,
    deliver_to_llm: int = 10,
    use_mmr: bool = False,
    mmr_lambda: float = 0.5,
    use_rerank: bool = False,
    reranker: Optional[ReRanker] = None,
    n_variants: int = 3,
    use_hyde: bool = False,
    hybrid: bool = False,
    bm25_stats: Optional[Dict[str, Any]] = None,
    rrf_k: int = 60,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    Returns (contexts, top_ids)
      - contexts: list of chunk objects (child + optional parent)
      - top_ids: list of selected child chunk IDs (for logging / debugging)
    """
    variants = make_query_variants(query, n_variants=n_variants)
    hint = hyde_hint(query, enabled=use_hyde)
    if hint:
        variants.append(hint)

    # --- Dense search per variant ---
    all_hits: List[Tuple[str, float]] = []  # (chunk_id, distance/score)
    for v in variants:
        qvec = embedder.encode([v])
        if qvec.shape[1] != index.d:
            raise AssertionError(
                f"Query vector dim ({qvec.shape[1]}) != index dim ({index.d}). "
                "Please rebuild the index or use an embedder with matching dimension."
            )
        D, I = index.search(qvec, k_per_branch)
        for i, d in zip(I[0], D[0]):
            if i < 0:
                continue
            cid = all_ids[i]
            if cid in chunks:
                all_hits.append((cid, float(d)))

    # Deduplicate IDs keeping the best score (direction depends on index metric)
    metric_type = getattr(index, "metric_type", faiss.METRIC_INNER_PRODUCT)
    bigger_is_better = (metric_type == faiss.METRIC_INNER_PRODUCT)

    best: Dict[str, float] = {}
    for cid, score in all_hits:
        if cid not in best:
            best[cid] = score
        else:
            if (bigger_is_better and score > best[cid]) or (not bigger_is_better and score < best[cid]):
                best[cid] = score

    candidate_ids = list(best.keys())
    if not candidate_ids:
        return [], []

    # --- Dense order (pre-fusion) ---
    dense_order = sorted(candidate_ids, key=lambda c: best[c], reverse=bigger_is_better)[:60]

    # --- Hybrid fusion: Dense + Sparse(BM25) via RRF ---
    ordered_ids = dense_order
    if hybrid and bm25_stats is not None:
        sparse_order = bm25_rank(query, chunks, bm25_stats, topn=60)
        if sparse_order:
            ordered_ids = rrf_fuse(dense_order, sparse_order, k=rrf_k, topn=60)

    # --- Optional MMR diversify on ordered list ---
    if use_mmr:
        cand_texts = [chunks[cid]["text"][:1200] for cid in ordered_ids]
        cand_vecs = embedder.encode(cand_texts)  # normalized by ST
        qvec = embedder.encode([query])[0]
        sel_idx = mmr(cand_vecs, qvec, lambda_mult=mmr_lambda, topn=min(60, len(ordered_ids)))
        ordered_ids = [ordered_ids[i] for i in sel_idx]

    # --- Optional rerank (query, doc[:2048]) ---
    if use_rerank:
        if reranker is None:
            reranker = ReRanker()
        docs = [chunks[cid]["text"][:2048] for cid in ordered_ids]
        scores = reranker.score(query, docs)
        order = np.argsort(-scores)
        top_ids = [ordered_ids[i] for i in order[:deliver_to_llm]]
    else:
        top_ids = ordered_ids[:deliver_to_llm]

    # --- Parent-Child expansion ---
    contexts: List[Dict[str, Any]] = []
    seen_ctx = set()
    for cid in top_ids:
        obj = chunks[cid]
        parent_id = obj.get("meta", {}).get("parent_id")
        if parent_id and parent_id in chunks:
            pobj = chunks[parent_id]
            if parent_id not in seen_ctx:
                contexts.append(pobj)
                seen_ctx.add(parent_id)
        if cid not in seen_ctx:
            contexts.append(obj)
            seen_ctx.add(cid)

    return contexts, top_ids


# =========================
# CLI
# =========================
def main():
    ap = argparse.ArgumentParser(description="RAG Retriever (UR-ready, hybrid optional)")
    ap.add_argument("--chunks", required=True, help="Path to chunks JSONL")
    ap.add_argument("--faiss", required=True, dest="faiss_path", help="Path to FAISS index")
    ap.add_argument("--ids",   required=True, help="Path to FAISS ids.json")

    ap.add_argument("--backend", default="sbert", choices=["sbert"], help="Embed backend (only sbert supported)")
    ap.add_argument("--embedder", default=None, help="SentenceTransformer model name (optional, auto if omitted)")
    ap.add_argument("--query", required=True, help="User query")
    ap.add_argument("--preset", default="auto", help="Placeholder to keep compatibility")

    ap.add_argument("--k-per-branch", type=int, default=20, help="Top-k per query variant")
    ap.add_argument("--deliver-to-llm", type=int, default=10, help="How many chunks to pass to LLM")

    ap.add_argument("--multiquery", type=int, default=3, help="Number of query variants")
    ap.add_argument("--hyde", action="store_true", help="Enable simple HyDE hint")

    ap.add_argument("--hybrid", action="store_true", help="Enable sparse BM25 + dense fusion (RRF)")
    ap.add_argument("--rrf-k", type=int, default=60, help="RRF k constant")

    ap.add_argument("--mmr", action="store_true", help="Enable MMR diversification")
    ap.add_argument("--mmr-lambda", type=float, default=0.5, help="MMR lambda (0..1)")

    ap.add_argument("--rerank", action="store_true", help="Enable Cross-Encoder reranking")
    ap.add_argument("--reranker", default="cross-encoder/ms-marco-MiniLM-L-6-v2", help="Cross-Encoder name")

    ap.add_argument("--print-context", action="store_true", help="Print selected contexts preview")

    # Accept unknown args to stay compatible with older wrappers
    args, _ = ap.parse_known_args()

    # Resolve paths cross-platform
    CHUNKS_PATH = Path(args.chunks).expanduser().resolve()
    INDEX_PATH  = Path(args.faiss_path).expanduser().resolve()
    IDS_PATH    = Path(args.ids).expanduser().resolve()

    for p, name in [(CHUNKS_PATH, "--chunks"), (INDEX_PATH, "--faiss"), (IDS_PATH, "--ids")]:
        if not p.exists():
            raise FileNotFoundError(f"{name} not found: {p}")

    # Load FAISS
    eprint(f"[info] Loading FAISS index: {INDEX_PATH}")
    index = faiss.read_index(str(INDEX_PATH))
    idx_dim = int(index.d)
    metric_type = getattr(index, "metric_type", faiss.METRIC_INNER_PRODUCT)
    metric_name = "IP" if metric_type == faiss.METRIC_INNER_PRODUCT else "L2/Other"
    eprint(f"[info] FAISS index dim: {idx_dim} (metric: {metric_name}), ntotal: {index.ntotal}")

    # Load ids/chunks
    eprint(f"[info] Loading ids: {IDS_PATH}")
    all_ids: List[str] = read_json(IDS_PATH)
    if len(all_ids) != index.ntotal:
        eprint(f"[warn] ids count ({len(all_ids)}) != index.ntotal ({index.ntotal}). Proceeding, but ensure they match.")

    eprint(f"[info] Loading chunks: {CHUNKS_PATH}")
    chunks = read_chunks_jsonl(CHUNKS_PATH)

    # Pick and build embedder
    chosen_model = pick_embedder_model_name(idx_dim, args.embedder)
    eprint(f"[info] Using embedder: {chosen_model}")
    embedder = Embedder(model_name=chosen_model)

    # Verify embedder dimension matches index
    probe_dim = embedder.dim
    if probe_dim != idx_dim:
        raise RuntimeError(
            f"Embedder '{chosen_model}' dim={probe_dim} does not match FAISS index dim={idx_dim}. "
            "Please pass --embedder with the exact model used to build the index. "
            "Common pairs: 384->all-MiniLM-L6-v2, 768->BAAI/bge-base-en-v1.5, 1024->BAAI/bge-large-en-v1.5."
        )

    # Optional reranker
    reranker = None
    if args.rerank:
        reranker = ReRanker(model_name=args.reranker)

    # Optional BM25 stats
    bm25_stats = None
    if args.hybrid:
        eprint("[info] Building BM25 stats...")
        bm25_stats = build_bm25_stats(chunks)

    # Retrieve
    contexts, top_ids = retrieve(
        query=args.query,
        index=index,
        all_ids=all_ids,
        chunks=chunks,
        embedder=embedder,
        k_per_branch=args.k_per_branch,
        deliver_to_llm=args.deliver_to_llm,
        use_mmr=args.mmr,
        mmr_lambda=args.mmr_lambda,
        use_rerank=args.rerank,
        reranker=reranker,
        n_variants=max(1, args.multiquery),
        use_hyde=args.hyde,
        hybrid=args.hybrid,
        bm25_stats=bm25_stats,
        rrf_k=args.rrf_k,
    )

    # Output
    print("==== Top Chunk IDs ====")
    for cid in top_ids:
        print(cid)

    if args.print_context:
        print("\n==== Context Preview ====")
        for i, obj in enumerate(contexts, 1):
            cid = obj.get("id", f"ctx-{i}")
            text = (obj.get("text") or "")[:500].replace("\n", " ")
            print(f"[{i}] {cid}: {text}")


if __name__ == "__main__":
    main()
