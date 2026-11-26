"""Local chapter cache and EPUB 3 packaging. No browser credentials are stored here."""

from datetime import datetime, timezone
import hashlib
from html import escape
import io
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
from zipfile import ZipFile, ZIP_DEFLATED, ZIP_STORED

from PIL import Image


class ContentError(ValueError):
    pass


def book_id(url):
    parsed = urlsplit(url)
    match = re.fullmatch(r"/book/(\d+)/?", parsed.path)
    if parsed.scheme != "https" or parsed.hostname != "www.ciweimao.com" or not match:
        raise ContentError("请填写 https://www.ciweimao.com/book/数字 格式的书籍链接。")
    return match[1]


def safe_name(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")[:100]
    if not name or name.upper().split(".")[0] in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)], *[f"LPT{i}" for i in range(1, 10)]}:
        return "book_" + name
    return name


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def write_json(path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))


def validate_image(data, displayed_width=0, displayed_height=0):
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            if image.width < 200 or image.height <= 100:
                raise ContentError("正文图片过小，可能是错误提示图。")
            if displayed_width > 0 and displayed_height > 100:
                height = image.height * displayed_width / image.width
                if height < displayed_height * 0.85:
                    raise ContentError("图片高度小于页面正文，可能不完整。")
            output = io.BytesIO()
            image.convert("RGB").save(output, "PNG")
            return output.getvalue()
    except ContentError:
        raise
    except Exception:
        raise ContentError("正文图片无法解码。") from None


def save_chapter(folder, chapter, *, text=None, image=None):
    identifier = chapter["id"]
    if not re.fullmatch(r"\d+", identifier):
        raise ContentError("无效的章节编号。")
    record = {"version": 1, "id": identifier, "title": chapter["title"]}
    if image is not None:
        image = validate_image(image)
        atomic_write(folder / f"{identifier}.png", image)
        record.update(kind="image", sha256=hashlib.sha256(image).hexdigest())
    elif text and all(isinstance(p, str) for p in text) and any(p.strip() for p in text):
        record.update(kind="text", paragraphs=text)
        record["sha256"] = hashlib.sha256(json.dumps(text, ensure_ascii=False).encode()).hexdigest()
    else:
        raise ContentError("没有可保存的正文。")
    write_json(folder / f"{identifier}.json", record)
    return record


def cached_chapter(folder, identifier):
    try:
        record = json.loads((folder / f"{identifier}.json").read_text(encoding="utf-8"))
        if record.get("version") != 1 or record.get("id") != identifier:
            return None
        if record["kind"] == "image":
            data = (folder / f"{identifier}.png").read_bytes()
            validate_image(data)
        elif record["kind"] == "text" and record.get("paragraphs"):
            data = json.dumps(record["paragraphs"], ensure_ascii=False).encode()
        else:
            return None
        return record if hashlib.sha256(data).hexdigest() == record.get("sha256") else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def xhtml(title, body):
    return ('<?xml version="1.0" encoding="utf-8"?>'
            '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="zh-CN" xml:lang="zh-CN">'
            f'<head><title>{escape(title)}</title><link rel="stylesheet" type="text/css" href="style.css"/></head>'
            f'<body>{body}</body></html>')


