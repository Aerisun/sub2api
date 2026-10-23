"""Adopt a newly selected proxy without closing in-flight model responses."""
import json
from pathlib import Path
import threading

import httpx
from qiniu_pool.transport import ProxySafeTransport


class _ResponseStream(httpx.SyncByteStream):
    def __init__(self,stream,release):self.stream,self.release=stream,release;self.closed=False
    def __iter__(self):yield from self.stream
    def close(self):
        if not self.closed:
            self.closed=True
            try:self.stream.close()
            finally:self.release()


class EgressTransport(httpx.BaseTransport):
    def __init__(self,route_file,**options):
        self.route_file=Path(route_file);self.options=options
        self.lock=threading.Lock();self.current=None;self.generations=[];self.closed=False
        self.http_versions={}
        self.refresh_lock=threading.Lock();self.stopped=threading.Event()
        self.refresh()
        self.watcher=threading.Thread(target=self._watch,name='nvidia-route-refresh',daemon=True)
        self.watcher.start()

    def _epoch(self):
        try:
            value=json.loads(self.route_file.read_text()).get('generation')
            if isinstance(value,str) and len(value)==64:return value
        except (OSError,ValueError):pass
        return None

    def refresh(self):
        # File reads and SSL context construction may block on the filesystem.
        # Neither operation belongs in the request's generation/refcount lock.
        with self.refresh_lock:
            epoch=self._epoch()
            with self.lock:
                if self.closed or self.current and (epoch is None or self.current['epoch']==epoch):
                    return
            candidate=ProxySafeTransport(**self.options)
            close=None
            with self.lock:
                if self.closed:
                    close=candidate
                else:
                    if self.current:
                        self.current['retired']=True
                        if self.current['active']==0:
                            close=self.current['transport'];self.generations.remove(self.current)
                    self.current={'epoch':epoch or 'initial','transport':candidate,'active':0,'retired':False}
                    self.generations.append(self.current)
            if close:close.close()

    def _watch(self):
        while not self.stopped.wait(.25):
            try:self.refresh()
            except (OSError,ValueError):pass  # Retain the existing healthy generation.

    def handle_request(self,request):
        with self.lock:
            if self.closed:raise RuntimeError('NVIDIA egress transport closed')
            generation=self.current;generation['active']+=1
        def release():
            retire=None
            with self.lock:
                generation['active']-=1
                if generation['retired'] and generation['active']==0:
                    retire=generation['transport'];self.generations.remove(generation)
            if retire:retire.close()
        try:response=generation['transport'].handle_request(request)
        except BaseException:release();raise
        version=response.extensions.get('http_version',b'unknown')
        if isinstance(version,bytes):version=version.decode('ascii',errors='replace')
        with self.lock:self.http_versions[version]=self.http_versions.get(version,0)+1
        response.stream=_ResponseStream(response.stream,release)
        return response

    def snapshot(self):
        with self.lock:return {'http_versions':dict(self.http_versions),'generations':len(self.generations),
            'active_requests':sum(g['active'] for g in self.generations)}

    def close(self):
        self.stopped.set()
        idle=[]
        with self.lock:
            self.closed=True
            for generation in list(self.generations):
                generation['retired']=True
                if generation['active']==0:
                    idle.append(generation['transport']);self.generations.remove(generation)
        for transport in idle:transport.close()
        self.watcher.join(timeout=2)
