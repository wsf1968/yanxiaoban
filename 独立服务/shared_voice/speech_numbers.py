"""Deterministic Chinese speech formatting for TTS.

Pure text -> text layer applied only to the text sent to TTS (speechText).
It never changes reply/text/history/page tables. Semantics of numbers are
inferred from local context with conservative guards; anything ambiguous is
left untouched. No LLM calls, no I/O, no randomness.
"""
import datetime
import re

_DIGITS = '零一二三四五六七八九'

# Units whose preceding Arabic numeral should be read as a quantity, longest
# first so alternation is greedy. 月/日 are excluded: dates are handled by
# convert_dates so that 2月 reads 二月, never 两月.
_UNITS = ('平方公里', '平方米', '公里', '厘米', '毫米', '毫升', '千克', '公斤',
          '小时', '分钟', '克', '吨', '升', '米', '秒', '天', '年',
          '元', '块钱', '块', '折', '个', '份', '位', '名', '只', '条', '杯',
          '瓶', '碗', '人', '次', '遍', '页', '张', '件', '层', '楼', '岁',
          '度', '套', '间', '座', '辆', '包', '袋', '盒', '箱', '分')
_UNIT_RE = '|'.join(_UNITS)

_PERIOD_WORDS = ('上午', '下午', '中午', '晚上', '夜里', '夜晚', '凌晨',
                 '早上', '早晨', '傍晚')
_PERIOD_RE = '(?:' + '|'.join(_PERIOD_WORDS) + ')'

# All valid clock minutes; colon may be half or full width.
_TIME_RE = r'(2[0-3]|[01]?\d)([:：])([0-5]\d)'
# Characters that mean the numeral belongs to a larger token (URL, ISO
# timestamp, version, IP, ratio) rather than a standalone clock time.
_TIME_LOOKBEHIND = r'(?<![\d/:：.\-A-Za-z—–])'
# Local context that marks a HH:MM as a score/ratio/chapter reference.
_SCORE_BEFORE = re.compile(
    r'(?:比分|比例|比值|净胜|得分|总分|胜|负|平|第\d{1,3}[章节回卷话]|[章节回卷话])\s*[::]?\s*$')


def _digits_cn(s):
    return ''.join(_DIGITS[int(c)] for c in s)


def quantity_cn(n):
    """Chinese quantity reading for 0 <= n < 10**8, else None."""
    if n >= 10 ** 8:
        return None
    if n < 10:
        return _DIGITS[n]
    if n < 20:
        return '十' + ('' if n == 10 else _DIGITS[n - 10])

    def small(m, strip=True):
        out, zero = '', False
        for i, c in enumerate(str(m)):
            d = int(c)
            pos = len(str(m)) - 1 - i
            if d == 0:
                zero = bool(out)
            else:
                if zero:
                    out += '零'
                out += _DIGITS[d] + ('十百千'[pos - 1] if pos else '')
                zero = False
        # 10-group leading 一 is unnatural in higher groups: 十万 not 一十万.
        # Internal lower 万-group keeps 一十: 一万零一十, never 一万零十.
        if strip and out.startswith('一十'):
            out = out[1:]
        return out

    if n < 10000:
        return small(n)
    high, low = divmod(n, 10000)
    out = small(high) + '万'
    if low:
        out += ('零' + small(low, strip=False)) if low < 1000 else small(low, strip=False)
    return out


def _unit_cn(n):
    return '两' if n == 2 else quantity_cn(n)


def _big_fallback(raw):
    """Over-range numbers: keep the Arabic digits verbatim (never None)."""
    return raw


def _decimal_cn(raw):
    """1234.50 -> 一千二百三十四点五零; 156 -> 一百五十六.

    Returns None when the whole part exceeds the supported range; callers
    must then keep the original token instead of concatenating None.
    """
    raw = raw.replace(',', '')
    if '.' in raw:
        whole, frac = raw.split('.', 1)
        out = quantity_cn(int(whole)) if whole else '零'
        if out is None:
            return None
        return out + '点' + _digits_cn(frac)
    return quantity_cn(int(raw))


def _minute_cn(m):
    if m == 0:
        return ''
    return (_digits_cn('0%d' % m) if m < 10 else quantity_cn(m)) + '分'


def _period_of(h):
    if h < 6:
        return '凌晨'
    if h < 12:
        return '上午'
    if h == 12:
        return '中午'
    if h < 18:
        return '下午'
    return '晚上'


def _hour_cn(h):
    if h == 0:
        return '零'
    if h == 12:
        return '十二'
    if h > 12:
        return quantity_cn(h - 12)
    return quantity_cn(h)


