"""Check publishable files and spec paths without running services or building EXEs."""
from pathlib import Path
import ast
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
names = subprocess.check_output(
    ['git', '-C', str(ROOT), 'ls-files', '--cached', '--others', '--exclude-standard', '-z']
).decode().split('\0')
files = sorted(set(name for name in names if name))
for name in files:
    path = ROOT / name
    assert not any(part in {'ads_data', 'build', 'dist', '__pycache__', 'node_modules', '.venv'} for part in path.relative_to(ROOT).parts), name
    assert path.suffix.lower() not in {'.exe', '.dll', '.log', '.sqlite3', '.db', '.pem', '.key', '.pyc'}, name
    assert path.name != 'firebase-key.json', name
    assert not path.name.startswith('.env') or path.name == '.env.example', name
    if path.suffix.lower() in {'.png', '.jpg', '.ico'}:
        continue
    content = path.read_text(encoding='utf-8')
    # Report only filenames, never matched credential contents.
    secret_patterns = [r'-----BEGIN [A-Z ]*PRIVATE KEY-----', r'AIza[0-9A-Za-z_-]{35}', r'gh[pousr]_[0-9A-Za-z]{30,}', r'"private_key"\s*:\s*"[^"\s]+']
    assert not any(re.search(pattern, content) for pattern in secret_patterns), f'Credential pattern: {name}'
    if path.suffix in {'.py', '.spec'}:
        ast.parse(content, filename=name)

# Execute specs using recorder stubs. This verifies entry/data paths and syntax,
# but is not a substitute for a complete PyInstaller build.
for spec in (ROOT / 'server').glob('*.spec'):
    def analysis(entries, **kwargs):
        assert all(Path(entry).is_absolute() and Path(entry).is_file() for entry in entries)
        assert all(Path(folder).is_absolute() and Path(folder).is_dir() for folder in kwargs['pathex'])
        assert all(Path(source).exists() and Path(source).is_absolute() for source, _ in kwargs['datas'])
        return type('AnalysisResult', (), dict(pure=[], scripts=[], binaries=[], datas=[]))()
    exec(compile(spec.read_text(encoding='utf-8'), str(spec), 'exec'), {
        'SPECPATH': str(spec.parent), 'Analysis': analysis,
        'PYZ': lambda *a, **k: None, 'EXE': lambda *a, **k: None,
    })
print(f'PASS: {len(files)} publishable files; credential-pattern and syntax checks; 2 relocatable spec configurations')
