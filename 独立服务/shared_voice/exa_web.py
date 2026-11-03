"""Keyless Exa MCP path selected by Agent-Reach; no paid fallback or credentials.

Search highlights are discovery only. fetch() returns separately retrieved text.
This uses Agent-Reach's upstream directly, as its capability-layer design intends.
"""
import ipaddress
import json
import re
import urllib.error
import urllib.request
from urllib.parse import urlsplit

ENDPOINT = 'https://mcp.exa.ai/mcp'
MAX_BYTES = 1_000_000


class WebError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def provider_failure(text):
    """Only classify an upstream notice at the start, never article content."""
    lead = text.strip()[:600]
    if re.match(r"(?is)^(?:error[:\s]*)?(?:you(?:'|’)ve hit exa(?:'|’)s free mcp rate limit|(?:exa[:\s-]*)?(?:rate limit(?:ed| exceeded)?|too many requests)|free (?:mcp )?(?:rate )?limit (?:reached|exceeded))\b",lead):
        return 'rate_limited'
    return None


def public_url(value):
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) <= 32 or ord(c) == 127 for c in value):
        raise WebError('invalid_url')
    try:
        u = urlsplit(value)
        host = (u.hostname or '').lower().rstrip('.')
        if u.scheme not in ('https', 'http') or not host or u.username is not None or u.password is not None or u.port not in (None, 80, 443):
            raise ValueError()
        if '\\' in value or host == 'localhost' or host.endswith(('.localhost', '.local', '.internal')) or '.' not in host:
            raise ValueError()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            # Avoid alternate numeric IPv4 forms; this service never fetches locally.
            if host.replace('.', '').isdigit() or host.startswith('0x'):
                raise ValueError()
        else:
            if not address.is_global:
                raise ValueError()
    except ValueError:
        raise WebError('invalid_url') from None
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def transport(payload, timeout):
    req = urllib.request.Request(ENDPOINT, data=json.dumps(payload).encode(), headers={
        'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream',
        'User-Agent': 'PiSearchValidation/1.0'})
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=timeout) as response:
            raw = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as error:
        code = 'rate_limited' if error.code == 429 else 'upstream_unavailable'
        error.close()
        raise WebError(code) from None
    except (OSError, urllib.error.URLError):
        raise WebError('upstream_unavailable') from None
    if len(raw) > MAX_BYTES:
        raise WebError('response_too_large')
    return raw


class ExaWeb:
    def __init__(self, send=transport):
        self.send = send

    def call(self, name, arguments, timeout=25):
        payload = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                   'params': {'name': name, 'arguments': arguments}}
        raw = self.send(payload, timeout)
        try:
            if not isinstance(raw, bytes) or len(raw) > MAX_BYTES:
                raise ValueError()
            text = raw.decode('utf-8')
            if text.lstrip().startswith('{'):
                replies = [json.loads(text)]
            else:
                replies = []
                for event in text.replace('\r\n', '\n').split('\n\n'):
                    data = '\n'.join(line[5:].lstrip() for line in event.splitlines() if line.startswith('data:'))
                    if data and data != '[DONE]':
                        replies.append(json.loads(data))
            matches = [r for r in replies if isinstance(r, dict) and r.get('id') == 1]
            if len(matches) != 1:
                raise ValueError()
            reply = matches[0]
            if 'error' in reply:
                message=reply['error'].get('message','') if isinstance(reply['error'],dict) else ''
                if isinstance(message,str) and provider_failure(message):raise WebError('rate_limited')
                raise WebError('rpc_error')
            result = reply['result']
            items = result['content']
            if not isinstance(items, list) or not items or any(not isinstance(c, dict) for c in items):
                raise ValueError()
            bodies = [c['text'] for c in items if c.get('type') == 'text']
            if not bodies or any(not isinstance(t, str) for t in bodies):
                raise ValueError()
            body='\n'.join(bodies)
            if provider_failure(body):raise WebError('rate_limited')
            if result.get('isError'):raise WebError('tool_error')
            return body
        except (ValueError, KeyError, TypeError, AttributeError):
            raise WebError('invalid_response') from None

    def search(self, query):
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 500:
            raise WebError('invalid_query')
        text = self.call('web_search_exa', {'query': query.strip(), 'numResults': 3},timeout=6)
        found = []
        for block in re.split(r'(?m)^Title: ', text)[1:]:
            title, _, rest = block.partition('\n')
            match = re.search(r'(?m)^URL: (\S+)\s*$', rest)
            if not match:
                continue
            try:
                url = public_url(match.group(1))
            except WebError:
                continue
            if not any(x['url'] == url for x in found):
                found.append({'title': title.strip()[:300], 'url': url})
            if len(found) == 3:
                break
        if not found:
            if re.match(r'(?is)^\s*(?:no (?:search )?results(?: found)?|no matches found)\b',text):raise WebError('search_empty')
            # A parser mismatch is not evidence that the requested fact is absent.
            raise WebError('search_format_changed')
        return found

    def fetch(self, result):
        url = public_url(result['url'])
        text = self.call('web_fetch_exa', {'urls': [url], 'maxCharacters': 6000},timeout=3.5)
        # Require the upstream's source attribution, then retain only its body.
        match = re.search(r'(?m)^URL: (\S+)\s*\n', text)
        if not match or match.group(1) != url:
            raise WebError('source_mismatch')
        body = text[match.end():].strip()
        if provider_failure(body):raise WebError('rate_limited')
        if len(body) < 100:
            raise WebError('no_body')
        if re.match(r'(?i)^(error|failed to|access denied|captcha|sign in|log in)\b', body):
            raise WebError('no_body')
        return {'title': result['title'], 'sourceUrl': url, 'text': body[:6000],
                'kind': 'web', 'retrieval': 'exa-keyless-mcp', 'freshness': 'not-verified'}
