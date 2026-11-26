import asyncio
import io
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET
from zipfile import ZipFile, ZIP_STORED

from PIL import Image
from playwright.async_api import async_playwright

from browser import ChapterReader, SessionExpired, discover
from exporter import ContentError, book_id, cached_chapter, export_epub, export_txt, save_chapter, validate_image
from gui import build_command, normalize_target, parse_chapter_range
from main import download
from ocr import cached_ocr, merge_lines_to_paragraphs, preprocess_image, save_ocr, split_into_chunks


def picture(width=800, height=3200):
    output = io.BytesIO()
    Image.new('RGB', (width, height), 'white').save(output, 'PNG')
    return output.getvalue()


BOOK = {'id': '100', 'title': '书名 & 测试', 'author': '作者'}
CHAPTERS = [dict(id=str(n), title=f'章节 {n}', url=f'https://www.ciweimao.com/chapter/{n}') for n in range(1, 5)]


class ExportTests(unittest.TestCase):
    def test_url_boundary(self):
        self.assertEqual(book_id('https://www.ciweimao.com/book/100?foo=bar'), '100')
        for url in ('http://www.ciweimao.com/book/100', 'https://evil.com/book/100', 'https://www.ciweimao.com/chapter/100'):
            with self.assertRaises(ContentError):
                book_id(url)

    def test_image_rejects_error_and_truncation(self):
        for data, width, height in ((picture(800, 30), 0, 0), (b'not an image', 0, 0), (picture(800, 300), 800, 3000)):
            with self.assertRaises(ContentError):
                validate_image(data, width, height)
        self.assertTrue(validate_image(picture(), 800, 3200))

    def test_cache_detects_corruption(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            save_chapter(folder, CHAPTERS[0], text=['测试正文'])
            self.assertIsNotNone(cached_chapter(folder, '1'))
            record = json.loads((folder / '1.json').read_text(encoding='utf-8'))
            record['paragraphs'] = ['被篡改']
            (folder / '1.json').write_text(json.dumps(record), encoding='utf-8')
            self.assertIsNone(cached_chapter(folder, '1'))
            save_chapter(folder, CHAPTERS[1], image=picture())
            (folder / '2.png').write_bytes(picture(height=2000))
            self.assertIsNone(cached_chapter(folder, '2'))

    def test_epub_order_missing_chapters_xml_and_resources(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / 'chapters'
            save_chapter(folder, CHAPTERS[1], image=picture())
            save_chapter(folder, CHAPTERS[0], text=['甲<&>乙', '第二段'])
            path, missing = export_epub(BOOK, CHAPTERS[:3], folder, Path(directory), {'3': '未购买'})
            self.assertEqual(missing, 1)
            self.assertIn('未完整', path.name)
            with ZipFile(path) as archive:
                self.assertIsNone(archive.testzip())
                self.assertEqual(archive.infolist()[0].filename, 'mimetype')
                self.assertEqual(archive.infolist()[0].compress_type, ZIP_STORED)
                self.assertEqual(archive.read('mimetype'), b'application/epub+zip')
                for name in archive.namelist():
                    if name.endswith(('.xml', '.opf', '.ncx', '.xhtml')):
                        ET.fromstring(archive.read(name))
                opf = ET.fromstring(archive.read('OEBPS/content.opf'))
                ns = {'o': 'http://www.idpf.org/2007/opf'}
                items = {i.attrib['id']: i.attrib['href'] for i in opf.findall('o:manifest/o:item', ns)}
                self.assertTrue(all('OEBPS/' + href in archive.namelist() for href in items.values()))
                self.assertEqual([items[i.attrib['idref']] for i in opf.findall('o:spine/o:itemref', ns)], ['chapter-1.xhtml', 'chapter-2.xhtml', 'chapter-3.xhtml'])
                self.assertIn('甲&lt;&amp;&gt;乙', archive.read('OEBPS/chapter-1.xhtml').decode())
                self.assertIn('未购买', archive.read('OEBPS/chapter-3.xhtml').decode())
                parts = [name for name in archive.namelist() if name.startswith('OEBPS/images/')]
                self.assertEqual(len(parts), 3)
                total_height = 0
                for name in parts:
                    with Image.open(io.BytesIO(archive.read(name))) as image:
                        total_height += image.height
                self.assertEqual(total_height, 3200)

    def test_ocr_preprocess_removes_watermark_and_handles_dark_mode(self):
        import numpy as np
        # Light beige background (240), watermark (205), dark text (65).
        arr = np.full((60, 120), 240, dtype=np.uint8)
        arr[10:20, 10:50] = 65
        arr[30:40, 10:50] = 205
        cleaned = preprocess_image(Image.fromarray(arr, mode='L'))
        self.assertLess(int(cleaned[15, 25]), 40)
        self.assertEqual(int(cleaned[35, 25]), 255)
        self.assertEqual(int(cleaned[5, 5]), 255)
        # Dark mode background (30), light text (220), faint watermark (65).
        dark = np.full((60, 120), 30, dtype=np.uint8)
        dark[10:20, 10:50] = 220
        dark[30:40, 10:50] = 65
        cleaned_dark = preprocess_image(Image.fromarray(dark, mode='L'))
        self.assertLess(int(cleaned_dark[15, 25]), 40)
        self.assertEqual(int(cleaned_dark[35, 25]), 255)

    def test_ocr_chunk_split_and_paragraph_merge(self):
        import numpy as np
        arr = np.full((1200, 400), 255, dtype=np.uint8)
        arr[100:120, 20:380] = 0
        arr[850:870, 20:380] = 0
        arr[950:970, 20:380] = 0
        chunks = split_into_chunks(arr, max_height=900, pad=10)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0][0], 0)
        self.assertGreaterEqual(chunks[1][0], 870)
        self.assertLessEqual(chunks[1][0], 900)
        boxes = [
            (40, 10, 800, 30, '第一段开头很长一直写到行尾'),
            (0, 40, 500, 60, '第一段换行续接。'),
            (40, 70, 420, 90, '第二段短句。'),
            (40, 100, 800, 120, '第三段开头'),
            (0, 130, 300, 150, '第三段结尾。'),
        ]
        self.assertEqual(
            merge_lines_to_paragraphs(boxes),
            ['第一段开头很长一直写到行尾第一段换行续接。', '第二段短句。', '第三段开头第三段结尾。'],
        )

    def test_ocr_cache_and_txt_export(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / 'chapters'
            save_chapter(folder, CHAPTERS[0], text=['文字第一段', '文字第二段'])
            rec = save_chapter(folder, CHAPTERS[1], image=picture())
            save_ocr(folder, '2', rec['sha256'], ['识别第一段', '识别第二段'])
            self.assertEqual(cached_ocr(folder, '2', rec['sha256']), ['识别第一段', '识别第二段'])
            self.assertIsNone(cached_ocr(folder, '2', 'different-sha'))
            chapters = [dict(volume='第一卷', **c) for c in CHAPTERS[:3]]
            path, missing = export_txt(
                BOOK, chapters, folder, Path(directory), {'3': '未购买'},
                ocr_fn=lambda f, r: cached_ocr(f, r['id'], r['sha256']),
            )
            self.assertEqual(missing, 1)
            self.assertIn('未完整', path.name)
            text = path.read_text(encoding='utf-8')
            self.assertIn('《书名 & 测试》', text)
            self.assertIn('=== 第一卷 ===', text)
            self.assertIn('　　文字第一段', text)
            self.assertIn('　　识别第一段', text)
            self.assertIn('【正文缺失】未购买', text)

    def test_gui_command_builder_and_range_validation(self):
        self.assertEqual(normalize_target('100363224'), 'https://www.ciweimao.com/book/100363224')
        self.assertEqual(normalize_target('100363224', offline_ocr=True), '100363224')
        self.assertEqual(parse_chapter_range(True, '5', '10'), (1, None))
        self.assertEqual(parse_chapter_range(False, '10', '15'), (10, 6))
        self.assertEqual(parse_chapter_range(False, '117', '117'), (117, 1))
        self.assertEqual(parse_chapter_range(False, '20', ''), (20, None))
        with self.assertRaises(ContentError):
            parse_chapter_range(False, '15', '10')
        cmd = build_command('python', '100363224', 'txt_only', False, '5', '8', workers=1, delay=2, no_prompt=True, headless=True)
        self.assertIn('--txt-only', cmd)
        self.assertIn('--start', cmd)
        self.assertEqual(cmd[cmd.index('--start') + 1], '5')
        self.assertEqual(cmd[cmd.index('--limit') + 1], '4')
        self.assertIn('--headless', cmd)
        offline_cmd = build_command('python', 'https://www.ciweimao.com/book/100363224', 'offline_ocr', False, '117', '117')
        self.assertTrue(offline_cmd[1].endswith('ocr.py'))
        self.assertEqual(offline_cmd[2], '100363224')
        self.assertEqual(offline_cmd[offline_cmd.index('--limit') + 1], '1')


class BrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(channel='msedge', headless=True)
        self.context = await self.browser.new_context()
        self.page = await self.context.new_page()
        self.image = picture()
        self.requested = []

        async def fixture(route):
            parsed = urlsplit(route.request.url)
            path = parsed.path
            self.requested.append(path)
            if path == '/chapter/book_chapter_image':
                return await route.fulfill(body=self.image, content_type='image/png')
            if path == '/book/100':
                html = '<meta property="og:novel:book_name" content="测试书"><meta property="og:novel:author" content="作者">'
            elif path == '/chapter-list/100':
                html = '<div class="book-catalog"><div class="book-chapter-box"><h4 class="sub-tit">第一卷</h4><ul class="book-chapter-list">' + ''.join(f'<li><a href="/chapter/{n}">章节 {n}</a></li>' for n in (2, 1, 2, 3)) + '</ul></div></div>'
            else:
                identifier = path.rsplit('/', 1)[-1]
                html = f'<script>window.HB={{book:{{book_id:100}},userinfo:{{reader_id:123}}}};</script><div id="J_BookCnt" data-id="{identifier}">'
                if identifier == '2':
                    html += '<div id="J_BookImage" style="width:800px;height:3200px;background-image:url(/chapter/book_chapter_image?chapter_id=2&amp;image_code=temporary)"><img id="realBookImage" src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"/></div>'
                elif identifier == '3':
                    html += '<script>var auth_access = 0;</script><div id="J_BookRead"><p class="chapter">这是不应导出的预览正文</p></div>'
                elif identifier == '4':
                    html += '<script>HB.userinfo.reader_id=0;</script><div id="J_BookRead"><p class="chapter">未登录</p></div>'
                else:
                    html += '<div id="J_BookRead"><div class="watermark"><p>不要水印</p></div><p class="chapter">第一段<span style="opacity:0.01">1H64Q</span><i class="J_Num">99</i></p><div class="chapter-comment-wrap"><p>不要评论</p></div><p class="chapter">第二段<span style="opacity:0.4">1H64Q</span><span style="display:none">隐藏</span><span style="font-size:0">零字号</span></p></div>'
                html += '</div>'
            await route.fulfill(body=html, content_type='text/html; charset=utf-8')

        await self.context.route('**/*', fixture)

    async def asyncTearDown(self):
        await self.context.close()
        await self.browser.close()
        await self.playwright.stop()

    async def test_discover_preserves_order_and_removes_duplicates(self):
        book, chapters = await discover(self.page, 'https://www.ciweimao.com/book/100')
        self.assertEqual(book['title'], '测试书')
        self.assertEqual([c['id'] for c in chapters], ['2', '1', '3'])
        self.assertTrue(all(c['volume'] == '第一卷' for c in chapters))

    async def test_text_strips_comments_watermarks_and_counts(self):
        result = await ChapterReader(self.page, timeout=5).read(CHAPTERS[0], '100')
        self.assertEqual(result, {'text': ['第一段', '第二段']})

    async def test_image_response(self):
        result = await ChapterReader(self.page, timeout=6).read(CHAPTERS[1], '100')
        with Image.open(io.BytesIO(result['image'])) as image:
            self.assertEqual(image.size, (800, 3200))
        self.assertIn('/chapter/book_chapter_image', self.requested)

    async def test_image_canvas_fallback(self):
        reader = ChapterReader(self.page, timeout=8)
        self.page.remove_listener('response', reader.on_response)
        result = await reader.read(CHAPTERS[1], '100')
        self.assertTrue(result['image'])

    async def test_denied_preview_is_not_saved(self):
        with self.assertRaisesRegex(ContentError, '阅读权限'):
            await ChapterReader(self.page, timeout=4).read(CHAPTERS[2], '100')

    async def test_session_expiry(self):
        with self.assertRaises(SessionExpired):
            await ChapterReader(self.page, timeout=4).read(CHAPTERS[3], '100')

    async def test_parallel_mixed_download_and_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            failures = await download(self.context, CHAPTERS[:2], BOOK, folder, workers=2, delay=0)
            self.assertEqual(failures, {})
            self.assertEqual(cached_chapter(folder, '1')['kind'], 'text')
            self.assertEqual(cached_chapter(folder, '2')['kind'], 'image')
            self.requested.clear()
            await download(self.context, CHAPTERS[:2], BOOK, folder, workers=2, delay=0)
            self.assertNotIn('/chapter/1', self.requested)
            self.assertNotIn('/chapter/2', self.requested)


if __name__ == '__main__':
    unittest.main()
