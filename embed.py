import json, faiss, numpy as np
from sentence_transformers import SentenceTransformer

# 1) Đọc chunks
texts, ids = [], []
with open("wstg_chunks.from_knowledge.jsonl", "r", encoding="utf-8") as f:
    for line in f:
        obj = json.loads(line)
        texts.append(obj["text"])
        ids.append(obj["id"])

# 2) Embed (ví dụ multilingual-e5-large)
model = SentenceTransformer("intfloat/multilingual-e5-large")
embs = model.encode(texts, batch_size=32, normalize_embeddings=True)

# 3) Lưu FAISS + mapping
index = faiss.IndexFlatIP(embs.shape[1])
index.add(np.asarray(embs, dtype="float32"))
faiss.write_index(index, "wstg_faiss.index")
with open("wstg_faiss_ids.json", "w", encoding="utf-8") as f:
    json.dump(ids, f, ensure_ascii=False, indent=2)
