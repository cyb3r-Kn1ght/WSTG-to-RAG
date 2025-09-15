from pathlib import Path
from typing import Dict, Any
from pydantic import BaseModel
import json
from openai import OpenAI
import fitz  # PyMuPDF
from termcolor import colored
from datetime import datetime
import shutil
import re
import os

# Configuration Constants
PDF_NAME = "wstg-v4.2.pdf"
BASE_DIR = Path("book_analysis")
PDF_DIR = BASE_DIR / "pdfs"
KNOWLEDGE_DIR = BASE_DIR / "knowledge_bases"
SUMMARIES_DIR = BASE_DIR / "summaries"
IMAGES_DIR = BASE_DIR / "images"
PDF_PATH = PDF_DIR / PDF_NAME
OUTPUT_PATH = KNOWLEDGE_DIR / f"{PDF_NAME.replace('.pdf', '_knowledge.json')}"
ANALYSIS_INTERVAL = 20
MODEL = "gpt-4o-mini"          # dùng cho lọc page-by-page
ANALYSIS_MODEL = "o1-mini"     # dùng cho phân tích cuối cùng
TEST_PAGES = None  # None = toàn bộ PDF

class PageContent(BaseModel):
    has_content: bool
    knowledge: list[str]

def load_or_create_knowledge_base() -> Dict[str, Any]:
    if Path(OUTPUT_PATH).exists():
        with open(OUTPUT_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def save_knowledge_base(knowledge_base: list[str]):
    output_path = KNOWLEDGE_DIR / f"{PDF_NAME.replace('.pdf', '')}_knowledge.json"
    print(colored(f"💾 Saving knowledge base ({len(knowledge_base)} items)...", "blue"))
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump({"knowledge": knowledge_base}, f, indent=2, ensure_ascii=False)

def extract_tables(text: str) -> str:
    """Detect simple tables and convert to markdown format."""
    lines = [l for l in text.split("\n") if l.strip()]
    if len(lines) > 1 and any("  " in l for l in lines):
        headers = re.split(r"\s{2,}", lines[0].strip())
        table = "| " + " | ".join(headers) + " |\n"
        table += "| " + " | ".join(["---"] * len(headers)) + " |\n"
        for row in lines[1:]:
            cols = re.split(r"\s{2,}", row.strip())
            table += "| " + " | ".join(cols) + " |\n"
        return table
    return text

def extract_images(page, page_num: int, doc) -> list[str]:
    """Extract images and save to IMAGES_DIR"""
    results = []
    images = page.get_images(full=True)
    for i, img in enumerate(images, start=1):
        xref = img[0]
        base_image = doc.extract_image(xref)
        img_bytes = base_image["image"]
        ext = base_image["ext"]
        img_name = f"img_page{page_num+1}_{i}.{ext}"
        img_path = IMAGES_DIR / img_name
        with open(img_path, "wb") as f:
            f.write(img_bytes)
        results.append(f"Image: {img_name}")
    return results

def detect_code_blocks(text: str) -> str:
    """Detect payload/code based on special chars/keywords."""
    if any(keyword in text.lower() for keyword in ["select ", "insert ", "delete ", "update ", "http", "payload", "script", "{", "}", ";"]):
        return f"```code\n{text.strip()}\n```"
    return text

def build_page_text(page, page_num: int, doc) -> tuple[str, list[str]]:
    """Tiền xử lý nội dung page thành 1 chuỗi text (text + table + code) và list ảnh."""
    blocks = page.get_text("dict")["blocks"]
    parts = []
    image_refs = []

    for block in blocks:
        if block["type"] == 0:  # text
            block_text = "\n".join([" ".join([span["text"] for span in line["spans"]]) for line in block["lines"]])
            if not block_text.strip():
                continue
            block_text = extract_tables(block_text)
            block_text = detect_code_blocks(block_text)
            parts.append(block_text)

        elif block["type"] == 1:  # image
            refs = extract_images(page, page_num, doc)
            image_refs.extend(refs)

    return "\n\n".join(parts), image_refs

def process_page(client: OpenAI, page, doc, current_knowledge: list[str], page_num: int) -> list[str]:
    print(colored(f"\n📖 Processing page {page_num + 1}...", "yellow"))

    page_text, image_refs = build_page_text(page, page_num, doc)

    completion = client.beta.chat.completions.parse(
        model=MODEL,
        messages=[
            {"role": "system", "content": """Analyze this page as if you're studying from a book. 
            
            SKIP if page is only:
            - TOC, index, copyright, references, acknowledgments, blank

            KEEP if page has:
            - Preface concepts
            - Educational content
            - Definitions, methodologies, frameworks
            - Payloads, code, tables
            - Key quotes/statements
            - Images (references like 'Image: ...')

            Output:
            - has_content true/false
            - knowledge: list of important points, keep code in ```code``` and tables in markdown"""},
            {"role": "user", "content": f"Page text:\n{page_text}"}
        ],
        response_format=PageContent
    )

    result = completion.choices[0].message.parsed

    # ép ảnh vào knowledge base
    if result.has_content:
        knowledge_points = result.knowledge + image_refs
        print(colored(f"✅ Found {len(knowledge_points)} knowledge points (including {len(image_refs)} images)", "green"))
        updated_knowledge = current_knowledge + knowledge_points
    else:
        if image_refs:
            print(colored(f"ℹ️ Page skipped by model, but {len(image_refs)} images kept", "cyan"))
            updated_knowledge = current_knowledge + image_refs
        else:
            print(colored("⏭️  Skipping page (no relevant content)", "yellow"))
            updated_knowledge = current_knowledge

    save_knowledge_base(updated_knowledge)
    return updated_knowledge

def load_existing_knowledge() -> list[str]:
    knowledge_file = KNOWLEDGE_DIR / f"{PDF_NAME.replace('.pdf', '')}_knowledge.json"
    if knowledge_file.exists():
        print(colored("📚 Loading existing knowledge base...", "cyan"))
        with open(knowledge_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
            print(colored(f"✅ Loaded {len(data['knowledge'])} existing knowledge points", "green"))
            return data['knowledge']
    print(colored("🆕 Starting with fresh knowledge base", "cyan"))
    return []

def analyze_knowledge_base(client: OpenAI, knowledge_base: list[str]) -> str:
    if not knowledge_base:
        print(colored("\n⚠️  Skipping analysis: No knowledge points collected", "yellow"))
        return ""
        
    print(colored("\n🤔 Generating final book analysis...", "cyan"))
    completion = client.chat.completions.create(
        model=ANALYSIS_MODEL,
        messages=[
            {"role": "user", "content": """Summarize the following knowledge in markdown.
- ## for main sections
- ### for subsections
- Bullet lists for items
- Keep code in ```code```
- Tables in markdown
- Bold for emphasis
- > for notes

Content:
""" + "\n".join(knowledge_base)}
        ]
    )
    print(colored("✨ Analysis generated successfully!", "green"))
    return completion.choices[0].message.content

def setup_directories():
    for directory in [KNOWLEDGE_DIR, SUMMARIES_DIR, IMAGES_DIR]:
        if directory.exists():
            for file in directory.glob("*"):
                file.unlink()
    for directory in [PDF_DIR, KNOWLEDGE_DIR, SUMMARIES_DIR, IMAGES_DIR]:
        directory.mkdir(parents=True, exist_ok=True)

    if not PDF_PATH.exists():
        source_pdf = Path(PDF_NAME)
        if source_pdf.exists():
            shutil.copy2(source_pdf, PDF_PATH)
            print(colored(f"📄 Copied PDF to analysis directory: {PDF_PATH}", "green"))
        else:
            raise FileNotFoundError(f"PDF file {PDF_NAME} not found")

def save_summary(summary: str, is_final: bool = False):
    if not summary:
        return
    if is_final:
        existing = list(SUMMARIES_DIR.glob(f"{PDF_NAME.replace('.pdf', '')}_final_*.md"))
        next_number = len(existing) + 1
        summary_path = SUMMARIES_DIR / f"{PDF_NAME.replace('.pdf', '')}_final_{next_number:03d}.md"
    else:
        existing = list(SUMMARIES_DIR.glob(f"{PDF_NAME.replace('.pdf', '')}_interval_*.md"))
        next_number = len(existing) + 1
        summary_path = SUMMARIES_DIR / f"{PDF_NAME.replace('.pdf', '')}_interval_{next_number:03d}.md"

    markdown_content = f"""# Book Analysis: {PDF_NAME}
Generated on: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

{summary}

---
*Analysis generated using AI Book Analysis Tool*
"""
    with open(summary_path, 'w', encoding='utf-8') as f:
        f.write(markdown_content)
    print(colored(f"✅ Saved analysis to: {summary_path}", "green"))

def main():
    try:
        print(colored("📚 Starting PDF Analysis Tool", "cyan"))
    except KeyboardInterrupt:
        print(colored("\n❌ Process cancelled by user", "red"))
        return

    setup_directories()
    client = OpenAI()
    knowledge_base = load_existing_knowledge()
    doc = fitz.open(PDF_PATH)
    pages_to_process = TEST_PAGES if TEST_PAGES else doc.page_count

    print(colored(f"\n📚 Processing {pages_to_process} pages...", "cyan"))
    for page_num in range(min(pages_to_process, doc.page_count)):
        page = doc[page_num]
        knowledge_base = process_page(client, page, doc, knowledge_base, page_num)

        if ANALYSIS_INTERVAL:
            is_interval = (page_num + 1) % ANALYSIS_INTERVAL == 0
            is_final = (page_num + 1 == pages_to_process)
            if is_interval and not is_final:
                interval_summary = analyze_knowledge_base(client, knowledge_base)
                save_summary(interval_summary, is_final=False)

        if page_num + 1 == pages_to_process:
            final_summary = analyze_knowledge_base(client, knowledge_base)
            save_summary(final_summary, is_final=True)

    print(colored("\n✨ Processing complete! ✨", "green", attrs=['bold']))

if __name__ == "__main__":
    main()
