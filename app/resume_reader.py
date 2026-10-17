from pathlib import Path

import pypdf
from docx import Document


async def read_resume_text(file_path: str) -> str:
    path = Path(file_path)
    suffix = path.suffix.lower()
    text = ""

    if suffix == ".txt":
        text = path.read_text(encoding="utf-8", errors="ignore")
    elif suffix == ".pdf":
        reader = pypdf.PdfReader(str(path))
        for page in reader.pages:
            text += page.extract_text() or ""
    elif suffix in {".doc", ".docx"}:
        doc = Document(str(path))
        for para in doc.paragraphs:
            text += para.text + "\n"
    else:
        raise ValueError(f"Unsupported resume format: {suffix}")

    return text
