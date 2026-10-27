"""Check publishable tracked content without printing any matched private value."""
from pathlib import Path
import json
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_NAMES = {'config.toml','personas.json','reply_policy.json','emoji_policy.json','tests.log'}
PATTERNS = {
    'private_key': r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
    'github_token': r'\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,})\b',
    'api_key': r'\bsk-[A-Za-z0-9]{20,}\b',
    'private_network': r'\b(?:192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b',
    'personal_qq_email': r'\b[1-9]\d{4,19}@qq\.com\b',
    'long_numeric_identifier': r'\b[1-9]\d{8,11}\b',
    'local_user_path': r'(?:[A-Za-z]:[\\/]+Users[\\/]|/home/)[A-Za-z0-9_.-]+',
}


def candidates():
    proc = subprocess.run(['git','-C',str(ROOT),'ls-files','-z'],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
    if proc.returncode == 0 and proc.stdout:
        return [ROOT / p for p in proc.stdout.decode('utf-8').split('\0') if p]
    return [p for p in ROOT.rglob('*') if p.is_file() and not set(p.relative_to(ROOT).parts).intersection({'.git','.venv','__pycache__'})]


def main():
    failures=[];paths=candidates()
    for path in paths:
        relative=path.relative_to(ROOT).as_posix()
        if (path.name in PRIVATE_NAMES or path.suffix in {'.db','.sqlite','.sqlite3','.log','.zip','.tgz'} or
                '.private.' in path.name or path.name.startswith('.env') or path.is_symlink()):
            failures.append({'path':relative,'rule':'private_file'})
            continue
        try:text=path.read_text(encoding='utf-8')
        except (UnicodeError,OSError):
            failures.append({'path':relative,'rule':'unexpected_non_text_file'});continue
        for label,pattern in PATTERNS.items():
            if re.search(pattern,text):failures.append({'path':relative,'rule':label})
    print(json.dumps(dict(files_checked=len(paths),passed=not failures,failures=failures)))
    return bool(failures)


if __name__=='__main__':raise SystemExit(main())
