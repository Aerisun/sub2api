"""Request-owned, short-lived in-memory image URLs for large VLM inputs."""
import base64
import binascii
from contextlib import contextmanager
import hashlib
import re
import secrets
import threading
import time
from urllib.parse import urlsplit

PREFIX='/v1/vision-assets/'
MIME={'png':'image/png','jpeg':'image/jpeg','webp':'image/webp'}


class AssetCapacityError(ValueError):pass


class ImageAssets:
    def __init__(self,origin,threshold=512*1024,max_bytes=512*1024*1024,ttl=300,clock=time.monotonic):
        parsed=urlsplit(origin)
        if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.query or parsed.fragment or not origin.endswith('/'):
            raise ValueError('Image asset origin must be a configured HTTPS directory')
        self.origin,self.threshold,self.max_bytes,self.ttl,self.clock=origin,threshold,max_bytes,ttl,clock
        self.lock=threading.Lock();self.entries={};self.bytes=0;self.published=0;self.fetched=0

    def _expire(self):
        now=self.clock()
        for token,entry in list(self.entries.items()):
            if entry[2]<=now:
                self.bytes-=len(entry[0]);del self.entries[token]

    def snapshot(self):
        with self.lock:
            self._expire()
            return {'active_images':len(self.entries),'active_bytes':self.bytes,
                    'published':self.published,'fetched':self.fetched,'capacity_bytes':self.max_bytes}

    @contextmanager
    def request(self,body):
        parts=[part for message in body.get('messages',[]) if isinstance(message,dict)
               for part in (message.get('content') if isinstance(message.get('content'),list) else [])
               if isinstance(part,dict) and part.get('type')=='image_url' and isinstance(part.get('image_url'),dict)]
        inline=[part['image_url'].get('url') for part in parts]
        inline=[url for url in inline if isinstance(url,str) and url.startswith('data:image/')]
        if sum(map(len,inline))<self.threshold:
            yield body;return
        replacements={};entries={};unique={}
        for url in inline:
            if url in replacements:continue
            match=re.fullmatch(r'data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/=\r\n]+)',url)
            if not match:continue
            try:raw=base64.b64decode(match[2].replace('\r','').replace('\n',''),validate=True)
            except (ValueError,binascii.Error):continue
            if len(raw)>16*1024*1024:raise AssetCapacityError('Image exceeds the 16 MiB upload limit')
            digest=(match[1],hashlib.sha256(raw).digest())
            if digest not in unique:
                token=secrets.token_urlsafe(32);unique[digest]=token
                entries[token]=(raw,MIME[match[1]],self.clock()+self.ttl)
            replacements[url]=self.origin+unique[digest]
        if not entries:
            yield body;return
        size=sum(len(entry[0]) for entry in entries.values())
        with self.lock:
            self._expire()
            if self.bytes+size>self.max_bytes:raise AssetCapacityError('Image transfer capacity is busy; retry shortly')
            self.entries.update(entries);self.bytes+=size;self.published+=len(entries)
        try:
            messages=[]
            for message in body.get('messages',[]):
                if not isinstance(message,dict) or not isinstance(message.get('content'),list):
                    messages.append(message);continue
                content=[]
                for part in message['content']:
                    if isinstance(part,dict) and part.get('type')=='image_url' and isinstance(part.get('image_url'),dict):
                        url=part['image_url'].get('url')
                        if isinstance(url,str) and url in replacements:part={**part,'image_url':{**part['image_url'],'url':replacements[url]}}
                    content.append(part)
                messages.append({**message,'content':content})
            yield {**body,'messages':messages}
        finally:
            with self.lock:
                for token in entries:
                    entry=self.entries.pop(token,None)
                    if entry:self.bytes-=len(entry[0])

    def serve(self,handler,head=False):
        if not handler.path.startswith(PREFIX):return False
        handler.connection.settimeout(20)
        handler.close_connection=True
        token=handler.path[len(PREFIX):]
        with self.lock:
            self._expire();entry=self.entries.get(token) if re.fullmatch(r'[A-Za-z0-9_-]{43}',token) else None
            if entry and not head:self.fetched+=1
        handler.send_response(200 if entry else 404)
        handler.send_header('Content-Type',entry[1] if entry else 'application/octet-stream')
        handler.send_header('Content-Length',str(len(entry[0])) if entry else '0')
        handler.send_header('Cache-Control','private, no-store, max-age=0')
        handler.send_header('Connection','close')
        handler.send_header('X-Content-Type-Options','nosniff')
        handler.end_headers()
        if entry and not head:
            raw=memoryview(entry[0])
            for start in range(0,len(raw),65536):handler.wfile.write(raw[start:start+65536])
        return True
