"""Resolve public Step image URLs to exact bytes before provider dispatch."""
import base64
from concurrent.futures import ThreadPoolExecutor
import http.client
import ipaddress
import socket
import time
from urllib.parse import urljoin,urlsplit


class ImageFetchError(ValueError):
    def __init__(self,message,status=400):super().__init__(message);self.status=status


def fetch_image(url,deadline=None):
    deadline=deadline or time.monotonic()+10
    for _ in range(4):
        target=urlsplit(url)
        if target.scheme not in ('https','http') or not target.hostname or target.username or target.password:
            raise ImageFetchError('Image URL must point to a public HTTP or HTTPS image')
        try:
            port=target.port or (443 if target.scheme=='https' else 80)
            addresses=socket.getaddrinfo(target.hostname,port,type=socket.SOCK_STREAM)
            if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
                raise ImageFetchError('Image URL must not resolve to a private address')
            remaining=deadline-time.monotonic()
            if remaining<=0:raise ImageFetchError('Image download timed out',502)
            factory=http.client.HTTPSConnection if target.scheme=='https' else http.client.HTTPConnection
            connection=factory(target.hostname,port,timeout=min(5,remaining))
            address=addresses[0][4][0]
            # Pin the vetted IP. TLS verification/SNI and the HTTP Host header
            # retain the original hostname; no second DNS lookup can rebind it.
            connection._create_connection=lambda _addr,timeout,source_address=None:socket.create_connection((address,port),timeout,source_address)
            try:
                path=target.path or '/'
                if target.query:path+='?'+target.query
                connection.request('GET',path,headers={'Accept':'image/png,image/jpeg,image/webp','User-Agent':'Aerisun-Vision/1.0','Connection':'close'})
                if connection.sock:connection.sock.settimeout(max(.01,min(5,deadline-time.monotonic())))
                response=connection.getresponse()
                if response.status in (301,302,303,307,308):
                    next_url=urljoin(url,response.getheader('Location',''))
                    if target.scheme=='https' and urlsplit(next_url).scheme!='https':
                        raise ImageFetchError('Image redirect must preserve HTTPS')
                    url=next_url;continue
                if response.status!=200:raise ImageFetchError('Image URL did not return an accessible image')
                length=response.getheader('Content-Length')
                if length and int(length)>16*1024*1024:raise ImageFetchError('Image exceeds 16 MiB')
                chunks=[];size=0
                while True:
                    remaining=deadline-time.monotonic()
                    if remaining<=0:raise ImageFetchError('Image download timed out',502)
                    sock=connection.sock or getattr(getattr(response.fp,'raw',None),'_sock',None)
                    if sock:sock.settimeout(min(5,remaining))
                    chunk=response.read1(65536)
                    if not chunk:break
                    size+=len(chunk)
                    if size>16*1024*1024:raise ImageFetchError('Image exceeds 16 MiB')
                    chunks.append(chunk)
                raw=b''.join(chunks)
                mime='png' if raw.startswith(b'\x89PNG\r\n\x1a\n') else 'jpeg' if raw.startswith(b'\xff\xd8\xff') else 'webp' if raw.startswith(b'RIFF') and raw[8:12]==b'WEBP' else None
                if not mime:raise ImageFetchError('Image URL did not return PNG, JPEG or WebP bytes')
                return 'data:image/'+mime+';base64,'+base64.b64encode(raw).decode()
            finally:connection.close()
        except ImageFetchError:raise
        except (OSError,ValueError,http.client.HTTPException):
            raise ImageFetchError('Could not download the public image',502) from None
    raise ImageFetchError('Image URL has too many redirects')


def inline_remote_images(body):
    urls={part['image_url']['url'] for message in body.get('messages',[]) if isinstance(message,dict)
          for part in (message.get('content') if isinstance(message.get('content'),list) else [])
          if isinstance(part,dict) and part.get('type')=='image_url' and isinstance(part.get('image_url'),dict)
          and isinstance(part['image_url'].get('url'),str) and part['image_url']['url'].startswith(('http://','https://'))}
    if not urls:return body
    if len(urls)>8:raise ImageFetchError('Step supports at most eight images')
    deadline=time.monotonic()+10
    with ThreadPoolExecutor(max_workers=min(4,len(urls))) as executor:
        replacements=dict(zip(urls,executor.map(lambda url:fetch_image(url,deadline),urls)))
    messages=[]
    for message in body['messages']:
        if not isinstance(message,dict) or not isinstance(message.get('content'),list):messages.append(message);continue
        content=[]
        for part in message['content']:
            if isinstance(part,dict) and part.get('type')=='image_url' and isinstance(part.get('image_url'),dict):
                url=part['image_url'].get('url')
                if isinstance(url,str) and url in replacements:part={**part,'image_url':{**part['image_url'],'url':replacements[url]}}
            content.append(part)
        messages.append({**message,'content':content})
    return {**body,'messages':messages}
