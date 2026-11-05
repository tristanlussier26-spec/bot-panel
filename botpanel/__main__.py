"""Lance le panel : python -m botpanel"""
import logging

from aiohttp import web

from botpanel import __version__
from botpanel.config import Settings
from botpanel.web import Panel


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s : %(message)s")
    settings = Settings()
    log = logging.getLogger("botpanel")
    log.info("Bot Panel %s — %s", __version__, settings.public_url)
    if not settings.admin_ids:
        log.warning("ADMIN_IDS est vide : personne pourra approuver les comptes. Mets ton ID Discord dans .env")
    if not settings.discord_client_id:
        log.warning("DISCORD_CLIENT_ID est vide : la connexion avec Discord marchera pas. Voir le README.")
    web.run_app(Panel(settings).app(), host=settings.host, port=settings.port, access_log=None)


if __name__ == "__main__":
    main()
