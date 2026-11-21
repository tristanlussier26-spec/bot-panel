"""Ximbo : bot musique. C'est un Bot NG (dossier botng/) avec le plugin « ximbo » ; ce fichier active le logo Ximbo."""
import os

os.environ.setdefault("XIMBO_MODELE", "1")  # le plugin met son logo comme photo du bot

from botng.bot import main  # noqa: E402

if __name__ == "__main__":
    main()
