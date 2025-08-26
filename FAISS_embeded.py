import json, faiss, numpy as np
from sentence_transformers import SentenceTransformer

IN_JSONL   = "wstg_docs_clean.jsonl"
OUT_INDEX  = "wstg.faiss"
OUT_META   = "wstg.meta.json"
MODEL_NAME = "BAAI/bge-m3"

# read JSONL
records = [json.loads(l) for l in open(IN_JSONL, "r", encoding="utf-8")]
texts   = ["passage: " + r["text"] for r in records]  # <— prefix passage
metas   = [{
    "id": r["id"],
    "section_id": r.get("section"),
    "tags": r.get("tags") or ["GENERIC"],
    "source": r.get("source", "wstg-v4.2.pdf")
} for r in records]

# embed
enc = SentenceTransformer(MODEL_NAME)
X = enc.encode(texts, batch_size=64, show_progress_bar=True, normalize_embeddings=False).astype("float32")
faiss.normalize_L2(X)  # dùng cosine/IP

# index FAISS
index = faiss.IndexFlatIP(X.shape[1])
index.add(X)

faiss.write_index(index, OUT_INDEX)
json.dump(metas, open(OUT_META, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("✅ Done:", OUT_INDEX, OUT_META, "vectors=", index.ntotal)