def _time_cn(h, m, before, period=None):
    """Clock time reading; suppress the period if text already states one."""
    if period is None:
        period = '' if re.search(_PERIOD_RE + r'$', before or '') else _period_of(h)
    return period + _hour_cn(h) + '点' + _minute_cn(m)


def convert_times(text):
    def before_of(m):
        return text[max(0, m.start() - 6):m.start()]

    def scored(m):
        return bool(_SCORE_BEFORE.search(before_of(m)))

    def rangerep(m):
        if scored(m):
            return m.group()
        # Outer parens make group 1 the whole first time, so the hour/minute
        # captures of the two _TIME_RE copies are groups 2-4 and 5-7.
        h1, m1, h2, m2 = int(m[2]), int(m[4]), int(m[6]), int(m[8])
        p1 = _period_of(h1)
        before = before_of(m)
        stated = re.search(_PERIOD_RE + r'$', before or '')
        if stated:
            p2 = ''  # 承接已点明的时段（如下午3:00—5:00）
        else:
            p2 = '' if p1 == _period_of(h2) else _period_of(h2)
        first = _time_cn(h1, m1, before)
        return '%s到%s' % (first, _time_cn(h2, m2, '', period=p2))

    # Ranges first: 09:00—17:00 / 9:00-18:00 / 08:30至17:30
    sep = r'\s*(?:——|—|–|-|~|～|至|到)\s*'
    text = re.sub(
        _TIME_LOOKBEHIND + r'(' + _TIME_RE + r')' + sep + r'(' + _TIME_RE + r')(?![::\d])',
        rangerep, text)

    def single(m):
        if scored(m):
            return m.group()
        return _time_cn(int(m[1]), int(m[3]), before_of(m))

    return re.sub(_TIME_LOOKBEHIND + _TIME_RE + r'(?![::：\d])', single, text)


_DUR_WORDS = ('历时', '持续', '耗时', '长达', '经过', '历经', '用了', '花费')


def convert_dates(text):
    # Invalid full dates like 13月40日 must stay verbatim: stash them out of
    # the per-field 月/日 rules, restore before returning.
    kept = []

    def stash(m):
        kept.append(m.group())
        return '\x01%d\x01' % (len(kept) - 1)

    def full_date(m):
        try:
            datetime.date(2024, int(m[1]), int(m[2]))
        except ValueError:
            return stash(m)
        return m.group()

    text = re.sub(r'(?<![\d./\-])(\d{1,2})月(\d{1,2})日', full_date, text)

    def isodate(m):
        y, mo, d = int(m[1]), int(m[2]), int(m[3])
        try:
            datetime.date(y, mo, d)
        except ValueError:
            return m.group()
        return _digits_cn(m[1]) + '年' + quantity_cn(mo) + '月' + quantity_cn(d) + '日'

    text = re.sub(r'(?<![\d/.\-])((?:19|20)\d{2})-(\d{1,2})-(\d{1,2})(?![\d\-])',
                  isodate, text)
    # Chinese year: only in a full-date or explicit-era/year context, so that
    # 历时2026年 (a duration) is not read as a calendar year.
    text = re.sub(r'(?<![\d.])((?:19|20)\d{2})年(?=\s*\d{1,2}月)',
                  lambda m: _digits_cn(m[1]) + '年', text)
    text = re.sub(r'(?<=(?:公元|西元))((?:19|20)\d{2})年',
                  lambda m: _digits_cn(m[1]) + '年', text)

    # Default: 19xx/20xx年 is a calendar year (二零二六年开放). Only a nearby
    # duration word keeps it a quantity (历时2026年 → 二千零二十六年).
    def year_or_dur(m):
        before = text[max(0, m.start() - 4):m.start()]
        if any(before.endswith(w) for w in _DUR_WORDS):
            return m.group()
        return _digits_cn(m[1]) + '年'

    text = re.sub(r'(?<![\d.])((?:19|20)\d{2})年', year_or_dur, text)
    text = re.sub(r'(?<![\d.])(\d{1,2})月', lambda m: quantity_cn(int(m[1])) + '月', text)
    text = re.sub(r'(?<![\d.])(\d{1,2})日', lambda m: quantity_cn(int(m[1])) + '日', text)
    for i, s in enumerate(kept):
        text = text.replace('\x01%d\x01' % i, s)
    return text


def convert_percent(text):
    def rep(m):
        sign = '负' if m[1].startswith('-') else ''
        spoken = _decimal_cn(m[1].lstrip('-'))
        if spoken is None:
            return m.group()
        return sign + '百分之' + spoken
    return re.sub(r'(?<![\d.\-])(-\d{1,3}(?:\.\d+)?|\d{1,3}(?:\.\d+)?)[％%]', rep, text)


