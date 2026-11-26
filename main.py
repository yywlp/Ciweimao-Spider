"""Ciweimao browser-based EPUB exporter. Run with Python 3.10+."""

import argparse
import asyncio
import os
from pathlib import Path
import sys

from playwright.async_api import async_playwright

from browser import ChapterReader, SessionExpired, apply_reader_font_size, block_purchase, discover, logged_in, normalize_font_size
from exporter import ContentError, book_id, cached_chapter, export_epub, export_txt, save_chapter, write_json


ROOT = Path(__file__).resolve().parent
PROFILE = Path(os.environ.get('LOCALAPPDATA', str(ROOT))) / 'CiweimaoImageSave' / 'EdgeProfile'


def arguments():
    parser = argparse.ArgumentParser(description='使用本人登录的 Edge 读取刺猬猫章节并导出 EPUB。')
    parser.add_argument('url', nargs='?', help='https://www.ciweimao.com/book/数字')
    parser.add_argument('--workers', type=int, default=2, choices=range(1, 5), help='并行标签页数（默认 2，最多 4）')
    parser.add_argument('--delay', type=float, default=2, help='每个标签页读取下一章前的间隔秒数（至少 1）')
    parser.add_argument('--start', type=int, default=1, help='从目录第几项开始（从 1 起算，含作品相关）')
    parser.add_argument('--limit', type=int, help='本次最多读取几章，适合先试运行')
    parser.add_argument('--output', type=Path, default=ROOT / 'output')
    parser.add_argument('--profile', type=Path, default=PROFILE, help='独立 Edge 登录目录，默认复用单章工具的登录状态')
    parser.add_argument('--no-prompt', action='store_true', help='已有登录状态时直接运行，否则退出')
    parser.add_argument('--headless', action='store_true', help='已有登录状态时在后台运行（需配合 --no-prompt）')
    parser.add_argument('--txt', action='store_true', help='同时导出 TXT（图片章节自动本地 OCR 识别）')
    parser.add_argument('--txt-only', action='store_true', help='仅导出 TXT，不生成 EPUB')
    parser.add_argument('--font-size', type=int, default=0, help='抓取图片章节时临时调大的阅读字号（12-48 偶数，如 28；0 为不修改）')
    parser.add_argument('--force-redownload-images', action='store_true', help='仅对已缓存的图片章节强制重新抓取（适合调大字号后重抓）')
    parser.add_argument('--ocr-mode', choices=('accurate', 'fast'), default='accurate', help='OCR 精度档位（默认 accurate=PP-OCRv5 Server）')
    parser.add_argument('--re-ocr', action='store_true', help='忽略已有 OCR 缓存，强制重新识别图片章节')
    parser.add_argument('--replacements', type=Path, help='额外指定的纠错词表 JSON 路径')
    args = parser.parse_args()
    if args.delay < 1 or args.start < 1 or (args.limit is not None and args.limit < 1):
        parser.error('delay 至少为 1；start 和 limit 必须为正整数。')
    if args.font_size:
        try:
            args.font_size = normalize_font_size(args.font_size)
        except ContentError as error:
            parser.error(str(error))
    if args.headless and not args.no_prompt:
        parser.error('--headless 必须配合 --no-prompt。')
    return args


async def download(context, chapters, book, folder, workers=2, delay=2, force_redownload_images=False):
    pending = asyncio.Queue()
    failures = {}
    stop = asyncio.Event()
    for chapter in chapters:
        cached = cached_chapter(folder, chapter['id'])
        if cached and not (force_redownload_images and cached['kind'] == 'image'):
            print(f'已缓存：{chapter["title"]}', flush=True)
        else:
            pending.put_nowait(chapter)
    print(f'待下载 {pending.qsize()} / {len(chapters)} 章，并行标签页 {workers} 个。', flush=True)

    async def worker():
        page = await context.new_page()
        page.set_default_timeout(25000)
        reader = ChapterReader(page)
        try:
            while not stop.is_set():
                try:
                    chapter = pending.get_nowait()
                except asyncio.QueueEmpty:
                    return
                for attempt in range(2):
                    try:
                        content = await reader.read(chapter, book['id'])
                        save_chapter(folder, chapter, **content)
                        print(f'已保存（{"图片" if "image" in content else "文字"}）：{chapter["title"]}', flush=True)
                        break
                    except SessionExpired as error:
                        failures[chapter['id']] = str(error)
                        stop.set()
                        print(str(error), flush=True)
                        break
                    except Exception as error:
                        if attempt == 0:
                            await asyncio.sleep(delay + 1)
                            continue
                        # Playwright errors can contain URLs, temporary codes and page source.
                        reason = str(error) if isinstance(error, ContentError) else f'读取失败（{type(error).__name__}），请检查网络或浏览器。'
                        failures[chapter['id']] = reason
                        print(f'未保存：{chapter["title"]}；{reason}', flush=True)
                pending.task_done()
                if not stop.is_set():
                    await asyncio.sleep(delay)
        finally:
            if reader.tasks:
                await asyncio.gather(*list(reader.tasks), return_exceptions=True)
            await page.close()

    await asyncio.gather(*(worker() for _ in range(min(workers, max(1, pending.qsize())))))
    if stop.is_set():
        for chapter in chapters:
            if not cached_chapter(folder, chapter['id']) and chapter['id'] not in failures:
                failures[chapter['id']] = '因登录或验证问题暂停，尚未读取；重新运行可续传。'
    return failures


