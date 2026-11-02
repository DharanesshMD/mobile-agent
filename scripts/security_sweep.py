#!/usr/bin/env python3
"""Pre-publication sweep: run before making the repository public.

    python3 scripts/security_sweep.py            # prints a green/red summary, exit code 1 if anything is red
    python3 scripts/security_sweep.py --verbose  # also lists every yellow finding

What it checks (it never prints a secret value, only where it was found):
  - the working tree AND the whole git history (every ref, including the old backup branch) for key-like strings, private keys, bearer or device
    tokens, and the exact values stored in your local .env
  - public IP addresses, the owner's e-mail addresses and personal paths in tracked text files (yellow: judge them)
  - that .env, data/, vault.key, state.json, uploads, APKs, backups and keys are ignored and not tracked
  - big files (tracked or in history), LICENSE and README links, placeholders still to fill, leftover backups next to the project
"""
import ipaddress
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERBOSE = "--verbose" in sys.argv
USE_COLOR = sys.stdout.isatty()
RESULTS: list[tuple[str, str, str]] = []  # (level, title, detail)

SECRET_PATTERNS = {
    "Meta/OpenAI-style key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    "AWS access key id": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    "Google API key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    "Slack token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    "private key block": re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |DSA |PGP )?PRIVATE KEY"),
    "bearer token": re.compile(r"[Bb]earer\s+(?!<|\$|\{|token|TOKEN|admin|\.\.\.)[A-Za-z0-9._~+/-]{24,}"),
    "device token (dev_...)": re.compile(r"\bdev_[A-Za-z0-9_-]{30,}"),
    "key assignment with a value": re.compile(r"(?i)\b(?:api[_-]?key|secret|token|password)\b\s*[=:]\s*['\"]?(?!<|\$|\{|your|changeme|xxx|\.\.\.|none|null|true|false)[A-Za-z0-9+/_-]{24,}"),
}
SAFE_IPS = {"8.8.8.8", "1.1.1.1"}  # well-known public resolvers used as examples
IP_SKIP_SUFFIXES = (".gradle", ".kts", ".properties", ".toml", ".xml")  # version numbers
IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
PERSONAL_PATH = re.compile(r"/Users/(?!Shared/)[A-Za-z0-9._-]+/")
KNOWN_FAKES = {"sk-abcdefghijklmnop1234"}  # fixtures that prove the secret detector works (tests/test_vault.py)
TEXT_SUFFIXES = {".md", ".py", ".sh", ".yaml", ".yml", ".json", ".kts", ".kt", ".xml", ".html", ".txt", ".toml", ".properties", ".svg", ".cff", ".env", ".example", ""}
SKIP_PARTS = {".venv", "build", ".gradle", "__pycache__", "node_modules", ".git"}


def run(*args: str, check: bool = False, text: bool = True) -> str:
    r = subprocess.run(args, cwd=ROOT, capture_output=True, text=text, errors="replace")
    return r.stdout if r.returncode == 0 or not check else ""


def add(level: str, title: str, detail: str = "") -> None:
    RESULTS.append((level, title, detail))


def tracked_files() -> list[str]:
    return [f for f in run("git", "ls-files", "-z").split("\0") if f]


def working_tree_files() -> list[str]:
    """Tracked files plus untracked files that are not ignored (what a `git add -A` would publish)."""
    others = [f for f in run("git", "ls-files", "-z", "--others", "--exclude-standard").split("\0") if f]
    return sorted(set(tracked_files()) | set(others))


def is_text(path: str) -> bool:
    p = Path(path)
    return not (set(p.parts) & SKIP_PARTS) and p.suffix.lower() in TEXT_SUFFIXES and (ROOT / path).is_file() and (ROOT / path).stat().st_size < 3_000_000


def env_values() -> dict[str, str]:
    out: dict[str, str] = {}
    f = ROOT / ".env"
    if f.exists():
        for line in f.read_text(errors="replace").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                v = v.strip().strip("'\"")
                if len(v) >= 12 and not v.lower().startswith(("http", "https")):
                    out[k.strip()] = v
    return out


def owner_emails() -> set[str]:
    mails = set(filter(None, [run("git", "config", "user.email").strip()]))
    mails |= {m.strip() for m in run("git", "log", "--all", "--format=%ae%n%ce").splitlines() if "@" in m}
    return {m for m in mails if not m.endswith("noreply.github.com") and not m.endswith("noreply@anthropic.com")}


