"""Offline OCR for Ciweimao image chapters using RapidOCR (ONNX Runtime)."""

import argparse
import io
import json
from pathlib import Path
import sys

import numpy as np
from PIL import Image

from exporter import ContentError, book_id, export_txt, write_json


ROOT = Path(__file__).resolve().parent
_ENGINE = None


def get_engine():
    global _ENGINE
    if _ENGINE is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError:
            raise ContentError(
                "缺少 OCR 依赖 rapidocr-onnxruntime，请先执行：python -m pip install -r requirements.txt"
            ) from None
        _ENGINE = RapidOCR()
    return _ENGINE


def preprocess_image(image):
    """Remove faint background/inline watermarks and normalize dark text onto white."""
    gray = np.array(image.convert("L"), dtype=np.uint8)
    bg = float(np.median(gray))
    if bg < 128.0:
        gray = (255 - gray).astype(np.uint8)
        bg = 255.0 - bg
    # Ciweimao watermarks sit near bg-20..bg-50, while real text is much darker.
    cutoff = max(120.0, bg - 60.0)
    mask = gray < cutoff
    if not np.any(mask):
        return np.full_like(gray, 255)
    dark = float(np.percentile(gray[mask], 5))
    span = max(1.0, cutoff - dark)
    stretched = (gray.astype(np.float32) - dark) * (255.0 / span)
    cleaned = np.clip(stretched, 0, 255).astype(np.uint8)
    cleaned[~mask] = 255
    return cleaned


