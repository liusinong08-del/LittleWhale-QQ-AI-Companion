"""Check a source publication tree without printing any potential secret value."""
from pathlib import Path
import re

root = Path(__file__).resolve().parents[1]
patterns = {
    'private key': re.compile(r'-----BEGIN (?:OPENSSH |RSA |EC |DSA )?PRIVATE KEY-----'),
    'service token': re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{25,}|github_pat_[A-Za-z0-9_]{30,}|sk-[A-Za-z0-9]{24,})\b'),
    'credential literal': re.compile(r"(?i)(?:api_key|access_token|agentToken|password_md5|secret_key)\s*[=:]\s*[\"'][A-Za-z0-9_\-/+=]{16,}[\"']"),
}
forbidden = {'downloads', 'dist', 'state', 'evidence', '.littlewhale', '.venv', 'venv', 'node_modules'}
private_suffixes = {'.pem', '.key', '.p12', '.pfx', '.keystore', '.jks', '.sqlite', '.sqlite3', '.db'}
failures = []
count = 0
for path in root.rglob('*'):
    relative = path.relative_to(root)
    if '.git' in relative.parts or '__pycache__' in relative.parts:
        continue
    if path.is_symlink():
        failures.append((relative.as_posix(), 'symlink requires review'))
        continue
    if not path.is_file():
        continue
    count += 1
    if any(p in forbidden or p.startswith('.littlewhale') for p in relative.parts) or path.suffix in private_suffixes or path.name in {'config.json', 'device.json', 'speech.json', 'panel-url', '.env', '.runtime-path'}:
        failures.append((relative.as_posix(), 'private runtime file'))
        continue
    if path.suffix.lower() in {'.png', '.jpg', '.jpeg', '.gif'}:
        continue  # ponytail: approved artwork only; future images need manual privacy review.
    try:
        content = path.read_text('utf-8')
    except UnicodeError:
        failures.append((relative.as_posix(), 'unreviewed binary'))
        continue
    for number, line in enumerate(content.splitlines(), 1):
        for kind, pattern in patterns.items():
            if pattern.search(line):
                failures.append((relative.as_posix() + ':' + str(number), kind))
assert not failures, 'Review potential private files/values (contents omitted): ' + repr(failures)
print('Publication check passed:', count, 'files; runtime data and common credential patterns checked.')
