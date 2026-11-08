#!/usr/bin/env python3
"""Barreira anti-vazamento para repositório público. Falha (exit 1) se achar
algo que não deveria estar versionado. Roda no CI e pode rodar antes do commit:

    python scripts/check_public_safe.py
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN_FILES = [
    r"(^|/)creds\.env$", r"(^|/)\.env($|\.)", r"(^|/)channels\.json$",
    r"(^|/)last-run", r"(^|/)data/", r"(^|/)state/", r"\.log$", r"chrome-profile",
]
ALLOWED_FILES = [r"\.example(\.|$)", r"creds\.env\.example$"]

CONTENT_RULES = {
    "token JWT": re.compile(r"eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\."),
    "Authorization com token": re.compile(r"[Aa]uthorization\"?\s*[:=]\s*\"?Bearer\s+[A-Za-z0-9._-]{20,}"),
    "caminho de usuário Windows": re.compile(r"[A-Za-z]:\\+Users\\+(?!<)[A-Za-z0-9._-]+"),
    "chave privada": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "token Z-API/GitHub": re.compile(r"\b(ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b"),
}
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+\.)+[A-Za-z]{2,}")
EMAIL_OK = re.compile(r"(exemplo\.com|loja\.com|example\.(com|org)|users\.noreply\.github\.com|noreply\.anthropic\.com)$", re.I)
SKIP_CONTENT = re.compile(r"(package-lock\.json|\.enc$|check_public_safe\.py$)")


def main() -> int:
    files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.splitlines()
    problems = []
    for name in files:
        if any(re.search(p, name) for p in FORBIDDEN_FILES) and not any(re.search(p, name) for p in ALLOWED_FILES):
            problems.append(f"arquivo que não deve ser versionado: {name}")
            continue
        if SKIP_CONTENT.search(name):
            continue
        try:
            text = (ROOT / name).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for label, rule in CONTENT_RULES.items():
            if rule.search(text):
                problems.append(f"{name}: parece conter {label}")
        for match in EMAIL.finditer(text):
            if not EMAIL_OK.search(match.group(0)) and "@" in match.group(0):
                domain = match.group(0).split("@", 1)[1]
                if "." in domain and not re.match(r"^(x|b\.c|d\.e)$", domain):
                    problems.append(f"{name}: e-mail real? {match.group(0)[:3]}***@{domain}")
    if problems:
        print("BLOQUEADO — possível dado exposto:\n  " + "\n  ".join(sorted(set(problems))))
        return 1
    print(f"ok: {len(files)} arquivos versionados sem dados sensíveis conhecidos")
    return 0


if __name__ == "__main__":
    sys.exit(main())