def export_epub(book, chapters, folder, output, failures=None):
    """Include a visible placeholder for every missing chapter; never silently drop one."""
    failures = failures or {}
    records = [cached_chapter(folder, c["id"]) for c in chapters]
    missing = sum(r is None for r in records)
    label = "-未完整" if missing else ""
    destination = output / f'{safe_name(book["title"])}-{book["id"]}{label}.epub'
    temporary = destination.with_suffix(".epub.tmp")
    output.mkdir(parents=True, exist_ok=True)
    manifest = ['<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
                '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
                '<item id="css" href="style.css" media-type="text/css"/>']
    spine, navigation, ncx = [], [], []
    uid = f'urn:ciweimao:book:{book["id"]}'
    with ZipFile(temporary, "w", ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=ZIP_STORED)
        archive.writestr("META-INF/container.xml", '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        archive.writestr("OEBPS/style.css", 'body{line-height:1.7}p{text-indent:2em}img{max-width:100%;height:auto}.image{text-align:center;text-indent:0;margin:0;break-inside:avoid;page-break-inside:avoid}.missing{color:#a22}')
        for index, (chapter, record) in enumerate(zip(chapters, records), 1):
            title = chapter["title"]
            filename = f"chapter-{chapter['id']}.xhtml"
            body = f'<h1>{escape(title)}</h1>'
            if record is None:
                reason = failures.get(chapter["id"], "尚未下载成功，请重新运行以续传。")
                body += f'<p class="missing">【正文缺失】{escape(reason)}</p>'
            elif record["kind"] == "text":
                body += "".join(f'<p>{escape(p)}</p>' for p in record["paragraphs"])
            else:
                with Image.open(folder / f'{chapter["id"]}.png') as image:
                    # Keep the original intact; smaller EPUB slices work in more readers.
                    slice_height = max(400, round(image.width * 1.5))
                    for part, top in enumerate(range(0, image.height, slice_height), 1):
                        name = f'images/{chapter["id"]}-{part}.png'
                        buffer = io.BytesIO()
                        image.crop((0, top, image.width, min(top + slice_height, image.height))).save(buffer, "PNG")
                        archive.writestr(f"OEBPS/{name}", buffer.getvalue())
                        manifest.append(f'<item id="img-{index}-{part}" href="{name}" media-type="image/png"/>')
                        body += f'<div class="image"><img src="{name}" alt="{escape(title)}（{part}）"/></div>'
            archive.writestr(f"OEBPS/{filename}", xhtml(title, body))
            manifest.append(f'<item id="c{index}" href="{filename}" media-type="application/xhtml+xml"/>')
            spine.append(f'<itemref idref="c{index}"/>')
            navigation.append(f'<li><a href="{filename}">{escape(title)}</a></li>')
            ncx.append(f'<navPoint id="n{index}" playOrder="{index}"><navLabel><text>{escape(title)}</text></navLabel><content src="{filename}"/></navPoint>')
        archive.writestr("OEBPS/nav.xhtml", xhtml(book["title"], '<nav epub:type="toc" id="toc"><h1>目录</h1><ol>' + "".join(navigation) + '</ol></nav>'))
        archive.writestr("OEBPS/toc.ncx", '<?xml version="1.0" encoding="utf-8"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1"><head>' + f'<meta name="dtb:uid" content="{uid}"/></head><docTitle><text>{escape(book["title"])}</text></docTitle><navMap>' + "".join(ncx) + '</navMap></ncx>')
        modified = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        metadata = f'<dc:identifier id="uid">{uid}</dc:identifier><dc:title>{escape(book["title"])}</dc:title><dc:creator>{escape(book.get("author", ""))}</dc:creator><dc:language>zh-CN</dc:language><meta property="dcterms:modified">{modified}</meta>'
        archive.writestr("OEBPS/content.opf", '<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" xmlns:dc="http://purl.org/dc/elements/1.1/" version="3.0" unique-identifier="uid"><metadata>' + metadata + '</metadata><manifest>' + "".join(manifest) + '</manifest><spine toc="ncx">' + "".join(spine) + '</spine></package>')
    temporary.replace(destination)
    return destination, missing


def export_txt(book, chapters, folder, output, failures=None, ocr_fn=None):
    """Export chapters to UTF-8 TXT; run ocr_fn for image chapters and mark missing ones."""
    failures = failures or {}
    records = [cached_chapter(folder, c["id"]) for c in chapters]
    lines = [f'《{book["title"]}》']
    if book.get("author"):
        lines.append(f'作者：{book["author"]}')
    lines.append("")
    current_volume = ""
    missing = 0
    for chapter, record in zip(chapters, records):
        volume = chapter.get("volume") or ""
        if volume and volume != current_volume:
            current_volume = volume
            lines.append(f"=== {volume} ===")
            lines.append("")
        lines.append(chapter["title"])
        lines.append("")
        if record is None:
            missing += 1
            reason = failures.get(chapter["id"], "尚未下载成功，请重新运行以续传。")
            lines.append(f"【正文缺失】{reason}")
            lines.append("")
            continue
        paragraphs = None
        if record["kind"] == "text":
            paragraphs = record["paragraphs"]
        elif ocr_fn is not None:
            try:
                paragraphs = ocr_fn(folder, record)
            except ContentError as error:
                missing += 1
                lines.append(f"【OCR 失败】{error}")
                lines.append("")
                continue
        else:
            missing += 1
            lines.append("【图片章节未识别】请使用 --txt 或 ocr.py 进行 OCR 识别。")
            lines.append("")
            continue
        if not paragraphs:
            missing += 1
            lines.append("【正文缺失】未提取到有效段落。")
            lines.append("")
            continue
        for paragraph in paragraphs:
            lines.append(f"　　{paragraph}")
        lines.append("")
    label = "-未完整" if missing else ""
    destination = Path(output) / f'{safe_name(book["title"])}-{book["id"]}{label}.txt'
    atomic_write(destination, "\n".join(lines).encode("utf-8"))
    return destination, missing
