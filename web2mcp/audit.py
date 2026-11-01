"""Private append-only JSONL audit trail. A local chain is not an external trust anchor."""
import fcntl
import getpass
import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()


class Audit:
    def __init__(self, root, *, sensitive=False):
        self.sensitive = sensitive
        self.root = Path(root) / 'audit'
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.root / 'fingerprint.key'
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(fd, 'r+b') as out:
            fcntl.flock(out, fcntl.LOCK_EX)
            self.key = out.read()
            if not self.key:
                self.key = secrets.token_bytes(32)
                out.write(self.key); out.flush(); os.fsync(out.fileno())
            if len(self.key) != 32: raise ValueError('Invalid audit key')

    def arguments_hash(self, arguments):
        return hmac.new(self.key, canonical(arguments), hashlib.sha256).hexdigest()

    def append(self, **event):
        path = self.root / 'calls.jsonl'
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, 'a+b') as out:
            fcntl.flock(out, fcntl.LOCK_EX)
            out.seek(0)
            lines = out.read().splitlines()
            previous = json.loads(lines[-1])['hash'] if lines else '0' * 64
            actor = getpass.getuser()
            if self.sensitive: actor = 'hmac:' + hmac.new(self.key, actor.encode(), hashlib.sha256).hexdigest()
            record = {'timestamp': datetime.now(timezone.utc).isoformat(), 'local_actor': actor,
                      'pid': os.getpid(), 'previous_hash': previous, **event}
            record['hash'] = hashlib.sha256(canonical(record)).hexdigest()
            out.write(canonical(record) + b'\n'); out.flush(); os.fsync(out.fileno())

    def verify(self):
        path = self.root / 'calls.jsonl'
        previous = '0' * 64
        count = 0
        if not path.exists(): return {'valid': True, 'records': 0}
        with path.open('rb') as stream:
            fcntl.flock(stream, fcntl.LOCK_SH)
            for line in stream:
                try:
                    record = json.loads(line); digest = record.pop('hash')
                except (ValueError, KeyError): return {'valid': False, 'record': count + 1}
                if record.get('previous_hash') != previous or hashlib.sha256(canonical(record)).hexdigest() != digest:
                    return {'valid': False, 'record': count + 1}
                previous = digest; count += 1
        return {'valid': True, 'records': count, 'head_hash': previous,
                'limit': 'Local chain detects inconsistent edits; cannot prove integrity against an owner rewriting the entire log.'}


SENSITIVE = ('password', 'secret', 'token', 'authorization', 'cookie', 'mac_key', 'uckey')


def redact(value):
    if isinstance(value, dict):
        return {key: '[REDACTED]' if any(word in key.lower() for word in SENSITIVE) else redact(item) for key, item in value.items()}
    if isinstance(value, list): return [redact(item) for item in value[:100]]
    if isinstance(value, str): return value[:16000]
    return value


def purge_diagnostics(root, days=7):
    if not isinstance(days, int) or days < 1: raise ValueError('Retention must be at least one day')
    folder = Path(root) / 'diagnostics'
    removed = 0
    for file in folder.glob('*.json'):
        if not file.is_symlink() and file.stat().st_mtime < time.time() - days*86400:
            file.unlink(); removed += 1
    return {'removed': removed, 'retention_days': days}
