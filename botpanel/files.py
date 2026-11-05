"""Fichiers des bots : chaque bot a son dossier, on n'en sort jamais (chemins, liens symboliques, zip piégés)."""
from __future__ import annotations

import os
import shutil
import stat
import zipfile
from pathlib import Path, PurePosixPath

BOT_UID = BOT_GID = 1000      # utilisateur des conteneurs
MAX_EDIT_SIZE = 1024 * 1024   # 1 Mo : au-delà, on édite pas dans le navigateur
MAX_ZIP_FILES = 20000
HIDDEN = {".local", ".npm", ".cache", "node_modules", "__pycache__"}  # repliés par défaut dans la liste


def own(path: Path | str) -> None:
    """Donne le fichier à l'utilisateur des bots. Hors Docker et sans être root (développement), on laisse faire."""
    try:
        os.chown(path, BOT_UID, BOT_GID, follow_symlinks=False)
    except PermissionError:
        if os.geteuid() == 0:
            raise


class FileError(Exception):
    """Erreur affichable à l'utilisateur."""


class BotFiles:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        own(self.root)

    # ---- Chemins sûrs ---------------------------------------------------------------------
    def path(self, rel: str, *, allow_link: bool = False) -> Path:
        """Chemin absolu dans le dossier du bot, sans jamais en sortir ni passer par un lien symbolique.

        allow_link : le dernier morceau peut être un lien (pour pouvoir supprimer ou renommer un lien créé par le bot).
        """
        parts = PurePosixPath("/" + (rel or "").replace("\\", "/")).parts[1:]
        if any(p in ("..", "") for p in parts) or len(rel or "") > 1024:
            raise FileError("Chemin invalide")
        current = self.root
        for i, part in enumerate(parts):
            current = current / part
            if current.is_symlink() and not (allow_link and i == len(parts) - 1):
                raise FileError("Les liens symboliques sont pas permis")
        return current

    def rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def _chown(self, path: Path) -> None:
        if path.is_dir() and not path.is_symlink():
            for dirpath, dirnames, filenames in os.walk(path):
                own(dirpath)
                for name in filenames:
                    full = os.path.join(dirpath, name)
                    if not os.path.islink(full):
                        own(full)
        elif not path.is_symlink():
            own(path)

    # ---- Taille / quota --------------------------------------------------------------------
    def size(self) -> int:
        total = 0
        for dirpath, _, filenames in os.walk(self.root):
            for name in filenames:
                try:
                    st = os.lstat(os.path.join(dirpath, name))
                except OSError:
                    continue
                if stat.S_ISREG(st.st_mode):
                    total += st.st_size
        return total

    # ---- Opérations ---------------------------------------------------------------------------
    def list(self, rel: str = "") -> list[dict]:
        directory = self.path(rel)
        if not directory.is_dir():
            raise FileError("Dossier introuvable")
        entries = []
        for entry in os.scandir(directory):
            st = entry.stat(follow_symlinks=False)
            entries.append({
                "name": entry.name,
                "path": self.rel(Path(entry.path)),
                "dir": entry.is_dir(follow_symlinks=False),
                "symlink": entry.is_symlink(),
                "size": st.st_size,
                "modified": int(st.st_mtime),
                "hidden": entry.name in HIDDEN,
            })
        return sorted(entries, key=lambda e: (not e["dir"], e["name"].lower()))

    def read_text(self, rel: str) -> str:
        path = self.path(rel)
        if not path.is_file():
            raise FileError("Fichier introuvable")
        if path.stat().st_size > MAX_EDIT_SIZE:
            raise FileError("Fichier trop gros pour l'éditeur (max 1 Mo)")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as f:
            data = f.read(MAX_EDIT_SIZE + 1)
        if b"\x00" in data[:4096]:
            raise FileError("Fichier binaire : télécharge-le plutôt")
        return data.decode("utf-8", errors="replace")

    def write_text(self, rel: str, content: str) -> None:
        self.write_bytes(rel, content.encode("utf-8"))

    def write_bytes(self, rel: str, data: bytes) -> None:
        path = self.path(rel)
        if path == self.root or path.is_dir():
            raise FileError("Chemin invalide")
        self._mkdirs(path.parent)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        own(path)

    def _mkdirs(self, directory: Path) -> None:
        """Crée les dossiers manquants (appartenant à l'utilisateur des bots)."""
        missing = []
        while not directory.exists() and directory != self.root:
            missing.append(directory)
            directory = directory.parent
        for d in reversed(missing):
            d.mkdir()
            own(d)

    def mkdir(self, rel: str) -> None:
        path = self.path(rel)
        if path.exists() and not path.is_dir():
            raise FileError("Il y a déjà un fichier avec ce nom")
        self._mkdirs(path)

    def delete(self, rel: str) -> None:
        path = self.path(rel, allow_link=True)
        if path == self.root:
            raise FileError("Impossible de supprimer le dossier du bot au complet")
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)

    def rename(self, rel: str, new_rel: str) -> None:
        src, dst = self.path(rel, allow_link=True), self.path(new_rel)
        if src == self.root or not (src.exists() or src.is_symlink()):
            raise FileError("Fichier introuvable")
        if dst.exists() or dst.is_symlink():
            raise FileError("Il y a déjà un fichier à cet endroit")
        if dst == self.root or src in dst.parents:
            raise FileError("Impossible de déplacer un dossier dans lui-même")
        self._mkdirs(dst.parent)
        src.rename(dst)

    def extract_zip(self, rel: str, *, max_bytes: int) -> int:
        """Décompresse un .zip du bot là où il est. Retourne le nombre de fichiers extraits."""
        archive = self.path(rel)
        if not archive.is_file() or not zipfile.is_zipfile(archive):
            raise FileError("C'est pas un fichier .zip valide")
        target = archive.parent
        base = "" if target == self.root else self.rel(target) + "/"
        with zipfile.ZipFile(archive) as z:
            members = [m for m in z.infolist() if not m.is_dir()]
            if len(members) > MAX_ZIP_FILES:
                raise FileError("Trop de fichiers dans le zip")
            total = sum(m.file_size for m in members)
            if total > max_bytes:
                raise FileError(f"Le zip décompressé ({total // 1024 // 1024} Mo) dépasse l'espace disque qui te reste")
            members = [m for m in members if not stat.S_ISLNK(m.external_attr >> 16)]  # pas de liens venant d'un zip
            plan = [(m, self.path(base + m.filename)) for m in members]  # tout vérifier avant d'écrire quoi que ce soit
            count = 0
            for member, dest in plan:
                if dest.is_dir():
                    continue
                self._mkdirs(dest.parent)
                fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
                with z.open(member) as src, os.fdopen(fd, "wb") as out:
                    shutil.copyfileobj(src, out)
                own(dest)
                count += 1
        return count
