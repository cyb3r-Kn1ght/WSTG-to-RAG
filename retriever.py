import re, json, math, argparse
from typing import List, Dict, Any, Optional, Tuple, Set
from collections import Counter, defaultdict

import faiss
from sentence_transformers import SentenceTransformer

# reranker (optional)
try:
    from FlagEmbedding import FlagReranker
    RERANKER = FlagReranker("BAAI/bge-reranker-v2-m3", use_fp16=True)
except Exception:
    RERANKER = None

INDEX = "wstg.faiss"
META  = "wstg.meta.json"
DOCS  = "wstg_docs_clean.jsonl"

SEC_PAT = re.compile(r"\bWSTG-[A-Z]{3,6}-\d{2}\b")

STOP = set("""
a an and are as at be been but by for from has have if in into is it its of on or such
that the their then there these this those to was were will with your you we our they them
themselves ourselves himself herself itself which who whom whose than while when where
can could should would may might also not no yes true false null na
""".split())

def tokenize(s: str) -> List[str]:
    toks = re.split(r"[^\w]+", s.lower())
    out=[]
    for t in toks:
        if not t or t in STOP: continue
        if t.isdigit(): continue
        if len(t) < 3: continue
        out.append(t)
    return out

def load_corpus(meta_path: str, docs_path: str):
    metas = json.load(open(meta_path, "r", encoding="utf-8"))
    id2meta = {m["id"]: m for m in metas}
    id2text, id2num = {}, {}
    with open(docs_path, "r", encoding="utf-8") as f:
        for line in f:
            o = json.loads(line)
            rid = o["id"]
            id2text[rid] = o["text"]
            m = re.search(r"kb-(\d+)", rid)
            if m: id2num[rid] = int(m.group(1))
    return metas, id2meta, id2text, id2num

def build_idf(id2text: Dict[str,str]):
    N = len(id2text)
    df = Counter()
    for txt in id2text.values():
        uniq = set(tokenize(txt))
        df.update(uniq)
    idf = {t: math.log((N+1)/(df[t]+1)) + 1.0 for t in df}
    avgdl = sum(len(tokenize(txt)) for txt in id2text.values()) / max(1, N)
    return idf, avgdl

def bm25_score(text: str, query_terms: List[str], idf: Dict[str,float], avgdl: float, k1=1.6, b=0.75):
    toks = tokenize(text)
    if not toks: return 0.0
    tf = Counter(toks)
    dl = len(toks)
    score = 0.0
    for term in query_terms:
        if term not in idf: continue
        f = tf.get(term, 0)
        if f == 0: continue
        denom = f + k1*(1 - b + b*dl/avgdl)
        score += idf[term] * (f*(k1+1)) / denom
    return float(score)

def group_by_clusters(ids: List[str], id2num: Dict[str,int], gap: int=3):
    pairs = [(rid, id2num.get(rid, -1)) for rid in ids]
    pairs.sort(key=lambda x: x[1])
    clusters, cur, prev = [], [], None
    for rid, n in pairs:
        if prev is None or (n - prev) <= gap:
            cur.append(rid)
        else:
            clusters.append(cur); cur=[rid]
        prev = n
    if cur: clusters.append(cur)
    return clusters

def fill_section_id(text: str, meta_val: Optional[str]):
    if meta_val: return meta_val
    m = SEC_PAT.search(text or "")
    return m.group(0) if m else None

def build_prf_regex(query: str, anchor_texts: List[str], neighbor_texts: List[str], idf: Dict[str,float],
                    top_terms: int=30) -> Tuple[re.Pattern, List[str]]:
    bag = Counter()
    for s in [query] + anchor_texts + neighbor_texts:
        bag.update(tokenize(s))
    scored = []
    for t, tf in bag.items():
        w = tf * idf.get(t, 1.0)
        scored.append((w, t))
    scored.sort(reverse=True)
    terms = [t for _, t in scored[:top_terms]]
    parts = [re.escape(t) for t in terms]
    pat = re.compile(r"(" + "|".join(parts) + r")", re.I) if parts else None
    return pat, terms

