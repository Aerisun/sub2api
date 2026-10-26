"""Shared NVIDIA vision health and quality-first routing; no request content."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time
from image_hub.concurrency import configured_limit

PRIMARY='nvidia/nemotron-3.5-super-vl-120b-a12b'
OMNI='nvidia/nemotron-3-nano-omni-30b-a3b-reasoning'
# Quality tiers are stable across transient queue spikes. Latency balances only
# equivalent models; a fast small model must not displace the stronger primary.
DEFAULTS=[{'id':PRIMARY,'prior_ms':2000,'ready':True,'tier':0},
          {'id':OMNI,'prior_ms':2500,'ready':True,'tier':3}]
ROOT=Path(__file__).resolve().parents[1]

class VisionRouter:
    def __init__(self,path=None,models=None,clock=time.time,capacity=None):
        self.path=Path(path or os.environ.get('IMAGE_RECOGNITION_ROUTING_DB',ROOT/'data/image-recognition-routing.sqlite3'))
        self.models=models or DEFAULTS;self.clock=clock
        self.capacity=capacity if capacity is not None else configured_limit('IMAGE_RECOGNITION_CONCURRENCY',64)
        if type(self.capacity) is not int or not 1 <= self.capacity <= 1024:
            raise ValueError('Vision routing capacity must be between 1 and 1024')
        self._schema_lock=threading.Lock();self._initialized=False

    @contextmanager
    def db(self):
        self.path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        conn=sqlite3.connect(self.path,timeout=5,isolation_level=None)
        try:
            conn.execute('PRAGMA busy_timeout=5000')
            with self._schema_lock:
                if not self._initialized:
                    conn.execute('CREATE TABLE IF NOT EXISTS models(id TEXT PRIMARY KEY,latency REAL,ready INTEGER,cooldown REAL DEFAULT 0,failures INTEGER DEFAULT 0,successes INTEGER DEFAULT 0,updated REAL DEFAULT 0,last_success REAL DEFAULT 0)')
                    conn.execute('CREATE TABLE IF NOT EXISTS leases(token TEXT PRIMARY KEY,model TEXT,expires REAL)')
                    if 'last_success' not in {r[1] for r in conn.execute('PRAGMA table_info(models)')}:
                        try:conn.execute('ALTER TABLE models ADD COLUMN last_success REAL DEFAULT 0')
                        except sqlite3.OperationalError as error:
                            if 'duplicate column name' not in str(error):raise
                    for m in self.models:conn.execute('INSERT OR IGNORE INTO models(id,latency,ready) VALUES(?,?,?)',(m['id'],m['prior_ms'],int(m['ready'])))
                    os.chmod(self.path,0o600);self._initialized=True
            yield conn
        finally:conn.close()

    def has_ready(self,exclude=()):
        names={m['id'] for m in self.models}-set(exclude)
        with self.db() as c:
            return any(row[0] in names for row in c.execute(
                'SELECT id FROM models WHERE ready=1 AND cooldown<=?',(self.clock(),)))

    def release(self,lease):
        # Abandoning a reservation is not an upstream success or failure.
        with self.db() as c:c.execute('DELETE FROM leases WHERE token=?',(lease[0],))

    def acquire(self,exclude=()):
        now=self.clock();names={m['id'] for m in self.models}
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            try:
                c.execute('DELETE FROM leases WHERE expires<?',(now,))
                rows=c.execute('SELECT m.id,m.latency,m.ready,m.cooldown,COUNT(l.token) FROM models m LEFT JOIN leases l ON l.model=m.id GROUP BY m.id').fetchall()
                # The tool admission limit is shared by every process. Do not
                # push a healthy primary onto a smaller fallback at eight calls.
                # Keep the same bound here for all routes, including retries.
                if sum(r[4] for r in rows if r[0] in names) >= self.capacity:
                    c.commit();return None
                rows=[r for r in rows if r[0] in names and r[0] not in exclude and r[2] and r[3]<=now]
                if not rows:c.commit();return None
                settings={m['id']:m for m in self.models}
                chosen=min(rows,key=lambda r:(settings[r[0]].get('tier',0),
                    r[1]*settings[r[0]].get('weight',1)*(1+r[4]/16)))
                token=secrets.token_hex(16);c.execute('INSERT INTO leases VALUES(?,?,?)',(token,chosen[0],now+120));c.commit()
                return token,chosen[0]
            except BaseException:c.rollback();raise

    def finish(self,lease,success,latency_ms=None,transient=False,partial_failure=False):
        token,model=lease;now=self.clock()
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            try:
                exists=c.execute('DELETE FROM leases WHERE token=?',(token,)).rowcount
                if exists and success:
                    c.execute('UPDATE models SET ready=1,cooldown=0,failures=0,successes=successes+1,latency=0.7*latency+0.3*?,updated=?,last_success=? WHERE id=?',(max(1,latency_ms or 2000),now,now,model))
                elif exists and transient:
                    failures,last_success=c.execute('SELECT failures,last_success FROM models WHERE id=?',(model,)).fetchone();failures+=1
                    # A single failed request must not evict a healthy route
                    # while the other 63 requests are still completing. The
                    # current request already excludes its failed model.
                    if failures>=3 and now-last_success>=60:
                        c.execute('UPDATE models SET ready=0,failures=?,cooldown=?,updated=? WHERE id=?',(failures,now+(120 if partial_failure else min(120,10*2**min(failures-3,4))),now,model))
                    else:c.execute('UPDATE models SET failures=?,updated=? WHERE id=?',(failures,now,model))
                c.commit()
            except BaseException:c.rollback();raise

    def probe(self,model,success,latency_ms=None,transient=False):
        now=self.clock()
        with self.db() as c:
            if success:
                # A fresh complete known-answer image is stronger evidence
                # than an earlier transient cooldown. Restore availability now.
                c.execute('UPDATE models SET ready=1,cooldown=0,failures=0,latency=0.7*latency+0.3*?,updated=?,last_success=? WHERE id=?',(max(1,latency_ms or 2000),now,now,model))
            elif transient:
                c.execute('BEGIN IMMEDIATE')
                try:
                    failures,last_success=c.execute('SELECT failures,last_success FROM models WHERE id=?',(model,)).fetchone()
                    failures+=1
                    if failures>=3 and now-last_success>=60:
                        c.execute('UPDATE models SET ready=0,cooldown=MAX(cooldown,?),failures=?,updated=? WHERE id=?',(now+30,failures,now,model))
                    else:
                        c.execute('UPDATE models SET failures=?,updated=? WHERE id=?',(failures,now,model))
                    c.commit()
                except BaseException:c.rollback();raise
            else:c.execute('UPDATE models SET ready=0,cooldown=MAX(cooldown,?),failures=failures+1,updated=? WHERE id=?',(now+30,now,model))

    def snapshot(self):
        with self.db() as c:
            c.row_factory=sqlite3.Row
            return [dict(row) for row in c.execute('SELECT * FROM models ORDER BY latency')]

    def active_count(self):
        with self.db() as c:
            return c.execute('SELECT COUNT(*) FROM leases WHERE expires>?',(self.clock(),)).fetchone()[0]
