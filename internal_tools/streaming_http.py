"""Flush real tool events over SSE; JSON callers keep their existing response."""
import json
import threading


class ToolHTTPStream:
    def __init__(self, handler):
        self.handler=handler
        self.started=False
        self.closed=False
        self.broken=False
        self.lock=threading.Lock()
        self.stopped=threading.Event()
        self.thread=None

    def _write(self, raw):
        try:
            self.handler.wfile.write(f"{len(raw):x}\r\n".encode()+raw+b"\r\n")
            self.handler.wfile.flush()
        except OSError:
            self.broken=True
            self.stopped.set()
            raise

    def emit(self, event, value):
        raw=("event: "+event+"\ndata: "+json.dumps(value,ensure_ascii=False,separators=(",",":"))+"\n\n").encode()
        with self.lock:
            if self.closed or self.broken:raise ConnectionResetError("Tool stream closed")
            if not self.started:
                self.handler.send_response(200)
                for name,value in {"Content-Type":"text/event-stream; charset=utf-8","Cache-Control":"private, no-store",
                                   "X-Accel-Buffering":"no","X-Content-Type-Options":"nosniff","Transfer-Encoding":"chunked"}.items():
                    self.handler.send_header(name,value)
                self.handler.end_headers()
                self.started=True
                self.thread=threading.Thread(target=self._heartbeat,daemon=True)
                self.thread.start()
            self._write(raw)

    def progress(self, event):
        self.emit("aerisun.tool",event)

    def _heartbeat(self):
        while not self.stopped.wait(5):
            with self.lock:
                if self.closed or self.broken:return
                try:self._write(b": keep-alive\n\n")
                except OSError:return

    def close(self):
        self.stopped.set()
        with self.lock:
            self.closed=True
            if self.started and not self.broken:
                try:
                    self.handler.wfile.write(b"0\r\n\r\n")
                    self.handler.wfile.flush()
                except OSError:self.broken=True
        if self.thread:self.thread.join(timeout=1)
