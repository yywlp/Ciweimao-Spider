"""Offline OCR for Ciweimao image chapters using RapidOCR (PP-OCRv5 Server / Mobile)."""

import argparse
import io
import json
from pathlib import Path
import sys

import numpy as np
from PIL import Image

from exporter import ContentError, book_id, export_txt, write_json
from proofread import apply_proofread, load_replacements


ROOT = Path(__file__).resolve().parent
OCR_CACHE_VERSION = 2
_ENGINES = {}


def engine_tag_for(mode="accurate", backend="rapidocr"):
    mode = mode if mode in ("accurate", "fast") else "accurate"
    if backend == "rapidocr":
        return "ppocrv5_server_v2" if mode == "accurate" else "ppocrv5_mobile_v2"
    return "ppocrv4_mobile_legacy_v2"


def get_engine(mode="accurate"):
    """Return (engine, engine_tag) for the requested OCR quality mode."""
    mode = mode if mode in ("accurate", "fast") else "accurate"
    if mode in _ENGINES:
        return _ENGINES[mode]
    try:
        from rapidocr import EngineType, ModelType, OCRVersion, RapidOCR

        rec_type = ModelType.SERVER if mode == "accurate" else ModelType.MOBILE
        engine = RapidOCR(
            params={
                "Det.engine_type": EngineType.ONNXRUNTIME,
                "Det.model_type": ModelType.MOBILE,
                "Det.ocr_version": OCRVersion.PPOCRV5,
                "Rec.engine_type": EngineType.ONNXRUNTIME,
                "Rec.model_type": rec_type,
                "Rec.ocr_version": OCRVersion.PPOCRV5,
            }
        )
        pair = (engine, engine_tag_for(mode, "rapidocr"))
        _ENGINES[mode] = pair
        return pair
    except ImportError:
        pass

    try:
        from rapidocr_onnxruntime import RapidOCR as LegacyRapidOCR

        engine = LegacyRapidOCR()
        pair = (engine, engine_tag_for(mode, "legacy"))
        _ENGINES[mode] = pair
        return pair
    except ImportError:
        raise ContentError(
            "缺少 OCR 依赖 rapidocr，请先执行：python -m pip install -r requirements.txt"
        ) from None


def preprocess_image(image):
    """Gently remove faint watermarks while preserving anti-aliased strokes of small text."""
    gray = np.array(image.convert("L"), dtype=np.uint8)
    bg = float(np.median(gray))
    if bg < 128.0:
        gray = (255 - gray).astype(np.uint8)
        bg = 255.0 - bg
    # Watermarks sit near bg-20..bg-50; keep strokes < cutoff intact and slightly darkened.
    cutoff = max(130.0, bg - 52.0)
    mask = gray < cutoff
    if not np.any(mask):
        return np.full_like(gray, 255)
    cleaned = np.full_like(gray, 255)
    cleaned[mask] = np.clip(gray[mask].astype(np.int32) - 40, 0, 255).astype(np.uint8)
    return cleaned


def split_into_chunks(gray, max_height=900, pad=16):
    """Split a tall grayscale image on blank horizontal rows so no line is cut."""
    height, _width = gray.shape
    if height <= max_height:
        padded = np.pad(gray, ((pad, pad), (0, 0)), mode="constant", constant_values=255)
        return [(0, pad, padded)]
    blank = gray.min(axis=1) >= 250
    chunks = []
    start = 0
    while start < height:
        end = min(height, start + max_height)
        if end < height:
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


def _extract_ocr_boxes(raw_output):
    if hasattr(raw_output, "boxes") and hasattr(raw_output, "txts"):
        if raw_output.boxes is None or not raw_output.txts:
            return []
        return [(pts, text) for pts, text in zip(raw_output.boxes, raw_output.txts) if text]
    if isinstance(raw_output, tuple) and raw_output:
        result = raw_output[0]
        if not result:
            return []
        return [(entry[0], entry[1]) for entry in result if len(entry) >= 2 and entry[1]]
    return []


def merge_lines_to_paragraphs(boxes):
    """Reconstruct Chinese novel paragraphs from OCR bounding boxes (x1, y1, x2, y2, text)."""
    items = [
        (float(x1), float(y1), float(x2), float(y2), text.strip())
        for x1, y1, x2, y2, text in boxes
        if text and text.strip()
    ]
    if not items:
        return []
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


def recognize_image(source, engine=None, mode="accurate"):
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

    ocr_engine = engine if engine is not None else get_engine(mode)[0]
    all_boxes = []
    for y_offset, pad, chunk_gray in split_into_chunks(cleaned):
        if chunk_gray.min() >= 250:
            continue
        rgb = np.stack([chunk_gray, chunk_gray, chunk_gray], axis=-1)
        for pts, text in _extract_ocr_boxes(ocr_engine(rgb)):
            xs = [float(p[0]) for p in pts]
            ys = [float(p[1]) - pad + y_offset for p in pts]
            all_boxes.append((min(xs), min(ys), max(xs), max(ys), text))
    paragraphs = merge_lines_to_paragraphs(all_boxes)
    if not paragraphs:
        raise ContentError("OCR 未识别到有效正文。")
    return paragraphs