async def run(args):
    url = args.url or input('粘贴书籍链接：').strip()
    identifier = book_id(url)
    url = f'https://www.ciweimao.com/book/{identifier}'
    folder = args.output.resolve() / identifier / 'chapters'
    folder.mkdir(parents=True, exist_ok=True)
    args.profile.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        try:
            context = await playwright.chromium.launch_persistent_context(
                str(args.profile), channel='msedge', headless=args.headless,
                viewport={'width': 1280, 'height': 900}, accept_downloads=False)
        except Exception:
            raise ContentError('无法打开 Edge。请关闭单章保存工具及使用相同登录目录的 Edge，再重试。') from None
        try:
            await context.route('https://www.ciweimao.com/**', block_purchase)
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(url, wait_until='domcontentloaded')
            if not args.no_prompt:
                print('请在工具打开的 Edge 中登录本人账号。无需填写 Cookie；登录状态仅保存在本机。')
                await asyncio.to_thread(input, '登录完成后，在此按回车开始：')
                await page.goto(url, wait_until='domcontentloaded')
            if not await logged_in(page):
                raise ContentError('尚未检测到账号登录，请运行启动脚本，在浏览器中登录后再开始。')
            book, all_chapters = await discover(page, url)
            chapters = all_chapters[args.start - 1:]
            if args.limit is not None:
                chapters = chapters[:args.limit]
            if not chapters:
                raise ContentError('所选范围内没有章节。')
            subset = len(chapters) != len(all_chapters)
            output = args.output.resolve() / identifier
            if subset:
                output = output / f'range-{args.start}-{args.start + len(chapters) - 1}'
            write_json(folder.parent / 'catalog.json', dict(book=book, chapters=all_chapters))
            print(f'《{book["title"]}》：目录共 {len(all_chapters)} 章，本次 {len(chapters)} 章。', flush=True)
            if args.font_size:
                print(f'已临时将图片抓取字号设为 {args.font_size}px（完成后自动恢复原设置）。', flush=True)
            async with apply_reader_font_size(context, args.font_size):
                failures = await download(
                    context, chapters, book, folder, args.workers, args.delay,
                    force_redownload_images=args.force_redownload_images,
                )
            write_json(output / 'failures.json', failures)
            missing = 0
            if not args.txt_only:
                path, missing = export_epub(book, chapters, folder, output, failures)
                print(f'EPUB：{path}\n成功 {len(chapters) - missing} 章；缺失 {missing} 章。', flush=True)
            if args.txt or args.txt_only:
                from ocr import make_ocr_runner
                from proofread import apply_proofread, load_replacements

                replacements = load_replacements(folder.parent, args.replacements)
                if replacements:
                    print(f'已加载纠错词表：{len(replacements)} 条替换规则。', flush=True)
                ocr_fn = make_ocr_runner(mode=args.ocr_mode, force=args.re_ocr)
                proofread_fn = lambda paras: apply_proofread(paras, replacements=replacements, normalize_punct=True)
                txt_path, txt_missing = export_txt(
                    book, chapters, folder, output, failures, ocr_fn=ocr_fn, proofread_fn=proofread_fn,
                )
                missing = max(missing, txt_missing)
                print(f'TXT：{txt_path}\n成功 {len(chapters) - txt_missing} 章；缺失 {txt_missing} 章。', flush=True)
            if missing:
                print('缺章已在输出文件中标注；修复登录或网络后，使用相同链接重新运行即可续传。')
            return 2 if missing else 0
        finally:
            await context.close()


def main():
    try:
        return asyncio.run(run(arguments()))
    except (KeyboardInterrupt, EOFError):
        print('\n已停止，已保存章节会在下次运行时复用。')
        return 130
    except Exception as error:
        if isinstance(error, ContentError):
            print(str(error))
        else:
            print(f'运行中断（{type(error).__name__}）。请检查网络和 Edge；已保存章节可续传。')
        return 1


if __name__ == '__main__':
    sys.exit(main())