def convert_negative_temp(text):
    def neg(m):
        return '零下' + _decimal_cn(m[1].lstrip('-')) + '度'

    def pos(m):
        return _decimal_cn(m[1]) + '度'
    text = re.sub(r'(?<![\d.\-])(-\d{1,2}(?:\.\d+)?)\s*(?:℃|°C|°c)', neg, text)
    return re.sub(r'(?<![\d.\-])(\d{1,2}(?:\.\d+)?)\s*(?:℃|°C|°c)', pos, text)


def convert_quantities(text):
    pattern = (r'(?<![\d.])(-\d{1,9}(?:,\d{3})*(?:\.\d+)?|\d{1,9}(?:,\d{3})*(?:\.\d+)?)'
               r'\s*(' + _UNIT_RE + ')')

    def rep(m):
        raw = m[1]
        neg = raw.startswith('-')
        body = raw.lstrip('-')
        n_int = None if '.' in body else int(body.replace(',', ''))
        if not neg and n_int is not None and m.start() and text[m.start() - 1] == '第':
            spoken = quantity_cn(n_int)  # ordinal: 第2个 → 第二个, never 两
        else:
            spoken = _decimal_cn(body) if '.' in body else (
                _unit_cn(n_int) if not neg else quantity_cn(n_int))
        if spoken is None:
            return m.group()  # over-range: keep the single number verbatim
        return ('负' + spoken if neg else spoken) + m[2]
    return re.sub(pattern, rep, text)


def convert_currency(text):
    """¥1,234.50 -> 一千二百三十四点五零元; ¥156元 -> 一百五十六元 (no doubled 元)."""
    def rep(m):
        spoken = _decimal_cn(m[1])
        if spoken is None:
            return m.group()
        return spoken + (m[2] or '元')
    return re.sub(r'[¥￥]\s*(\d{1,9}(?:,\d{3})*(?:\.\d+)?)\s*(元|块钱|块)?', rep, text)


_SERIAL_LEAD = ('订单号', '快递单号', '运单号', '流水号', '取件码', '验证码', '单号', '编号')


def convert_serials(text):
    """Only explicit serial labels make digit runs read digit-by-digit."""
    lead = '|'.join(_SERIAL_LEAD)
    return re.sub(r'(?:' + lead + r')\s*[:：]?\s*(\d{4,20})(?![\d\-/])',
                  lambda m: m.group()[:m.start(1) - m.start()] +
                  '、'.join(_digits_cn(m[1])), text)


_PHONE_CTX = re.compile(r'电话|手机|热线|拨打|致电|来电|联系号码|咨询号码')
_PHONE_STRONG = re.compile(
    r'(?<![\d])(1[3-9]\d{9}|400[- ]?\d{3}[- ]?\d{4}|0\d{2,3}-?\d{7,8})'
    r'(?!\d|\s*[元块％%])')
_PHONE_LABELLED = re.compile(
    r'((?:客服电话|服务热线|热线|电话|手机号|联系号码|咨询号码)\s*[:：]?\s*)(\d{7,8})(?!\d)')


def _phone_fmt(v):
    digits = str.maketrans('0123456789', _DIGITS)
    groups = [v[:3], v[3:7], v[7:]] if len(v) >= 10 else [v[:4], v[4:]]
    return '，'.join(g.translate(digits) for g in groups)


def convert_phones(text, context=''):
    """Phone-shaped numbers only, anchored to phone words; nearby amounts,
    serial-protected and product numbers never qualify."""
    if not _PHONE_CTX.search((context or '') + ' ' + text):
        return text

    def strong(m):
        v = re.sub(r'\D', '', m.group())
        if not re.fullmatch(r'1[3-9]\d{9}|400\d{7,8}|0\d{2,3}\d{7,8}', v):
            return m.group()
        return _phone_fmt(v)

    def labelled(m):
        return m.group(1) + _phone_fmt(m.group(2))

    text = _PHONE_STRONG.sub(strong, text)
    return _PHONE_LABELLED.sub(labelled, text)


_RAIL_LETTER = {'G': '高', 'D': '动', 'C': '城', 'Z': '直', 'T': '特',
                'K': '快', 'Y': '游', 'L': '临'}