def cached_ocr(folder, identifier, image_sha256, engine_tag=None):
    try:
        record = json.loads((Path(folder) / f"{identifier}.ocr.json").read_text(encoding="utf-8"))
        if (
            record.get("version") == OCR_CACHE_VERSION
            and record.get("id") == identifier
            and record.get("image_sha256") == image_sha256
            and (engine_tag is None or record.get("engine_tag") == engine_tag)
            and isinstance(record.get("paragraphs"), list)
            and any(isinstance(p, str) and p.strip() for p in record["paragraphs"])
        ):
            return record["paragraphs"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def save_ocr(folder, identifier, image_sha256, paragraphs, engine_tag="ppocrv5_server_v2"):
    record = {
        "version": OCR_CACHE_VERSION,
        "id": identifier,
        "image_sha256": image_sha256,
        "engine_tag": engine_tag,
        "paragraphs": paragraphs,
    }
    write_json(Path(folder) / f"{identifier}.ocr.json", record)
    return record


def chapter_paragraphs(folder, record, engine=None, engine_tag=None, mode="accurate", force=False, progress=None):
    """Return paragraphs for a cached text or image chapter (with model-aware OCR caching)."""
    if record["kind"] == "text":
        return record["paragraphs"]
    identifier = record["id"]
    sha = record["sha256"]
    if not force:
        cached = cached_ocr(folder, identifier, sha, engine_tag=engine_tag)
        if cached is not None:
            return cached
    if progress:
        progress(record)
    if engine is None:
        engine, engine_tag = get_engine(mode)
    elif engine_tag is None:
        engine_tag = engine_tag_for(mode)
    paragraphs = recognize_image(Path(folder) / f"{identifier}.png", engine=engine, mode=mode)
    save_ocr(folder, identifier, sha, paragraphs, engine_tag=engine_tag)
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


def make_ocr_runner(mode="accurate", force=False):
    engine = None
    tag = None

    def ocr_fn(ch_folder, record):
        nonlocal engine, tag
        if record["kind"] == "image":
            # Peek cache before initializing the heavy OCR model if we can match expected tag.
            expected_tag = tag or engine_tag_for(mode, "rapidocr")
            if not force and cached_ocr(ch_folder, record["id"], record["sha256"], engine_tag=expected_tag) is not None:
                return cached_ocr(ch_folder, record["id"], record["sha256"], engine_tag=expected_tag)
            if engine is None:
                label = "高精度 PP-OCRv5 Server" if mode == "accurate" else "快速 Mobile"
                print(f"正在初始化本地 OCR 引擎（{label}）...", flush=True)
                engine, tag = get_engine(mode)
            if force or cached_ocr(ch_folder, record["id"], record["sha256"], engine_tag=tag) is None:
                print(f"正在 OCR 识别：{record['title']}", flush=True)
        return chapter_paragraphs(ch_folder, record, engine=engine, engine_tag=tag, mode=mode, force=force)

    return ocr_fn


def main():
    parser = argparse.ArgumentParser(description="将已下载的刺猬猫书籍（含图片章节自动 OCR 与词表校对）导出为 TXT。")
    parser.add_argument("target", nargs="?", help="书籍 ID、书籍链接，或 output\\书籍ID 目录路径")
    parser.add_argument("--start", type=int, default=1, help="从目录第几项开始（默认 1）")
    parser.add_argument("--limit", type=int, help="最多导出几章")
    parser.add_argument("--output", type=Path, default=ROOT / "output", help="默认输出根目录")
    parser.add_argument("--ocr-mode", choices=("accurate", "fast"), default="accurate", help="OCR 精度档位（默认 accurate=PP-OCRv5 Server）")
    parser.add_argument("--re-ocr", action="store_true", help="忽略已有 OCR 缓存，强制重新识别所有图片章节")
    parser.add_argument("--replacements", type=Path, help="额外指定的纠错词表 JSON 路径")
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
        replacements = load_replacements(book_dir, args.replacements)
        if replacements:
            print(f"已加载纠错词表：{len(replacements)} 条替换规则。", flush=True)
        ocr_fn = make_ocr_runner(mode=args.ocr_mode, force=args.re_ocr)
        proofread_fn = lambda paras: apply_proofread(paras, replacements=replacements, normalize_punct=True)
        path, missing = export_txt(book, chapters, folder, dest_dir, failures, ocr_fn=ocr_fn, proofread_fn=proofread_fn)
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
