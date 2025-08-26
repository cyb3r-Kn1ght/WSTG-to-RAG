"""
Convert WSTG knowledge -> doc_clean JSONL + meta
- Bảo toàn thứ tự để tạo dải ID liên tiếp cho từng topic
- Tự nhận diện section_id WSTG-XXX-NN (nếu xuất hiện trong câu)
- Suy luận tags theo từ khóa bảo mật phổ biến
- Chunk theo ngưỡng ký tự/câu, tránh cắt ngang ý
"""

import argparse, json, re, sys
from pathlib import Path
from typing import List, Dict, Any

SECTION_RE = re.compile(r"\bWSTG(?:-v?\d+(?:\.\d+)?)?-[A-Z-]+-\d{2,}\b")

TAG_RULES = {
    "IDOR": [r"\bidor\b", r"insecure direct object reference", r"\bobject reference\b", r"direct object"],
    "AUTHZ": [r"\bauthori[sz]ation\b", r"\baccess control\b", r"\bacl\b", r"privilege", r"rbac", r"role"],
    "AUTHN": [r"\bauthenti", r"password", r"login", r"mfa", r"2fa", r"credential"],
    "SESSION": [r"\bsession\b", r"cookie", r"jsessionid", r"httponly", r"samesite", r"hsts"],
    "SQLI": [r"\bsql[\s-]?injection\b", r"\bsqli\b", r"\bselect\b", r"\binject\b.*\bsql\b"],
    "XSS": [r"\bxss\b", r"cross[-\s]?site scripting"],
    "PATH": [r"path traversal", r"directory traversal", r"\.\./", r"\b\dot[-\s]?dot\b", r"file include"],
    "CSRF": [r"\bcsrf\b", r"cross[-\s]?site request forgery"],
    "INFO": [r"information gathering", r"fingerprint", r"discovery", r"recon", r"osint"],
    "CONF": [r"configuration", r"misconfig", r"default credential", r"hardening"],
    "CRYPTO": [r"encrypt", r"decrypt", r"hash", r"rsa\b", r"aes\b", r"md5\b", r"sha[-\s]?\d"],
    "API": [r"\bapi\b", r"rest", r"graphql", r"openapi", r"swagger"],
    "CLIENT": [r"client[-\s]?side", r"javascript", r"sourcemap", r"dom\b"],
    "BUSINESS": [r"business logic", r"workflow", r"rule\b", r"maker[-\s]?checker"],
    "HTTP": [r"\bhttp\b", r"method override", r"trace\b", r"options\b", r"put\b", r"delete\b"],
    "UPLOAD": [r"file upload", r"content[-\s]?type", r"extension\b", r"mime\b"],
}

def compile_rules():
    return {tag: [re.compile(pat, re.I) for pat in pats] for tag, pats in TAG_RULES.items()}

TAG_RULES_C = compile_rules()

def infer_tags(text: str) -> List[str]:
    tags = []
    for tag, pats in TAG_RULES_C.items():
        if any(p.search(text) for p in pats):
            tags.append(tag)
    return sorted(set(tags)) or ["GENERIC"]

def norm_space(s: str) -> str:
    s = s.replace("\u00a0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\s+\n", "\n", s)
    s = re.sub(r"\n\s+", "\n", s)
    return s.strip()

def extract_section(text: str) -> str:
    m = SECTION_RE.search(text)
    return m.group(0) if m else None

def split_sentences(s: str) -> List[str]:
    parts = re.split(r"(?<=[\.\!\?])\s+", s.strip())
    parts = [p.strip() for p in parts if p.strip()]
    return parts

