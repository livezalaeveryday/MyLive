"""Import PNGs only; ZIP sequence numbers are never platform gift IDs."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import zipfile


def import_catalog(source, destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    entries = []
    seen = set()
    with zipfile.ZipFile(source) as archive:
        for entry in archive.infolist():
            if entry.is_dir():
                continue
            path = PurePosixPath(entry.filename)
            if '\\' in entry.filename or path.is_absolute() or '..' in path.parts or path.suffix.lower() != '.png':
                raise ValueError('Unsafe or unsupported ZIP entry')
            name = path.name
            if not re.fullmatch(r'[A-Za-z0-9_. -]+\.png', name) or name in seen or entry.file_size > 10_000_000:
                raise ValueError('Unsafe, duplicate, or oversized PNG')
            seen.add(name)
            data = archive.read(entry)
            if not data.startswith(b'\x89PNG\r\n\x1a\n'):
                raise ValueError('Invalid PNG signature')
            target = destination / name
            if target.exists() and target.read_bytes() != data:
                raise ValueError('Refusing to overwrite existing asset')
            target.write_bytes(data)
            label = re.sub(r'^\d+_', '', path.stem).replace('_', ' ').strip()
            entries.append({'asset': name, 'names': [label] if label else [], 'verifiedIds': [], 'sha256': hashlib.sha256(data).hexdigest()})
    return {'version': 1, 'sourceSha256': hashlib.sha256(Path(source).read_bytes()).hexdigest(), 'entries': entries}


if __name__ == '__main__':
    root = Path(__file__).resolve().parents[1]
    catalog = import_catalog(root / 'Object/gifts_773.zip', root / 'server/static/gifts')
    target = root / 'server/static/gifts/catalog.json'
    if target.exists():
        raise SystemExit('Catalog already exists; refusing to replace user mappings')
    target.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
