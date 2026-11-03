"""Apple-documented launch URL builder; launch/callback are NOT write receipts.
No listener is installed. A native adapter and authenticated endpoints are required.
"""
import json
import re
from urllib.parse import urlencode, urlsplit


def shortcut_url(shortcut_name, operation_id, return_url):
    if not isinstance(shortcut_name,str) or not shortcut_name.strip():
        raise ValueError('Missing installed shortcut name')
    if not re.fullmatch(r'[0-9a-f]{32}',operation_id):
        raise ValueError('Invalid host operation ID')
    url=urlsplit(return_url)
    if url.scheme!='https' or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError('Trusted stable HTTPS return URL required')
    callbacks={name:return_url+'?'+urlencode({'operation_id':operation_id,'transport':status})
               for name,status in [('x-success','finished'),('x-cancel','cancelled'),('x-error','error')]}
    data={'protocol_version':1,'operation_id':operation_id}
    # Opaque ID only. Native adapter resolves it via its already authorized channel.
    # Do not put private credentials, action fields, native IDs, or receipt tokens here.
    return 'shortcuts://x-callback-url/run-shortcut?'+urlencode({
        'name':shortcut_name,'input':'text','text':json.dumps(data,separators=(',',':')),**callbacks})
