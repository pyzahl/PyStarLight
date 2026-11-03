"""Hand an analysis (`<movie>.event.json`) to PySyzygos' observation GUI.

NEW CODE. PySyzygos (`pysyzygos gui-observation`, port 8088; its docs/observation_interface.md
section 3) takes the event.json and opens the report of the matching favorite.

The usual setup: the videos are copied to the computer that runs PySyzygos and analysed there.
The connection then needs no configuration: the observation GUI writes, at every start, a
connection file (readable by the user only), which `connection()` reads:

    Linux    ~/.config/pysyzygos/observation_api.json   (or $XDG_CONFIG_HOME/pysyzygos/)
    macOS    ~/Library/Application Support/PySyzygos/observation_api.json
    Windows  %APPDATA%\\PySyzygos\\observation_api.json

    {"app", "version", "url": "http://127.0.0.1:8088", "send": ".../api/obs/v1/analysis",
     "inbox": "<data folder>/observations/inbox", "data_folder", "token": "pyobs_...", ...}

    POST <send>     body: the event.json, Authorization: Bearer <token>
      -> 200 {"ok": true, "file", "asteroid", "date", "favorites": [...], "msg"}
         400 not an analysis, 401 key missing or wrong, 413 too large (20 MB)
    GET  <url>/api/obs/v1/      status (no key needed): {"ok", "version", "inbox": n}

Connection refused (the GUI is not running): the event.json is copied into its `inbox`
(temporary name, then renamed); PySyzygos lists it at its next start. A PySyzygos on another
computer (rare): [pysyzygos_observations] url / token (and $PYSYZYGOS_OBS_URL /
$PYSYZYGOS_OBS_TOKEN) override the file; not reached, the file is queued in
~/.cache/pystarlight/outbox and sent with the next one. Standard library only. The token is
never printed or put into a message.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

MAX_BYTES = 20 * 1024 * 1024
ERRORS = {400: 'not an analysis', 401: 'token refused (pysyzygos gui --new-token PyStarLight)',
          403: 'not allowed', 413: 'too large (20 MB)'}


class SendError(Exception):
    """The server answered with an error (not worth queueing: sending again gives the same)."""


API_PATH = '/api/obs/v1/analysis'


def connection_file() -> Path:
    """Where PySyzygos' observation GUI writes its connection file on this computer."""
    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Application Support' / 'PySyzygos' / 'observation_api.json'
    if os.name == 'nt':
        return Path(os.environ.get('APPDATA') or Path.home()) / 'PySyzygos' / 'observation_api.json'
    base = os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config'
    return Path(base) / 'pysyzygos' / 'observation_api.json'


def load_connection(path=None) -> dict | None:
    """The connection file as a dict, None when there is none (or it is not readable)."""
    p = Path(path) if path else connection_file()
    try:
        d = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def connection(cfg: dict | None = None) -> dict:
    """How to reach PySyzygos: {'source': 'config' | 'file' | 'none', 'url', 'send', 'token',
    'inbox', 'auto_send', 'file'}. The config's url (or $PYSYZYGOS_OBS_URL) wins over the
    connection file; without either, source 'none'."""
    if cfg is None:
        from .config import load
        cfg = load()
    sec = (cfg or {}).get('pysyzygos_observations') or {}
    out = {'source': 'none', 'url': '', 'send': '', 'token': '',
           'auto_send': bool(sec.get('auto_send', False)),
           'inbox': os.path.expanduser(str(sec['inbox'])) if sec.get('inbox') else '',
           'file': str(connection_file())}
    url = (os.environ.get('PYSYZYGOS_OBS_URL') or sec.get('url') or '').strip()
    token = (os.environ.get('PYSYZYGOS_OBS_TOKEN') or sec.get('token') or '').strip()
    if url:
        out.update(source='config', url=url, send=url.rstrip('/') + API_PATH, token=token)
        return out
    f = load_connection()
    if f and f.get('url'):
        out.update(source='file', url=str(f['url']), token=token or str(f.get('token') or ''),
                   send=str(f.get('send') or str(f['url']).rstrip('/') + API_PATH))
        out['inbox'] = out['inbox'] or str(f.get('inbox') or '')
    return out


def no_connection_message(conn: dict) -> str:
    return (f"PySyzygos' observation GUI has never run on this computer (no {conn['file']}): start "
            "'pysyzygos gui-observation' once, or for one on another computer set "
            "[pysyzygos_observations] url / token")


def settings(cfg: dict | None = None) -> tuple[str, str, bool]:
    """(url, token, auto_send) of connection()."""
    c = connection(cfg)
    return c['url'], c['token'], c['auto_send']