def scan_tree() -> None:
    hits: dict[str, set[str]] = defaultdict(set)
    ips: dict[str, set[str]] = defaultdict(set)
    paths: set[str] = set()
    mails: dict[str, set[str]] = defaultdict(set)
    owner = owner_emails()
    envs = env_values()
    env_hits: dict[str, set[str]] = defaultdict(set)
    for f in working_tree_files():
        if not is_text(f) or f == "uv.lock":
            continue
        try:
            text = (ROOT / f).read_text(errors="replace")
        except OSError:
            continue
        for name, rx in SECRET_PATTERNS.items():
            if any(m.group(0) not in KNOWN_FAKES for m in rx.finditer(text)):
                hits[name].add(f)
        for k, v in envs.items():
            if v in text:
                env_hits[k].add(f)
        for ip in IPV4.findall(text):
            try:
                a = ipaddress.ip_address(ip)
            except ValueError:
                continue
            if a.is_global and ip not in SAFE_IPS and not f.endswith(IP_SKIP_SUFFIXES):
                ips[ip].add(f)
        if PERSONAL_PATH.search(text):
            paths.add(f)
        for m in owner:
            if m in text:
                mails[m].add(f)
    add("fail" if hits else "ok", "Key-like strings in the working tree", "; ".join(f"{n}: {', '.join(sorted(fs))}" for n, fs in hits.items()))
    add("fail" if env_hits else "ok", "Values from .env appear in tracked or publishable files", "; ".join(f"{k}: {', '.join(sorted(fs))}" for k, fs in env_hits.items()))
    add("warn" if ips else "ok", "Public IP addresses in text files", "; ".join(f"{ip} in {', '.join(sorted(fs))}" for ip, fs in sorted(ips.items())))
    add("warn" if mails else "ok", "The owner's e-mail address in text files (git metadata always has it; that is normal)", "; ".join(f"{m.split('@')[0][:3]}***@{m.split('@')[1]} in {', '.join(sorted(fs))}" for m, fs in mails.items()))
    add("warn" if paths else "ok", "Personal paths (/Users/<name>/) in text files", f"{len(paths)} files: " + ", ".join(sorted(paths)[:12]) + (" ..." if len(paths) > 12 else ""))


def scan_history() -> None:
    envs = env_values()
    findings: dict[str, set[str]] = defaultdict(set)
    env_findings: dict[str, set[str]] = defaultdict(set)
    proc = subprocess.Popen(["git", "log", "--all", "--no-color", "-p", "--no-ext-diff", "--format=@@COMMIT %h %D"], cwd=ROOT, stdout=subprocess.PIPE, text=True, errors="replace")
    commit, path = "?", "?"
    assert proc.stdout is not None
    for line in proc.stdout:
        if line.startswith("@@COMMIT "):
            commit = line.split()[1]
            continue
        if line.startswith("+++ b/"):
            path = line[6:].strip()
            continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        for name, rx in SECRET_PATTERNS.items():
            if any(m.group(0) not in KNOWN_FAKES for m in rx.finditer(line)):
                findings[name].add(f"{commit}:{path}")
        for k, v in envs.items():
            if v in line:
                env_findings[k].add(f"{commit}:{path}")
    proc.wait()
    add("fail" if findings else "ok", "Key-like strings anywhere in git history (all refs)", "; ".join(f"{n}: {', '.join(sorted(v)[:6])}" for n, v in findings.items()))
    add("fail" if env_findings else "ok", "Values from .env anywhere in git history", "; ".join(f"{k}: {', '.join(sorted(v)[:6])}" for k, v in env_findings.items()))


def check_refs() -> None:
    refs = [r for r in run("git", "for-each-ref", "--format=%(refname:short)").split() if r]
    extra = [r for r in refs if r not in ("main", "origin/main", "origin/HEAD", "origin")]
    add("warn" if extra else "ok", "Extra git refs (they publish old history if pushed)", ", ".join(extra))
    trailer = re.compile(r"(?i)co-authored-by:\s*claude|generated with \[?claude")
    per_ref = {}
    for r in refs:
        n = len(trailer.findall(run("git", "log", r, "--format=%B")))
        if n:
            per_ref[r] = n
    add("warn" if per_ref else "ok", "Claude co-author or 'generated with' lines in commit messages", ", ".join(f"{r}: {n}" for r, n in per_ref.items()))


