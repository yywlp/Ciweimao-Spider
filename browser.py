"""Read the same DOM and image responses as the user's signed-in Edge tabs."""

import asyncio
import base64
import time
from urllib.parse import parse_qs, urlsplit

from exporter import ContentError, validate_image


class SessionExpired(ContentError):
    pass


SNAPSHOT = r"""() => {
    const root = document.querySelector('#J_BookCnt');
    const element = document.querySelector('#J_BookImage');
    const background = element ? getComputedStyle(element).backgroundImage : '';
    const match = background.match(/^url\(["']?(.*?)["']?\)$/);
    const textRoot = document.querySelector('#J_BookRead');
    let paragraphs = [];
    if (textRoot) {
        let nodes = [...textRoot.querySelectorAll('.chapter')];
        if (!nodes.length) nodes = [...textRoot.querySelectorAll('p')];
        const extractText = node => {
            if (node.nodeType === Node.TEXT_NODE) return node.nodeValue || '';
            if (node.nodeType !== Node.ELEMENT_NODE) return '';
            if (node.matches('script,style,.J_Num,.num,.chapter-comment-wrap,.watermark,[hidden]')) return '';
            const style = getComputedStyle(node);
            if (style.display === 'none' || style.visibility === 'hidden'
                    || parseFloat(style.opacity) < 0.9 || parseFloat(style.fontSize) === 0) return '';
            let out = '';
            for (const child of node.childNodes) out += extractText(child);
            return out;
        };
        paragraphs = nodes.filter(node => !node.closest('.watermark, .chapter-comment-wrap, .tsukkomi, [hidden]') && getComputedStyle(node).display !== 'none')
            .map(node => extractText(node).trim()).filter(Boolean);
    }
    const prompts = [...document.querySelectorAll('.dialog-tip, #J_BookRead .subscribe, .read-bd .pay, .read-bd .buy')]
        .filter(n => n.getBoundingClientRect().height > 0).map(n => n.textContent).join(' ');
    const access = [...document.scripts].map(s => s.textContent.match(/var\s+auth_access\s*=\s*(\d+)/)).find(Boolean);
    return {
        id: root?.getAttribute('data-id'),
        book: String(window.HB?.book?.book_id || ''),
        loggedIn: Number(window.HB?.userinfo?.reader_id || 0) > 0,
        image: match ? {url: new URL(match[1], location.href).href,
            width: element.getBoundingClientRect().width,
            height: element.getBoundingClientRect().height} : null,
        authorized: access ? Number(access[1]) > 0 : null,
        paragraphs, prompts
    };
}"""


async def logged_in(page):
    return await page.evaluate("() => Number(window.HB?.userinfo?.reader_id || 0) > 0")


async def discover(page, url):
    await page.goto(url, wait_until="domcontentloaded")
    await page.wait_for_selector('meta[property="og:novel:book_name"]', state="attached")
    metadata = await page.evaluate("""() => ({
        title: document.querySelector('meta[property="og:novel:book_name"]')?.content,
        author: document.querySelector('meta[property="og:novel:author"]')?.content || ''
    })""")
    identifier = urlsplit(url).path.strip('/').split('/')[-1]
    await page.goto(f"https://www.ciweimao.com/chapter-list/{identifier}", wait_until="domcontentloaded")
    await page.wait_for_selector('.book-chapter-list a[href*="/chapter/"]', state="attached")
    raw = await page.evaluate("""() => [...document.querySelectorAll('.book-catalog .book-chapter-list a[href]')].map(a => ({
        url: a.href, title: a.textContent.trim(),
        volume: a.closest('.book-chapter-box')?.querySelector('.sub-tit')?.textContent.trim() || ''
    }))""")
    seen, chapters = set(), []
    for item in raw:
        parsed = urlsplit(item['url'])
        parts = parsed.path.strip('/').split('/')
        if parsed.hostname != 'www.ciweimao.com' or len(parts) != 2 or parts[0] != 'chapter' or not parts[1].isdigit():
            continue
        if parts[1] not in seen:
            seen.add(parts[1])
            chapters.append(dict(id=parts[1], title=item['title'], volume=item['volume'],
                                 url=f'https://www.ciweimao.com/chapter/{parts[1]}'))
    if not chapters:
        raise ContentError("没有取得目录，网站结构可能已变化。")
    return dict(id=identifier, **metadata), chapters


