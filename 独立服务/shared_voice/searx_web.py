"""360-only private SearXNG adapter; bounded direct public IPv4 page reader.

Search snippets are never supplied as page evidence. DNS is checked at each
redirect, then the connection pins a validated address while preserving TLS SNI.
No proxy, cookie, credential, paid fallback, or automatic retry is used.
"""
import http.client
from html.parser import HTMLParser
import ipaddress
import json
import re
import socket
import ssl
import time
import urllib.error
import urllib.request
from urllib.parse import quote, urlencode, urljoin, urlsplit, urlunsplit

from exa_web import WebError, public_url, NoRedirect

SEARCH_URL = 'http://127.0.0.1:18198/search'
MAX_PAGE_BYTES = 3_000_000
MAX_SEARCH_BYTES = 1_000_000


def site_key(url):
    """Publisher-diversity hint, not a trust/ownership or security decision."""
    host = urlsplit(url).hostname.lower().rstrip('.')
    parts = host.split('.')
    suffix = '.'.join(parts[-2:])
    common_multilevel = {'com.cn','net.cn','org.cn','gov.cn','edu.cn','ac.cn',
                        'co.uk','org.uk','com.au','co.jp','co.kr'}
    return '.'.join(parts[-3:] if suffix in common_multilevel else parts[-2:])


def search_transport(query):
    request = urllib.request.Request(SEARCH_URL + '?' + urlencode({
        'q': query, 'format': 'json', 'engines': '360search', 'language': 'zh-CN'}),
        headers={'Accept': 'application/json', 'User-Agent': 'PiSearch/0.3.9'})
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect).open(request, timeout=6) as response:
            raw = response.read(MAX_SEARCH_BYTES + 1)
        if len(raw) > MAX_SEARCH_BYTES:
            raise WebError('search_format_changed')
        return json.loads(raw)
    except urllib.error.HTTPError as error:
        code = 'rate_limited' if error.code == 429 else 'upstream_unavailable'
        error.close()
        raise WebError(code) from None
    except (OSError, urllib.error.URLError):
        raise WebError('upstream_unavailable') from None
    except (ValueError, UnicodeError):
        raise WebError('search_format_changed') from None


