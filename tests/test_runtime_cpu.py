"""Limite CPU d'un bot : jamais plus que les processeurs de la machine Docker (sinon Docker refuse le conteneur)."""
from botpanel.config import Settings
from botpanel.runtime import BotRuntime

BOT = {"id": "abc", "image": "python:3.13-slim", "ram_mb": 1024, "cpu_percent": 300, "main_file": "bot.py",
       "language": "python", "startup_cmd": "", "port": None}


def test_cpu_limit_is_capped_to_the_machine():
    rt = BotRuntime(Settings())
    assert rt.cpu_limit(BOT) == 3.0                       # machine inconnue : la limite du bot telle quelle
    rt.host_cpus = 2.0                                    # VM à 2 processeurs : 300 % devient 2 cœurs
    assert rt.cpu_limit(BOT) == 2.0
    assert rt.container_config(BOT, {})["HostConfig"]["NanoCpus"] == 2_000_000_000
    assert rt.cpu_limit(dict(BOT, cpu_percent=50)) == 0.5  # en dessous : inchangé
    assert rt.container_config(dict(BOT, cpu_percent=100), {})["HostConfig"]["NanoCpus"] == 1_000_000_000
    # Quand la machine est pleine, chaque bot reçoit sa part selon son allocation : 300 % pèse 3 fois 100 %
    assert rt.container_config(BOT, {})["HostConfig"]["CpuShares"] == 3072
    assert rt.container_config(dict(BOT, cpu_percent=100), {})["HostConfig"]["CpuShares"] == 1024


async def test_docker_refusal_is_a_readable_error():
    """Un refus de Docker devient un message clair dans le panel (pas une « Erreur 500 »)."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import pytest
    from aiodocker.exceptions import DockerError

    from botpanel.runtime import RuntimeError_

    rt = BotRuntime(Settings())
    rt.ensure_image = AsyncMock()
    rt._get = AsyncMock(return_value=None)
    refused = DockerError(400, "range of CPUs is from 0.01 to 2.00, as there are only 2 CPUs available")  # comme aiodocker
    rt.docker = SimpleNamespace(containers=SimpleNamespace(create=AsyncMock(side_effect=refused)))
    with pytest.raises(RuntimeError_, match="Docker refuse de démarrer le bot : range of CPUs"):
        await rt.start(BOT, {})
