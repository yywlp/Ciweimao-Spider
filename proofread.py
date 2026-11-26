"""Post-processing replacement dictionary and light punctuation cleanup for OCR text."""

import json
from pathlib import Path
import re

from exporter import ContentError, write_json


ROOT = Path(__file__).resolve().parent
GLOBAL_REPLACEMENTS = ROOT / "ocr_replacements.json"

DEFAULT_TEMPLATE = {
    "_说明": "键为 OCR 错字，值为正确写法；按词长优先精确替换，仅作用于图片章节 OCR 文本。",
    "示例错字": "正确写法",
}


def _read_mapping(path):
    path = Path(path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ContentError(f"纠错词表格式无效（{path.name}）：{error}") from None
    if not isinstance(data, dict):
        raise ContentError(f"纠错词表必须为 JSON 对象：{path.name}")
    return {
        str(k): str(v)
        for k, v in data.items()
        if isinstance(k, str) and isinstance(v, str) and k and not k.startswith("_") and k != "示例错字"
    }


def load_replacements(book_dir=None, extra_path=None):
    """Merge global root ocr_replacements.json, per-book replacements.json, and optional extra file."""
    merged = {}
    merged.update(_read_mapping(GLOBAL_REPLACEMENTS))
    if book_dir is not None:
        merged.update(_read_mapping(Path(book_dir) / "replacements.json"))
    if extra_path is not None:
        merged.update(_read_mapping(extra_path))
    return merged


def ensure_book_replacements_file(book_dir):
    path = Path(book_dir) / "replacements.json"
    if not path.exists():
        write_json(path, DEFAULT_TEMPLATE)
    return path


def _normalize_punctuation(text):
    # Replace ASCII ellipsis ("..." or "......") adjacent to Chinese text or line ends with "……".
    text = re.sub(r"(?<=[\u4e00-\u9fff，。！？；：、“”‘’】）])\.{3,6}", "……", text)
    text = re.sub(r"\.{3,6}(?=[\u4e00-\u9fff“”‘’【（])", "……", text)
    return text


def apply_proofread(paragraphs, replacements=None, normalize_punct=True):
    """Apply longest-first dictionary replacements and conservative punctuation cleanup."""
    if not paragraphs:
        return []
    pairs = sorted((replacements or {}).items(), key=lambda kv: len(kv[0]), reverse=True)
    result = []
    for paragraph in paragraphs:
        text = paragraph
        for wrong, right in pairs:
            if wrong in text:
                text = text.replace(wrong, right)
        if normalize_punct:
            text = _normalize_punctuation(text)
        result.append(text)
    return result