def check_ignored_and_untracked() -> None:
    must_ignore = [".env", "data/app.db", "data/vault.key", "data/state.json", "data/uploads/x.jpg", "x.apk", "demo.mp4", "server.pem", "id.key", "something-backup-1/f", "foo-before.db"]
    leaking = [p for p in must_ignore if subprocess.run(["git", "check-ignore", "-q", p], cwd=ROOT).returncode != 0]
    add("fail" if leaking else "ok", "Sensitive paths are gitignored", ", ".join(leaking))
    bad = [f for f in tracked_files() if re.search(r"(^|/)(\.env|vault\.key|state\.json)$|\.(db|db-wal|db-shm|pem|key|apk|aab|mp4|mov|wav)$|(^|/)(uploads|data|runs)/", f) and f != ".env.example"]
    add("fail" if bad else "ok", "No secret, database, key, APK or recording is tracked", ", ".join(bad))
    big = [(f, (ROOT / f).stat().st_size) for f in tracked_files() if (ROOT / f).is_file() and (ROOT / f).stat().st_size > 5_000_000]
    add("warn" if big else "ok", "Tracked files over 5 MB", ", ".join(f"{f} ({s // 1_000_000} MB)" for f, s in big))
    objs = run("bash", "-c", "git rev-list --objects --all | git cat-file --batch-check='%(objecttype) %(objectsize) %(rest)' | awk '$1==\"blob\" && $2>5000000 {print $2, $3}'")
    add("warn" if objs.strip() else "ok", "Blobs over 5 MB anywhere in history", objs.strip().replace("\n", "; "))
    example = (ROOT / ".env.example").read_text() if (ROOT / ".env.example").exists() else ""
    filled = [l.split("=")[0] for l in example.splitlines() if "=" in l and not l.lstrip().startswith("#") and l.split("=", 1)[1].strip()
              and re.search(r"KEY|TOKEN|SECRET|PASSWORD", l.split("=")[0])]
    add("fail" if filled else "ok", ".env.example has no filled-in secret values (settings with defaults are fine)", "; ".join(filled))


def check_docs() -> None:
    add("ok" if (ROOT / "LICENSE").exists() else "fail", "LICENSE exists")
    broken: list[str] = []
    placeholders: list[str] = []
    for md in ("README.md", "docs/README.md"):
        f = ROOT / md
        if not f.exists():
            broken.append(f"{md} missing")
            continue
        text = f.read_text()
        for m in re.finditer(r"\]\(([^)\s]+)\)", text):
            target = m.group(1).split("#")[0]
            if not target or target.startswith(("http://", "https://", "mailto:")) or target in ("VIDEO_URL", "REPLAY_URL"):
                continue
            if not (f.parent / target).exists():
                broken.append(f"{md} -> {target}")
        for ph in ("VIDEO_URL", "REPLAY_URL", "OPEN_WEIGHTS_STATUS"):
            if ph in text:
                placeholders.append(f"{md}: {ph}")
    add("fail" if broken else "ok", "README and docs index links resolve", "; ".join(broken))
    add("warn" if placeholders else "ok", "Placeholders still to fill before submission", ", ".join(placeholders))
    parent = ROOT.parent
    stray = sorted(p.name for p in parent.iterdir() if p.name.startswith("mobile-agent-") and p != ROOT) if parent.exists() else []
    add("warn" if stray else "ok", "Backups next to the project (outside git, delete when no longer needed)", ", ".join(stray))


def main() -> int:
    if not (ROOT / ".git").exists():
        print("not a git repository")
        return 2
    scan_tree()
    scan_history()
    check_refs()
    check_ignored_and_untracked()
    check_docs()
    colors = {"ok": "\033[32m", "warn": "\033[33m", "fail": "\033[31m"} if USE_COLOR else {"ok": "", "warn": "", "fail": ""}
    reset = "\033[0m" if USE_COLOR else ""
    tags = {"ok": "[ OK ]", "warn": "[WARN]", "fail": "[FAIL]"}
    for level, title, detail in RESULTS:
        print(f"{colors[level]}{tags[level]}{reset} {title}")
        if detail and (level == "fail" or VERBOSE or level == "warn"):
            for part in detail.split("; "):
                print(f"         {part[:300]}")
    counts = {k: sum(1 for r in RESULTS if r[0] == k) for k in ("ok", "warn", "fail")}
    verdict = "RED: fix the [FAIL] items before the repository goes public" if counts["fail"] else ("YELLOW: review the [WARN] items" if counts["warn"] else "GREEN")
    print(f"\n{colors['fail' if counts['fail'] else 'warn' if counts['warn'] else 'ok']}{verdict}{reset}  ({counts['ok']} ok, {counts['warn']} warnings, {counts['fail']} failures)")
    print("Still on you: rotate the Meta API key before the repo goes public, delete the old backup branch and the backups next to the project.")
    return 1 if counts["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