def drop_in_inbox(path, inbox: str) -> Path:
    """Put the event.json into PySyzygos' inbox as it stores them itself: '<created
    stamp>_<movie>.event.json', written under a temporary name and renamed (atomic)."""
    js = json.loads(Path(path).read_text())
    d = Path(inbox)
    d.mkdir(parents=True, exist_ok=True)
    stamp = re.sub(r'\D', '', str(js.get('created_utc') or ''))[:14]
    name = Path(path).name
    dest = d / (f'{stamp}_{name}' if stamp else name)
    fd, tmp = tempfile.mkstemp(dir=d, suffix='.tmp')
    with os.fdopen(fd, 'wb') as f:
        f.write(Path(path).read_bytes())
    os.replace(tmp, dest)
    return dest


def outbox() -> Path:
    return Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache') / 'pystarlight' / 'outbox'


def _request(url: str, token: str, path: str = '', body: bytes | None = None, timeout: float = 10.0) -> dict:
    headers = {'Accept': 'application/json'}
    if body is not None:
        headers['Content-Type'] = 'application/json'
    if token:
        headers['Authorization'] = f'Bearer {token}'
    req = urllib.request.Request(url.rstrip('/') + path, data=body, headers=headers,
                                 method='POST' if body is not None else 'GET')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            msg = json.load(e).get('msg', '')
        except Exception:
            msg = ''
        raise SendError(f'PySyzygos answered {e.code}: {ERRORS.get(e.code, e.reason)}'
                        + (f' ({msg})' if msg else '')) from None
    except ValueError:
        raise SendError('PySyzygos answered, but not with JSON') from None


def status(url: str, token: str = '', timeout: float = 5.0) -> dict:
    """GET /api/obs/v1/: {"ok", "version", "inbox"}."""
    return _request(url, token, '/api/obs/v1/', timeout=timeout)


def send(path, url: str, token: str = '', timeout: float = 10.0, send_url: str = '') -> dict:
    """POST one event.json to send_url (else url + /api/obs/v1/analysis); the server's answer.
    Raises SendError (the server said no), OSError (no server, timeout) or ValueError (no url,
    not a file to send)."""
    if not url:
        raise ValueError('no PySyzygos observation server: set [pysyzygos_observations] url')
    body = Path(path).read_bytes()
    if len(body) > MAX_BYTES:
        raise ValueError(f'{Path(path).name}: larger than 20 MB')
    json.loads(body)                        # only JSON goes out
    if send_url:
        return _request(send_url, token, '', body, timeout)
    return _request(url, token, API_PATH, body, timeout)


def queue(path) -> Path:
    """Keep a copy in the outbox, to send later."""
    box = outbox()
    box.mkdir(parents=True, exist_ok=True)
    dest = box / Path(path).name
    shutil.copyfile(path, dest)
    return dest


def send_queued(url: str, token: str = '', timeout: float = 10.0) -> list[str]:
    """Send what waits in the outbox (oldest first); returns the names sent. Stops at the first
    file the server cannot be reached for; a file the server refuses stays (renamed .refused)."""
    box = outbox()
    sent = []
    if not box.is_dir():
        return sent
    for p in sorted(box.glob('*.json'), key=lambda p: p.stat().st_mtime):
        try:
            send(p, url, token, timeout)
        except OSError:
            break
        except (SendError, ValueError):
            p.rename(p.with_suffix('.refused'))
            continue
        p.unlink()
        sent.append(p.name)
    return sent


def push_with(path, conn: dict, timeout: float = 10.0) -> str:
    """push() with a connection() dict."""
    if conn['source'] == 'none' and not conn['inbox']:
        return 'not sent: ' + no_connection_message(conn)
    return push(path, conn['url'], conn['token'], timeout, conn['inbox'], conn['send'])


def push(path, url: str, token: str = '', timeout: float = 10.0, inbox: str = '', send_url: str = '') -> str:
    """Send the queued files and this one; a line for the status bar. Not reached: into the
    inbox folder if given, else queued."""
    try:
        if not url:
            raise ConnectionRefusedError('no url')
        earlier = send_queued(url, token, timeout)
        answer = send(path, url, token, timeout, send_url)
    except OSError as e:
        reason = getattr(e, 'reason', e)
        if inbox:
            dest = drop_in_inbox(path, inbox)
            return (f'PySyzygos not running ({reason}): copied to the inbox as {dest.name} '
                    '(listed at its next start)')
        if not url:
            return 'not sent: no PySyzygos url and no inbox'
        dest = queue(path)
        return f'PySyzygos not reached ({reason}): queued in {dest.parent}'
    except (SendError, ValueError) as e:
        return f'not sent: {e}'
    msg = answer.get('msg') or ('received' if answer.get('ok') else 'not accepted')
    more = f' (+{len(earlier)} queued)' if earlier else ''
    return f'sent {Path(path).name} to PySyzygos{more}: {msg}'