def target(url, resolver=socket.getaddrinfo):
    public_url(url)
    parsed = urlsplit(url)
    host = parsed.hostname.rstrip('.').encode('idna').decode('ascii')
    port = 443 if parsed.scheme == 'https' else 80
    if parsed.port not in (None, port):
        raise WebError('invalid_url')
    try:
        # IPv4-only deliberately avoids mapped/transition IPv6 address ambiguity.
        answers = resolver(host, port, socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        raise WebError('page_unavailable') from None
    if not answers:
        raise WebError('page_unavailable')
    addresses = []
    for family, _, _, _, address in answers:
        try:
            ip = ipaddress.ip_address(address[0])
            if family != socket.AF_INET or ip.version != 4 or not ip.is_global or ip.is_multicast:
                raise ValueError()
        except ValueError:
            raise WebError('invalid_url') from None
        addresses.append(str(ip))
    path = quote(parsed.path or '/', safe='/%:@!$&\'()*+,;=-._~')
    query = quote(parsed.query, safe='/%?:@!$&\'()*+,;=-._~')
    normalized = urlunsplit((parsed.scheme, host, path, query, ''))
    return normalized, host, port, path + ('?' + query if query else ''), addresses[0]


class PinnedConnection(http.client.HTTPConnection):
    def __init__(self, host, port, address, secure, timeout):
        super().__init__(host, port, timeout=timeout)
        self.address, self.secure = address, secure

    def connect(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(self.timeout)
            sock.connect((self.address, self.port))
            if self.secure:
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=self.host)
            self.sock = sock
        except Exception:
            sock.close()
            raise


class PageLinks(HTMLParser):
    """Collects anchor targets so the planner may pick real in-page links."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.raw = []

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            for name, value in attrs:
                if name == 'href' and value:
                    self.raw.append((value, ''))
                    break


class BodyText(HTMLParser):
    HIDDEN = {'script', 'style', 'noscript', 'svg', 'template'}
    BLOCK = {'p', 'div', 'li', 'br', 'tr', 'h1', 'h2', 'h3', 'section', 'footer', 'article'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = []
        self.parts = []
        self.size = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.HIDDEN:
            self.hidden.append(tag)
        elif not self.hidden and tag in self.BLOCK:
            self.add('\n')

    def handle_endtag(self, tag):
        if self.hidden and tag == self.hidden[-1]:
            self.hidden.pop()
        elif not self.hidden and tag in self.BLOCK:
            self.add('\n')

    def add(self, value):
        if self.size < 250_000:
            value = value[:250_000-self.size]
            self.parts.append(value)
            self.size += len(value)

    def handle_data(self, data):
        if not self.hidden:
            self.add(data)


def read_page(url, *, resolver=socket.getaddrinfo, connection=PinnedConnection, timeout=3.5, with_links=False):
    deadline = time.monotonic() + timeout
    for hop in range(4):
        url, host, port, path, address = target(url, resolver)
        remaining = deadline-time.monotonic()
        if remaining <= 0:
            raise WebError('page_timeout')
        conn = connection(host, port, address, port == 443, remaining)
        try:
            conn.request('GET', path, headers={'User-Agent': 'PiReadOnlySearch/0.3.9',
                'Accept': 'text/html,text/plain', 'Accept-Encoding': 'identity', 'Connection': 'close'})
            io_sock = conn.sock
            response = conn.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader('Location')
                if not location or hop == 3:
                    raise WebError('page_redirect')
                url = urljoin(url, location)
                continue
            if response.status in (403, 429):
                raise WebError('page_restricted')
            if response.status != 200:
                raise WebError('page_unavailable')
            mime = response.getheader('Content-Type', '').split(';')[0].strip().lower()
            if mime not in {'text/html', 'application/xhtml+xml', 'text/plain'}:
                raise WebError('unsupported_page')
            if response.getheader('Content-Encoding', 'identity').lower() not in ('identity', ''):
                raise WebError('unsupported_page')
            length = response.getheader('Content-Length')
            if length and int(length) > MAX_PAGE_BYTES:
                raise WebError('page_too_large')
            chunks, count = [], 0
            while True:
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    raise WebError('page_timeout')
                # HTTPResponse closes a Connection: close socket after its last
                # Content-Length byte. A final EOF read must not touch that socket.
                if io_sock and io_sock.fileno() >= 0:
                    io_sock.settimeout(remaining)
                chunk = response.read1(min(65536, MAX_PAGE_BYTES + 1 - count))
                if not chunk:
                    break
                count += len(chunk)
                if count > MAX_PAGE_BYTES:
                    raise WebError('page_too_large')
                chunks.append(chunk)
            raw = b''.join(chunks)
            charset = response.headers.get_content_charset()
            if not charset:
                match = re.search(br'charset\s*=\s*["\']?([\w-]+)', raw[:4096], re.I)
                charset = match.group(1).decode('ascii') if match else 'utf-8'
            # Only encodings used by these text documents are accepted.
            charset = charset.lower().replace('_', '-')
            if charset not in {'utf-8', 'utf8', 'gbk', 'gb2312', 'gb18030', 'big5', 'iso-8859-1', 'windows-1252'}:
                raise WebError('unsupported_page')
            decoded = raw.decode(charset, errors='replace')
            links = []
            if with_links and mime != 'text/plain':
                collector = PageLinks()
                collector.feed(decoded)
                seen_links = set()
                for href, _ in collector.raw:
                    resolved = urljoin(url, href.split('#')[0])
                    if not resolved or resolved == url or resolved in seen_links:
                        continue
                    try:
                        public_url(resolved)
                    except WebError:
                        continue
                    seen_links.add(resolved)
                    links.append(resolved)
                    if len(links) == 20:
                        break
            if mime != 'text/plain':
                parser = BodyText()
                parser.feed(decoded)
                decoded = ''.join(parser.parts)
            text = '\n'.join(' '.join(line.split()) for line in decoded.splitlines() if line.strip())
            if len(text) < 100 or '\ufffd' in text[:100]:
                raise WebError('no_body')
            # Interstitial/verification pages (e.g. paths like /challenge?...)
            # are not usable body text; report them as restricted so the runtime
            # model is told the source is blocked, never counted as evidence.
            if re.search(r'captcha|安全验证|人机验证|访问验证|访问过于频繁', text[:500], re.I) or (
                    'challenge' in urlsplit(url).path and len(text) < 1000):
                raise WebError('page_restricted')
            return (url, text[:6000], links) if with_links else (url, text[:6000])
        except WebError:
            raise
        except (TimeoutError, socket.timeout):
            raise WebError('page_timeout') from None
        except (OSError, ValueError, http.client.HTTPException, UnicodeError):
            raise WebError('page_unavailable') from None
        finally:
            conn.close()
    raise WebError('page_redirect')


class SearxWeb:
    provider = 'searxng-360'

    def __init__(self, send=search_transport, reader=read_page):
        self.send, self.reader = send, reader

    def search(self, query):
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 500:
            raise WebError('invalid_query')
        data = self.send(query.strip())
        if not isinstance(data, dict) or not isinstance(data.get('results'), list):
            raise WebError('search_format_changed')
        errors = data.get('unresponsive_engines', [])
        if errors:
            code = 'rate_limited' if re.search(r'captcha|429|too many', str(errors), re.I) else 'upstream_unavailable'
            raise WebError(code)
        found = []
        for item in data['results'][:50]:
            if not isinstance(item, dict) or item.get('engines') != ['360search']:
                raise WebError('search_format_changed')
            if not isinstance(item.get('title'), str) or not item['title'].strip():
                continue
            try:
                url = public_url(item.get('url'))
            except WebError:
                continue
            if not any(x['url'] == url for x in found):
                found.append({'title': item['title'][:300], 'url': url})
        if not found:
            raise WebError('search_empty')
        # Avoid consuming both body-reading slots on desktop/mobile copies of
        # one publisher while a distinct institution is already in the results.
        selected, sites = [], set()
        for item in found:
            site = site_key(item['url'])
            if site not in sites:
                selected.append(item)
                sites.add(site)
            if len(selected) == 6:
                break
        for item in found:
            if len(selected) == 6:
                break
            if item not in selected:
                selected.append(item)
        return selected

    def fetch(self, item):
        requested = public_url(item['url'])
        try:
            final, text, links = self.reader(requested, with_links=True)
        except TypeError:
            final, text = self.reader(requested)
            links = []
        return {'title': item['title'], 'sourceUrl': final, 'requestedUrl': requested,
                'text': text, 'kind': 'web', 'retrieval': self.provider + '-direct-body',
                'links': links, 'freshness': 'not-verified'}
