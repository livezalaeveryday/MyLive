"""Desktop-only persisted identity. No secrets are logged or stored in the checkout."""
from __future__ import annotations
import csv
import io
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import time
from contextlib import contextmanager

class DesktopIdentityError(RuntimeError):
    pass

def _private_directory(path: Path) -> None:
    path.mkdir(parents=True,exist_ok=True)
    if os.name == 'nt':
        try:
            result=subprocess.run(['whoami','/user','/fo','csv','/nh'],capture_output=True,text=True,check=True,creationflags=subprocess.CREATE_NO_WINDOW)
            sid=next(csv.reader(io.StringIO(result.stdout)))[1]
            if not re.fullmatch(r'S-1-[0-9-]+',sid):raise ValueError('Invalid SID')
            subprocess.run(['icacls',str(path),'/inheritance:r','/grant:r',f'*{sid}:(OI)(CI)F','*S-1-5-18:(OI)(CI)F'],capture_output=True,check=True,creationflags=subprocess.CREATE_NO_WINDOW)
        except (OSError,subprocess.SubprocessError,ValueError,IndexError) as exc:
            raise DesktopIdentityError('Cannot secure desktop identity directory; check folder permissions.') from exc
    else:
        path.chmod(0o700)

@contextmanager
def _identity_lock(directory: Path):
    lock=directory/'.identity.lock'
    deadline=time.monotonic()+5
    while True:
        try:
            descriptor=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
            os.close(descriptor)
            break
        except FileExistsError:
            if time.monotonic()>=deadline:
                raise DesktopIdentityError('Desktop identity is locked by another launch. Close other instances; if none remain, remove .identity.lock and retry.')
            time.sleep(0.05)
    try:
        yield
    finally:
        lock.unlink()


def load_desktop_identity(data_dir: Path | None = None, explicit_token: str | None = None) -> dict[str,str]:
    if data_dir is None:
        configured=os.getenv('LCA_DATA_DIR')
        base=os.getenv('LOCALAPPDATA')
        if not configured and not base:
            raise DesktopIdentityError('LOCALAPPDATA is unavailable; configure LCA_DATA_DIR outside the application folder.')
        data_dir=Path(configured)/'MyLive' if configured else Path(base)/'MyLive'
    else:
        data_dir=Path(data_dir)/'MyLive'
    path=data_dir/'desktop-identity.json'
    _private_directory(data_dir)
    with _identity_lock(data_dir):
        if path.exists():
            try:
                if path.stat().st_size > 8192:raise ValueError('Oversized profile')
                profile=json.loads(path.read_text(encoding='utf-8'))
                if not isinstance(profile,dict) or set(profile)!={'schema','token','session'} or profile['schema']!=1:raise ValueError('Invalid schema')
                if not isinstance(profile['token'],str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,256}',profile['token']):raise ValueError('Invalid token')
                if not isinstance(profile['session'],str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,64}',profile['session']):raise ValueError('Invalid session')
            except (OSError,ValueError,TypeError) as exc:
                raise DesktopIdentityError('Desktop identity profile is unreadable or corrupt. Restore desktop-identity.json from backup; it was not replaced.') from exc
        else:
            profile={'schema':1,'token':secrets.token_urlsafe(32),'session':secrets.token_hex(16)}
            temp_name=None
            try:
                with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=data_dir,prefix='.identity-',delete=False) as target:
                    temp_name=target.name
                    json.dump(profile,target);target.flush();os.fsync(target.fileno())
                if os.name!='nt':os.chmod(temp_name,0o600)
                os.replace(temp_name,path)
            except OSError as exc:
                raise DesktopIdentityError('Cannot save desktop identity; check folder permissions and free disk space.') from exc
            finally:
                if temp_name and os.path.exists(temp_name):os.unlink(temp_name)
    override=explicit_token if explicit_token is not None else os.getenv('LCA_LOCAL_AUTH_TOKEN','')
    if override:
        override=override.strip()
        if not 24<=len(override)<=256 or any(c.isspace() for c in override):
            raise DesktopIdentityError('LCA_LOCAL_AUTH_TOKEN must be 24-256 characters without whitespace.')
    return {'token':override or profile['token'],'session':profile['session']}