def convert_rail(text):
    """Rail tool context only: G8612 -> 高、八、六、一、二."""
    digits = str.maketrans('0123456789', _DIGITS)
    text = re.sub(r'(?<![A-Za-z0-9])([GDCZTKYL])(\d{1,5})(?![A-Za-z0-9])',
                  lambda m: _RAIL_LETTER[m[1]] + '、'.join(m[2].translate(digits)),
                  text)
    # Digit boundaries only: 123060 must not lose its 12306 prefix.
    text = re.sub(r'铁路\s*(?<![\d])12306(?![\d])', '铁路一二三零六', text)
    text = re.sub(r'(?<![\dA-Za-z])12306(?![\dA-Za-z])', '铁路一二三零六', text)
    return text


_FLIGHT_CODE = r'([A-Z]{2}|[A-Z][0-9]|[0-9][A-Z])(\d{3,4})'


def convert_flights(text):
    """Flight tool context only; spell IATA designator and flight digits, no airline inference."""
    def spoken(match):
        prefix = ' '.join(_DIGITS[int(c)] if c.isdigit() else c for c in match[1])
        return prefix + ' ' + '、'.join(_DIGITS[int(c)] for c in match[2])
    return re.sub(r'(?<![A-Za-z0-9])' + _FLIGHT_CODE + r'(?![A-Za-z0-9])', spoken, text)


# ---- token protection: URL / IP / version / ISO timestamp / serial / code ----
_LEAD_RE = '|'.join(_SERIAL_LEAD)
_PROTECT_RES = (
    r'https?://[^\s，。、,,;;)）】]+',
    r'(?<![A-Za-z0-9.])www\.[A-Za-z0-9\-._~%/?:#=&+]+',
    r'(?<![\d])\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?(?!\d)',
    r'(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])',
    r'(?<![\d.])(?:\d{1,3}\.){2,}\d{1,3}(?![\d.])',
    r'(?<![A-Za-z0-9])(?:' + _LEAD_RE + r')\s*[:：]?\s*\d[\d\-/]*\d(?![\d\-/])',
    r'`[^`]{1,64}`',
)
_PRODUCT_RES = r'(?<![A-Za-z0-9])[A-Z]{1,2}\d{4,6}(?![A-Za-z0-9])'
_PLACEHOLDER = '\x00%s\x00'
_PLACEHOLDER_RE = re.compile('\x00(.)\x00')


def _protect(text, rail=False, serial=True, flight=False):
    patterns = list(_PROTECT_RES)
    if not serial:
        patterns = [p for p in patterns if p is not _PROTECT_RES[5]]
    if not rail:
        patterns.append(_PRODUCT_RES)  # product codes like G8612 stay intact
    slots = []

    def keep(m):
        if flight and re.fullmatch(_FLIGHT_CODE,m.group()):
            return m.group()
        slots.append(m.group())
        return _PLACEHOLDER % chr(0xE000 + len(slots) - 1)

    for pat in patterns:
        text = re.sub(pat, keep, text)
    return text, slots


def _restore(text, slots):
    return _PLACEHOLDER_RE.sub(lambda m: slots[ord(m.group(1)) - 0xE000], text)


def speech_for_tts(reply, context='', rail=False, flight=False):
    """Return the TTS text. Never raises; each step fails independently so one
    anomaly cannot roll the whole speech back to the raw reply."""
    # A confirmed reminder acknowledgement uses a label colon before its clock.
    # Repair that TTS-only boundary, retaining all source reply/history content.
    reply = re.sub(r'(已设置好)[：:](?=(?:2[0-3]|[01]?\d)[:：][0-5]\d提醒)', r'\1，', reply)
    try:
        text, slots = _protect(reply, rail, flight=flight)
    except Exception:
        text, slots = reply, []
    for step in (convert_times, convert_dates, convert_percent,
                 convert_negative_temp, convert_currency):
        try:
            text = step(text)
        except Exception:
            pass
    if flight:
        try:
            text = convert_flights(text)
        except Exception:
            pass
    if rail:
        try:
            text = convert_rail(text)  # rail BEFORE quantities: G8612次 must
        except Exception:              # never become G八千六百一十二次
            pass
    try:
        text = convert_quantities(text)
    except Exception:
        pass
    try:
        text = _restore(text, slots)
    except Exception:
        return reply
    # Serials/phones run on re-protected text so URLs/IPs/ISO stay intact.
    try:
        guarded, slots2 = _protect(text, serial=False)
    except Exception:
        guarded, slots2 = text, []
    try:
        text = convert_serials(guarded)
    except Exception:
        text = guarded
    try:
        text = convert_phones(text, context)
    except Exception:
        pass
    try:
        text = _restore(text, slots2)
    except Exception:
        return reply
    return text
