"""Recognize complete SSE generations without waiting for HTTP EOF."""
import json


class SSETerminal:
    def __init__(self,complete_on_usage=False):
        self.pending=b''
        self.data=[]
        self.data_bytes=0
        self.complete_on_usage=complete_on_usage
        self.finish_seen=False
        self.usage_seen=False
        self.failed=False
        self.explicit_done=False
        self.finished=False

    def _complete(self):
        payload=b'\n'.join(self.data)
        if payload==b'[DONE]':
            self.explicit_done=True
            return True
        if not self.complete_on_usage or not payload:return False
        try:value=json.loads(payload)
        except (ValueError,UnicodeError):return False
        if not isinstance(value,dict):return False
        self.failed|=bool(value.get('error'))
        choices=value.get('choices')
        for choice in choices if isinstance(choices,list) else ():
            if isinstance(choice,dict) and choice.get('index',0)==0 and choice.get('finish_reason') in ('stop','length'):
                self.finish_seen=True
        usage=value.get('usage')
        # Some compatible providers report cumulative usage during decoding.
        # Only usage at/after finish can close an image with its final counts.
        if self.finish_seen and isinstance(usage,dict):
            self.usage_seen|=any(type(usage.get(k)) in (int,float) and usage[k]>0
                                 for k in ('total_tokens','prompt_tokens','completion_tokens'))
        return self.finish_seen and self.usage_seen and not self.failed

    def feed(self,chunk):
        """Return the end offset in this chunk, or None until a terminal frame.

        A marker inside model text is not a terminal frame. Handle CRLF,
        comments and boundaries anywhere within a chunk, including [DONE].
        """
        if self.finished:return 0
        previous=len(self.pending)
        value=self.pending+chunk
        position=0
        while True:
            end=value.find(b'\n',position)
            if end<0:break
            line=value[position:end].removesuffix(b'\r')
            position=end+1
            if not line:
                if self._complete():
                    self.finished=True;self.pending=b''
                    return position-previous
                self.data=[]
                self.data_bytes=0
            elif line.startswith(b'data:'):
                payload=line[5:].lstrip(b' ')
                self.data.append(payload)
                self.data_bytes+=len(payload)
                if self.data_bytes>2*1024*1024:raise ValueError('Oversized upstream event')
        self.pending=value[position:]
        if len(self.pending)>2*1024*1024:raise ValueError('Oversized upstream event')
        return None
