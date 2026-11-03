"""Fetch pinned provider reference sheets for a local build, never a paid API.

The provider has not granted an open redistribution license for these sheets.
Keep generated files in .build; do not upload them or built bundles publicly.
"""
from pathlib import Path
import hashlib
import json
import re
import zipfile
import xml.etree.ElementTree as ET

NS = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}


def read_table(path, headers, expected_rows):
    with zipfile.ZipFile(path) as archive:
        if sum(i.file_size for i in archive.infolist()) > 20_000_000:
            raise ValueError('Reference sheet is unexpectedly large')
        strings = [''.join(node.itertext()) for node in
                   ET.fromstring(archive.read('xl/sharedStrings.xml')).findall('s:si', NS)]
        rows = []
        for row in ET.fromstring(archive.read('xl/worksheets/sheet1.xml')).findall('s:sheetData/s:row', NS):
            cells = {}
            for cell in row:
                column = re.sub(r'[0-9]', '', cell.get('r', ''))
                if column not in ('A', 'B'):
                    continue
                if cell.get('t') != 's':
                    raise ValueError('Unexpected reference cell type')
                cells[column] = strings[int(cell.findtext('s:v', '', NS))]
            rows.append([cells.get('A', ''), cells.get('B', '')])
    if rows[0] != headers or len(rows) - 1 != expected_rows:
        raise ValueError('Reference schema or row count changed')
    if any(not re.fullmatch('[A-Z]{3}', code) or not label for code, label in rows[1:]):
        raise ValueError('Invalid location reference')
    return rows[1:]


def prepare(root, cache, download):
    manifest = json.loads((root/'产品化/flight-data-sources.json').read_text())
    result = {'sources': [e['url'] for e in manifest['files']],
              'retrieved_on': manifest['retrieved_on'],
              'redistribution': 'not included in the GPL source distribution'}
    print('Preparing Juhe reference sheets for local use; no paid query. '
          'These data have no confirmed open redistribution license.', flush=True)
    for entry in manifest['files']:
        path = cache/'downloads'/entry['name']
        download(entry['url'], path, entry['sha256'], entry['size'])
        result[entry['table']] = read_table(path, entry['headers'], entry['rows'])
    target = cache/'flight-data/flight-locations.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    content = (json.dumps(result, ensure_ascii=False, indent=2)+'\n').encode('utf-8')
    if hashlib.sha256(content).hexdigest() != manifest['converted_sha256']:
        raise ValueError('Converted reference table does not match pinned metadata')
    target.write_bytes(content)
    return target