def split_into_chunks(gray, max_height=900, pad=16):
    """Split a tall grayscale image on blank horizontal rows so no line is cut."""
    height, width = gray.shape
    if height <= max_height:
        padded = np.pad(gray, ((pad, pad), (0, 0)), mode="constant", constant_values=255)
        return [(0, pad, padded)]
    blank = gray.min(axis=1) >= 250
    chunks = []
    start = 0
    while start < height:
        end = min(height, start + max_height)
        if end < height:
            # Walk backward from `end` to find a blank row between lines.
            cut = end
            min_cut = start + max(100, max_height // 3)
            while cut > min_cut and not blank[cut]:
                cut -= 1
            if blank[cut]:
                end = cut
        segment = gray[start:end, :]
        padded = np.pad(segment, ((pad, pad), (0, 0)), mode="constant", constant_values=255)
        chunks.append((start, pad, padded))
        start = end
    return chunks


def merge_lines_to_paragraphs(boxes):
    """Reconstruct Chinese novel paragraphs from OCR bounding boxes (x1, y1, x2, y2, text)."""
    items = [(float(x1), float(y1), float(x2), float(y2), text.strip()) for x1, y1, x2, y2, text in boxes if text and text.strip()]
    if not items:
        return []
    # Sort top-to-bottom first, then group boxes belonging to the same visual line.
    items.sort(key=lambda b: ((b[1] + b[3]) / 2.0, b[0]))
    lines = []
    for box in items:
        x1, y1, x2, y2, text = box
        yc = (y1 + y2) / 2.0
        h = max(1.0, y2 - y1)
        if lines:
            prev = lines[-1]
            prev_yc = (prev["y1"] + prev["y2"]) / 2.0
            prev_h = max(1.0, prev["y2"] - prev["y1"])
            if abs(yc - prev_yc) < 0.55 * min(h, prev_h):
                prev["parts"].append((x1, x2, text))
                prev["x1"] = min(prev["x1"], x1)
                prev["x2"] = max(prev["x2"], x2)
                prev["y1"] = min(prev["y1"], y1)
                prev["y2"] = max(prev["y2"], y2)
                continue
        lines.append({"x1": x1, "y1": y1, "x2": x2, "y2": y2, "parts": [(x1, x2, text)]})

    for line in lines:
        line["parts"].sort(key=lambda p: p[0])
        line["text"] = "".join(p[2] for p in line["parts"]).strip()
    lines = [line for line in lines if line["text"]]
    if not lines:
        return []

    line_heights = [max(1.0, line["y2"] - line["y1"]) for line in lines]
    line_h = float(np.median(line_heights))
    min_x = min(line["x1"] for line in lines)
    max_x1 = max(line["x1"] for line in lines)
    max_x2 = max(line["x2"] for line in lines)
    has_indent_contrast = (max_x1 - min_x) >= 0.8 * line_h
    indent_threshold = min_x + 0.8 * line_h

    paragraphs = [lines[0]["text"]]
    for prev, cur in zip(lines, lines[1:]):
        gap = cur["y1"] - prev["y2"]
        is_indented = has_indent_contrast and cur["x1"] >= indent_threshold
        large_gap = gap > 1.15 * line_h
        prev_ended_short = (max_x2 - prev["x2"]) > 2.5 * line_h
        no_wraps = not has_indent_contrast
        if is_indented or large_gap or prev_ended_short or no_wraps:
            paragraphs.append(cur["text"])
        else:
            paragraphs[-1] += cur["text"]
    return paragraphs


def recognize_image(source, engine=None):
    """Run offline OCR on a chapter image (Path, str, bytes, or PIL.Image)."""
    if isinstance(source, Image.Image):
        image = source
    elif isinstance(source, (bytes, bytearray)):
        image = Image.open(io.BytesIO(source))
    else:
        image = Image.open(source)
    try:
        cleaned = preprocess_image(image)
    finally:
        if not isinstance(source, Image.Image):
            image.close()

    ocr_engine = engine or get_engine()
    all_boxes = []
    for y_offset, pad, chunk_gray in split_into_chunks(cleaned):
        if chunk_gray.min() >= 250:
            continue
        rgb = np.stack([chunk_gray, chunk_gray, chunk_gray], axis=-1)
        result, _ = ocr_engine(rgb)
        if not result:
            continue
        for entry in result:
            pts, text = entry[0], entry[1]
            xs = [p[0] for p in pts]
            ys = [p[1] - pad + y_offset for p in pts]
            all_boxes.append((min(xs), min(ys), max(xs), max(ys), text))
    paragraphs = merge_lines_to_paragraphs(all_boxes)
    if not paragraphs:
        raise ContentError("OCR 未识别到有效正文。")
    return paragraphs


def cached_ocr(folder, identifier, image_sha256):
    try:
        record = json.loads((Path(folder) / f"{identifier}.ocr.json").read_text(encoding="utf-8"))
        if (record.get("version") == 1
                and record.get("id") == identifier
                and record.get("image_sha256") == image_sha256
                and isinstance(record.get("paragraphs"), list)
                and any(isinstance(p, str) and p.strip() for p in record["paragraphs"])):
            return record["paragraphs"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def save_ocr(folder, identifier, image_sha256, paragraphs):
    record = {
        "version": 1,
        "id": identifier,
        "image_sha256": image_sha256,
        "paragraphs": paragraphs,
    }
    write_json(Path(folder) / f"{identifier}.ocr.json", record)
    return record


def chapter_paragraphs(folder, record, engine=None, progress=None):
    """Return paragraphs for a cached text or image chapter (with OCR caching)."""
    if record["kind"] == "text":
        return record["paragraphs"]
    identifier = record["id"]
    sha = record["sha256"]
    cached = cached_ocr(folder, identifier, sha)
    if cached is not None:
        return cached
    if progress:
        progress(record)
    paragraphs = recognize_image(Path(folder) / f"{identifier}.png", engine=engine)
    save_ocr(folder, identifier, sha, paragraphs)
    return paragraphs


def resolve_book_dir(target, output_root):
    path = Path(target)
    if (path / "catalog.json").exists():
        return path.resolve()
    if target.isdigit():
        candidate = output_root / target
        if (candidate / "catalog.json").exists():
            return candidate.resolve()
    if target.startswith("http"):
        candidate = output_root / book_id(target)
        if (candidate / "catalog.json").exists():
            return candidate.resolve()
    raise ContentError(f"未找到书籍目录或 catalog.json：{target}")


def main():
    parser = argparse.ArgumentParser(description="将已下载的刺猬猫书籍（含图片章节自动 OCR）导出为 TXT。")
    parser.add_argument("target", nargs="?", help="书籍 ID、书籍链接，或 output\\书籍ID 目录路径")
    parser.add_argument("--start", type=int, default=1, help="从目录第几项开始（默认 1）")
    parser.add_argument("--limit", type=int, help="最多导出几章")
    parser.add_argument("--output", type=Path, default=ROOT / "output", help="默认输出根目录")
    args = parser.parse_args()
    if args.start < 1 or (args.limit is not None and args.limit < 1):
        parser.error("start 和 limit 必须为正整数。")
    try:
        target = args.target or input("输入书籍 ID、链接或 output\\书籍ID 路径：").strip()
        book_dir = resolve_book_dir(target, args.output.resolve())
        catalog = json.loads((book_dir / "catalog.json").read_text(encoding="utf-8"))
        book = catalog["book"]
        all_chapters = catalog["chapters"]
        chapters = all_chapters[args.start - 1:]
        if args.limit is not None:
            chapters = chapters[:args.limit]
        if not chapters:
            raise ContentError("所选范围内没有章节。")
        subset = len(chapters) != len(all_chapters)
        dest_dir = book_dir / f"range-{args.start}-{args.start + len(chapters) - 1}" if subset else book_dir
        failures_path = dest_dir / "failures.json"
        failures = json.loads(failures_path.read_text(encoding="utf-8")) if failures_path.exists() else {}
        folder = book_dir / "chapters"
        engine = None

        def ocr_fn(ch_folder, record):
            nonlocal engine
            if record["kind"] == "image" and cached_ocr(ch_folder, record["id"], record["sha256"]) is None:
                if engine is None:
                    print("正在初始化本地 OCR 引擎（RapidOCR）...", flush=True)
                    engine = get_engine()
                print(f"正在 OCR 识别：{record['title']}", flush=True)
            return chapter_paragraphs(ch_folder, record, engine=engine)

        path, missing = export_txt(book, chapters, folder, dest_dir, failures, ocr_fn=ocr_fn)
        print(f"TXT：{path}\n成功 {len(chapters) - missing} 章；缺失 {missing} 章。", flush=True)
        return 2 if missing else 0
    except (KeyboardInterrupt, EOFError):
        print("\n已停止，已识别的 OCR 缓存会在下次运行时复用。")
        return 130
    except ContentError as error:
        print(str(error))
        return 1


if __name__ == "__main__":
    sys.exit(main())