def load_knowledge(path: Path) -> List[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "knowledge" in data:
        arr = data["knowledge"]
    elif isinstance(data, list):
        arr = data
    else:
        raise ValueError("Định dạng knowledge không hợp lệ.")
    out = []
    for x in arr:
        if not isinstance(x, str):
            continue
        t = norm_space(x)
        if t:
            out.append(t)
    return out

def should_merge(prev_text: str, curr_text: str) -> bool:
    """
    Heuristic: nếu câu hiện tại ngắn hoặc tiếp nối cùng mục, cho phép gộp tiếp.
    """
    if len(curr_text) < 140:
        return True
    if re.match(r"^[-•\*\d]+\s", curr_text):
        return True
    return False

def build_chunks(items: List[str], max_chars=1400, min_chars=600) -> List[Dict[str, Any]]:
    """
    Duyệt tuần tự knowledge (theo thứ tự trong file) và gộp thành chunk
    - Giữ current_section khi bắt gặp WSTG-...
    - Khi vượt ngưỡng ký tự hoặc chuyển section mạnh, flush chunk
    """
    chunks = []
    buf: List[str] = []
    buf_chars = 0
    current_section = None

    def flush():
        nonlocal buf, buf_chars, current_section
        if not buf:
            return
        text = norm_space(" ".join(buf))
        chunks.append({
            "section": current_section,
            "text": text
        })
        buf = []
        buf_chars = 0

    for i, raw in enumerate(items):
        sec = extract_section(raw)
        if sec and sec != current_section and buf_chars >= min_chars:
            flush()
            current_section = sec
        elif sec and sec != current_section and not buf:
            current_section = sec

        if buf_chars and (buf_chars + 1 + len(raw) > max_chars):
            flush()

        if not buf:
            buf.append(raw)
            buf_chars = len(raw)
        else:
            if should_merge(" ".join(buf[-2:]) if len(buf) >= 2 else buf[-1], raw):
                buf.append(raw)
                buf_chars += 1 + len(raw)
            else:
                flush()
                buf.append(raw)
                buf_chars = len(raw)

    flush()
    return chunks

def attach_tags(chunks: List[Dict[str, Any]]) -> None:
    for ch in chunks:
        ch["tags"] = infer_tags(ch["text"])

def write_outputs(chunks: List[Dict[str, Any]], out_jsonl: Path, out_meta: Path, source_name: str):
    def kb_id(i: int) -> str:
        return f"kb-{i:06d}"

    with out_jsonl.open("w", encoding="utf-8") as f_doc, out_meta.open("w", encoding="utf-8") as f_meta:
        metas = []
        for i, ch in enumerate(chunks, 1):
            rid = kb_id(i)
            doc = {
                "id": rid,
                "section_id": ch.get("section"),
                "tags": ch.get("tags", ["GENERIC"]),
                "source": source_name,
                "text": ch["text"]
            }
            f_doc.write(json.dumps({
                "id": doc["id"],
                "section": doc["section_id"],
                "tags": doc["tags"],
                "source": doc["source"],
                "text": doc["text"]
            }, ensure_ascii=False) + "\n")

            metas.append({
                "id": rid,
                "section_id": ch.get("section"),
                "tags": ch.get("tags", ["GENERIC"]),
                "source": source_name,
                "text_len": len(ch["text"])
            })
        f_meta.write(json.dumps(metas, ensure_ascii=False, indent=2))

def preview(chunks: List[Dict[str, Any]], n=8):
    print(f"Total chunks: {len(chunks)}")
    for i, ch in enumerate(chunks[:n], 1):
        short = ch["text"][:120].replace("\n", " ") + ("..." if len(ch["text"]) > 120 else "")
        print(f"- #{i:03d} sec={ch.get('section')} tags={','.join(ch.get('tags', []))} | {short}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--knowledge", required=True, help="Đường dẫn file knowledge JSON (từ read_books.py)")
    ap.add_argument("--out-jsonl", default="wstg_docs_clean.jsonl", help="Đầu ra JSONL (doc_clean)")
    ap.add_argument("--out-meta", default="wstg.meta.json", help="Metadata khớp với doc_clean")
    ap.add_argument("--source", default="wstg-v4.2.pdf", help="Tên nguồn để ghi vào metadata")
    ap.add_argument("--max-chars", type=int, default=1400, help="Ngưỡng ký tự tối đa mỗi chunk")
    ap.add_argument("--min-chars", type=int, default=600, help="Ngưỡng ký tự tối thiểu trước khi cho phép flush khi đổi section")
    args = ap.parse_args()

    knowledge_path = Path(args.knowledge)
    out_jsonl = Path(args.out_jsonl)
    out_meta = Path(args.out_meta)

    if not knowledge_path.exists():
        print(f"Không thấy file: {knowledge_path}")
        sys.exit(1)

    items = load_knowledge(knowledge_path)
    if not items:
        print("Knowledge rỗng.")
        sys.exit(1)

    chunks = build_chunks(items, max_chars=args.max_chars, min_chars=args.min_chars)
    attach_tags(chunks)
    write_outputs(chunks, out_jsonl, out_meta, args.source)
    preview(chunks, n=10)
    print(f"\nĐã ghi:\n- {out_jsonl}\n- {out_meta}")

if __name__ == "__main__":
    main()
