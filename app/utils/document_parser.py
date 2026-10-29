import io
import base64
from typing import Dict, Any, List, Optional

def extract_text_from_attachment(att: Dict[str, Any]) -> str:
    """
    Extracts plain text content from an attachment dictionary.
    Handles raw contentText or base64 encoded PDF, DOCX, TXT, CSV, MD, JSON.
    """
    if not isinstance(att, dict):
        return ""

    # If text is already extracted on client
    if att.get("contentText"):
        return str(att["contentText"]).strip()

    raw_content = att.get("content") or att.get("base64")
    if not raw_content:
        return ""

    filename = (att.get("filename") or att.get("name") or "").lower()

    # If content is a base64 string
    binary_data: Optional[bytes] = None
    if isinstance(raw_content, str):
        # Strip data URL prefix if present e.g. "data:application/pdf;base64,"
        if "," in raw_content:
            raw_content = raw_content.split(",", 1)[1]
        try:
            binary_data = base64.b64decode(raw_content)
        except Exception as e:
            print(f"[WARN] Failed to b64decode attachment: {e}")
            return raw_content  # Fallback to literal string

    if not binary_data:
        return ""

    # PDF Parsing
    if filename.endswith(".pdf"):
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(binary_data))
            extracted_pages = []
            for i, page in enumerate(reader.pages):
                text = page.extract_text()
                if text:
                    extracted_pages.append(f"--- Page {i+1} ---\n{text}")
            return "\n\n".join(extracted_pages)
        except Exception as e:
            print(f"[WARN] Failed to extract PDF text from {filename}: {e}")
            return ""

    # DOCX Parsing
    elif filename.endswith(".docx") or filename.endswith(".doc"):
        try:
            import docx
            doc = docx.Document(io.BytesIO(binary_data))
            paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
            for table in doc.tables:
                for row in table.rows:
                    row_text = " | ".join(cell.text.strip() for cell in row.cells if cell.text.strip())
                    if row_text:
                        paragraphs.append(row_text)
            return "\n".join(paragraphs)
        except Exception as e:
            print(f"[WARN] Failed to extract DOCX text from {filename}: {e}")
            return ""

    # Plain text / CSV / JSON / Markdown
    else:
        try:
            return binary_data.decode("utf-8")
        except UnicodeDecodeError:
            try:
                return binary_data.decode("latin-1")
            except Exception as e:
                print(f"[WARN] Could not decode text file {filename}: {e}")
                return ""
