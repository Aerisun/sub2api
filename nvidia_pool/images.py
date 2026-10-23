"""Remove nonvisual PNG metadata from inline uploads without recoding pixels."""
import base64
import binascii
import struct

PNG=b'\x89PNG\r\n\x1a\n'
# Keep all rendering, color-management, transparency, animation and EXIF
# chunks. In particular eXIf can carry orientation, and must not be removed.
NONVISUAL={b'tEXt',b'zTXt',b'iTXt',b'tIME'}


def strip_png_metadata(raw):
    if not raw.startswith(PNG):return raw
    offset=8;chunks=[];removed=False;first=True
    while offset<len(raw):
        if offset+12>len(raw):return raw
        size=struct.unpack_from('>I',raw,offset)[0];end=offset+12+size
        if end>len(raw):return raw
        kind=raw[offset+4:offset+8]
        if first and (kind!=b'IHDR' or size!=13):return raw
        first=False
        if kind in NONVISUAL:removed=True
        else:chunks.append(raw[offset:end])
        offset=end
        if kind==b'IEND':
            # Do not rewrite malformed/trailing data; let normal validation
            # reject it, rather than making invalid input silently valid.
            if size or offset!=len(raw):return raw
            return PNG+b''.join(chunks) if removed else raw
    return raw


def normalize_images(body):
    messages=body.get('messages')
    if not isinstance(messages,list):return body
    normalized=[];changed=False
    for message in messages:
        if not isinstance(message,dict) or not isinstance(message.get('content'),list):
            normalized.append(message);continue
        parts=[];message_changed=False
        for part in message['content']:
            replacement=part
            if isinstance(part,dict) and part.get('type')=='image_url' and isinstance(part.get('image_url'),dict):
                value=part['image_url'].get('url')
                prefix='data:image/png;base64,'
                if isinstance(value,str) and value.startswith(prefix) and 128*1024<len(value)<=24*1024*1024:
                    try:
                        raw=base64.b64decode(value[len(prefix):].replace('\r','').replace('\n',''),validate=True)
                        clean=strip_png_metadata(raw)
                        if len(clean)<len(raw):
                            replacement={**part,'image_url':{**part['image_url'],'url':prefix+base64.b64encode(clean).decode('ascii')}}
                            message_changed=True
                    except (ValueError,binascii.Error):pass
            parts.append(replacement)
        normalized.append({**message,'content':parts} if message_changed else message)
        changed|=message_changed
    return {**body,'messages':normalized} if changed else body