async def block_purchase(route):
    """Block known purchase/settings calls; navigation and normal reading remain enabled."""
    path = urlsplit(route.request.url).path.lower()
    if any(term in path for term in ('/buy', '_buy', '/subscribe', '/dingyue', '/recharge', 'set_auto_buy')):
        await route.abort()
    else:
        await route.continue_()


class ChapterReader:
    def __init__(self, page, timeout=45):
        self.page = page
        self.timeout = timeout
        self.responses = {}
        self.tasks = set()
        self.identifier = None
        self.page.on('response', self.on_response)

    def on_response(self, response):
        parsed = urlsplit(response.url)
        if (parsed.hostname == 'www.ciweimao.com'
                and parsed.path == '/chapter/book_chapter_image'
                and parse_qs(parsed.query).get('chapter_id') == [self.identifier]
                and response.status == 200):
            task = asyncio.create_task(self.capture(response))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

    async def capture(self, response):
        try:
            data = await response.body()
            parsed = urlsplit(response.url)
            if parse_qs(parsed.query).get('chapter_id') == [self.identifier]:
                self.responses[response.url] = data
        except Exception:
            pass  # Navigation may cancel an in-flight image; next navigation retries it.

    async def read(self, chapter, expected_book, require_login=True):
        if self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)
        self.responses.clear()
        self.identifier = chapter['id']
        response = await self.page.goto(chapter['url'], wait_until='domcontentloaded')
        if response and response.status in (403, 429):
            raise SessionExpired("网站要求验证或限制访问，请在浏览器处理后重试。")
        if response and response.status >= 400:
            raise ContentError("章节页面返回错误状态。")
        deadline = time.monotonic() + self.timeout
        previous, stable_since = None, None
        image_error = None
        while time.monotonic() < deadline:
            info = await self.page.evaluate(SNAPSHOT)
            if info['id'] != chapter['id'] or info['book'] != expected_book:
                await asyncio.sleep(0.3)
                continue
            if require_login and not info['loggedIn']:
                raise SessionExpired("登录状态已失效，请重新登录后续传。")
            if info['authorized'] is False:
                raise ContentError("账号无本章阅读权限，未保存预览内容。")
            if any(term in info['prompts'] for term in ('请先订阅', '购买本章', '请先登录', '验证码')):
                raise ContentError("章节要求订阅、登录或验证，未保存预览内容。")
            if info['image']:
                image_info = info['image']
                parsed = urlsplit(image_info['url'])
                if (parsed.hostname != 'www.ciweimao.com' or parsed.path != '/chapter/book_chapter_image'
                        or parse_qs(parsed.query).get('chapter_id') != [chapter['id']]):
                    raise ContentError("正文图片不属于当前章节。")
                if self.tasks:
                    await asyncio.gather(*list(self.tasks), return_exceptions=True)
                data = self.responses.get(image_info['url'])
                if data is not None and image_info['height'] > 100:
                    try:
                        return {'image': validate_image(data, image_info['width'], image_info['height'])}
                    except ContentError as error:
                        image_error = error
                elif image_info['height'] > 100 and time.monotonic() + self.timeout - deadline > 3:
                    # This is the verified cache/canvas fallback, not a direct HTTP refetch.
                    try:
                        encoded = await asyncio.wait_for(self.page.evaluate("""async url => {
                            const image = new Image(); image.src = url; await image.decode();
                            const canvas = document.createElement('canvas');
                            canvas.width = image.naturalWidth; canvas.height = image.naturalHeight;
                            canvas.getContext('2d').drawImage(image, 0, 0);
                            return canvas.toDataURL('image/png').split(',')[1];
                        }""", image_info['url']), timeout=8)
                        return {'image': validate_image(base64.b64decode(encoded, validate=True), image_info['width'], image_info['height'])}
                    except Exception:
                        image_error = ContentError("无法从浏览器缓存取得完整正文图片。")
            elif info['paragraphs']:
                current = info['paragraphs']
                if any(term in ''.join(current) for term in ('本章为VIP章节，请先订阅', '登录后继续阅读', '订阅后阅读', '购买后阅读')):
                    raise ContentError("正文含有订阅或登录提示，未保存预览内容。")
                if current == previous:
                    if time.monotonic() - stable_since >= 1.2:
                        return {'text': current}
                else:
                    previous, stable_since = current, time.monotonic()
            await asyncio.sleep(0.3)
        if image_error:
            raise image_error
        raise ContentError("正文加载超时，可能需要登录、订阅或人工验证。")
