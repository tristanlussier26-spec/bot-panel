"""Déploiement d'un bot depuis un dépôt GitHub (ou tout dépôt git en HTTPS) : clone, puis pull."""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from botpanel.files import BotFiles, FileError

URL_RE = re.compile(r"^https://[A-Za-z0-9.-]+(:\d+)?/[A-Za-z0-9._~/-]+$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,100}$")


def validate(url: str, branch: str) -> None:
    if not URL_RE.match(url):
        raise FileError("Adresse git invalide : il faut une adresse https://, ex. https://github.com/toi/mon-bot")
    if branch and (not BRANCH_RE.match(branch) or branch.startswith("-")):
        raise FileError("Nom de branche invalide")


def _auth_url(url: str, token: str) -> str:
    if not token:
        return url
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, f"x-access-token:{quote(token, safe='')}@{parts.netloc}", parts.path, "", ""))


async def _git(*args: str, cwd: Path | None = None, timeout: int = 180) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true"}
    proc = await asyncio.create_subprocess_exec(
        "git", "-c", "safe.directory=*", "-c", "credential.helper=", *args, cwd=cwd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise FileError("git a pris trop de temps") from None
    text = out.decode(errors="replace")
    if proc.returncode != 0:
        lines = [l for l in text.strip().splitlines() if l.strip()]
        fatal = next((l for l in lines if l.startswith(("fatal:", "error:"))), lines[0] if lines else "git a échoué")
        if "could not read Username" in text or "Authentication failed" in text:
            fatal = "accès refusé : dépôt privé ? ajoute un jeton GitHub (lecture seule) dans les réglages Git du bot"
        raise FileError("git : " + fatal.removeprefix("fatal: ").removeprefix("error: "))
    return text


async def deploy(files: BotFiles, url: str, branch: str = "", token: str = "") -> str:
    """Clone le dépôt dans le dossier du bot (ou fait un pull s'il est déjà cloné). Retourne un résumé."""
    validate(url, branch)
    root = files.root
    auth = _auth_url(url, token)
    try:
        if (root / ".git").is_dir():
            await _git("remote", "set-url", "origin", auth, cwd=root)
            try:
                out = await _git("pull", "--ff-only", "origin", *([branch] if branch else []), cwd=root)
            finally:
                await _git("remote", "set-url", "origin", url, cwd=root)  # le jeton reste pas dans .git/config
            files._chown(root)
            return "Mis à jour : " + (out.strip().splitlines()[-1] if out.strip() else "déjà à jour")
        # Premier déploiement : clone dans un dossier temporaire puis copie (garde les fichiers déjà présents, ex. .env)
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "repo"
            args = ["clone", "--depth", "1", *(["--branch", branch] if branch else []), "--", auth, str(dest)]
            await _git(*args)
            await _git("remote", "set-url", "origin", url, cwd=dest)
            for item in dest.iterdir():
                target = root / item.name
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target)
                elif target.exists() or target.is_symlink():
                    target.unlink()
                shutil.move(str(item), target)
        files._chown(root)
        return "Dépôt cloné."
    except FileError as e:
        message = str(e)
        if token:
            message = message.replace(quote(token, safe=""), "***").replace(token, "***")
        raise FileError(message) from None