def search_universal(
    index, enc, metas, id2meta, id2text, id2num,
    idf, avgdl,
    query: str,
    k_anchors: int=12,
    cand_k: int=1000,
    window: int=6,
    per_cluster_take: int=4,
    lambda_dist: float=0.02,
    kw_boost: float=0.05,
    bm25_gamma: float=0.12,
    rerank_final: bool=True,
    topk_cap: int=32,
    cluster_gap: int=3,
    print_snippets: bool=False
):
    v = enc.encode(["query: " + query], normalize_embeddings=False).astype("float32")
    faiss.normalize_L2(v)
    D, I = index.search(v, max(cand_k, k_anchors*10))
    anchors = []
    id2faiss = {}
    for s, i in zip(D[0], I[0]):
        if i == -1: continue
        mrec = metas[i].copy()
        rid = mrec["id"]
        id2faiss[rid] = float(s)
        anchors.append(mrec)
        if len(anchors) >= k_anchors: break

    anchor_ids = [a["id"] for a in anchors]
    anchor_texts = [id2text.get(rid, "") for rid in anchor_ids]
    clusters = group_by_clusters(anchor_ids, id2num, gap=cluster_gap)

    final = []
    neighbor_all_texts = []
    per_cluster_candidates = []
    for cl in clusters:
        if not cl: continue
        base_anchor = max(cl, key=lambda rid: id2faiss.get(rid, 0.0))
        base_score = id2faiss.get(base_anchor, 0.6)
        base_nums = [id2num.get(rid) for rid in cl if rid in id2num]

        nids=set()
        for bn in base_nums:
            for d in range(-window, window+1):
                nid = f"kb-{bn+d:06d}"
                if nid in id2meta: nids.add(nid)

        cand=[]
        for nid in sorted(nids, key=lambda x: id2num.get(x,0)):
            txt = id2text.get(nid,"")
            neighbor_all_texts.append(txt)
            dist = min(abs(id2num[nid]-bn) for bn in base_nums) if (nid in id2num and base_nums) else 999
            faiss_s = id2faiss.get(nid, None)  
            base = faiss_s if faiss_s is not None else base_score  
            cand.append({"id": nid, "_text": txt, "_base": float(base), "_dist": float(dist)})
        per_cluster_candidates.append(cand)

    prf_regex, prf_terms = build_prf_regex(query, anchor_texts, neighbor_all_texts, idf, top_terms=30)

    results=[]
    q_terms = tokenize(query) + prf_terms[:10] 
    for cand in per_cluster_candidates:
        scored=[]
        for c in cand:
            kw_hits = len(prf_regex.findall(c["_text"])) if prf_regex else 0
            bm25 = bm25_score(c["_text"], q_terms, idf, avgdl)
            score = c["_base"] - lambda_dist*c["_dist"] + kw_boost*kw_hits + bm25_gamma*bm25
            m = id2meta[c["id"]].copy()
            m["_text"] = c["_text"]
            m["_final"] = float(score)
            scored.append(m)
        scored.sort(key=lambda x: x["_final"], reverse=True)
        results.extend(scored[:per_cluster_take])

    if rerank_final and RERANKER and results:
        pairs = [(query, r["_text"]) for r in results]
        rr = RERANKER.compute_score(pairs, normalize=True)
        for r,s in zip(results, rr): r["_final"] = float(s)

    for r in results:
        r["section_id"] = fill_section_id(r.get("_text",""), r.get("section_id"))
    results.sort(key=lambda x: x["_final"], reverse=True)
    results = results[:topk_cap]

    for r in results:
        tags = ",".join(r.get("tags", [])) or "GENERIC"
        print(f"{r['id']}  sec={r.get('section_id')}  [{tags}]  score={r.get('_final'):.3f}")
        if print_snippets:
            txt = (r.get("_text","") or "").strip().replace("\n"," ")
            if len(txt) > 220: txt = txt[:220] + "…"
            print("   ", txt)
    return results

def main():
    ap = argparse.ArgumentParser(description="Universal preset-free retriever with PRF & neighbors")
    ap.add_argument("--index", default=INDEX)
    ap.add_argument("--meta",  default=META)
    ap.add_argument("--docs",  default=DOCS)
    ap.add_argument("--query", required=True)
    ap.add_argument("-k","--k-anchors", type=int, default=12)
    ap.add_argument("--cand-k", type=int, default=1000)
    ap.add_argument("--window", type=int, default=6)
    ap.add_argument("--per-cluster-take", type=int, default=4)
    ap.add_argument("--topk-cap", type=int, default=32)
    ap.add_argument("--gap", type=int, default=3)
    ap.add_argument("--dist-penalty", type=float, default=0.02)
    ap.add_argument("--kw-boost", type=float, default=0.05)
    ap.add_argument("--bm25-gamma", type=float, default=0.12)
    ap.add_argument("--no-rerank", action="store_true")
    ap.add_argument("--print-snippets", action="store_true")
    args = ap.parse_args()

    metas, id2meta, id2text, id2num = load_corpus(args.meta, args.docs)
    index = faiss.read_index(args.index)
    enc   = SentenceTransformer("BAAI/bge-m3")

    print("building IDF…")
    idf, avgdl = build_idf(id2text)

    search_universal(
        index=index, enc=enc, metas=metas, id2meta=id2meta, id2text=id2text, id2num=id2num,
        idf=idf, avgdl=avgdl,
        query=args.query,
        k_anchors=args.k_anchors, cand_k=args.cand_k, window=args.window,
        per_cluster_take=args.per_cluster_take, lambda_dist=args.dist_penalty,
        kw_boost=args.kw_boost, bm25_gamma=args.bm25_gamma,
        rerank_final=(not args.no_rerank),
        topk_cap=args.topk_cap, cluster_gap=args.gap,
        print_snippets=args.print_snippets
    )

if __name__ == "__main__":
    main()
