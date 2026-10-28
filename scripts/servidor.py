"""Sobe o RTP sem janela, aberto para a rede interna (porta 8000), com log em dados/logs/servidor.log.

Usado pela tarefa agendada "RTP - servidor" (ao entrar no Windows). À mão:
    .venv\\Scripts\\python scripts\\servidor.py
RTP_HOST=127.0.0.1 deixa o servidor só para esta máquina; RTP_PORTA muda a porta.
"""
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
os.chdir(RAIZ)
sys.path.insert(0, str(RAIZ))
(RAIZ / "dados" / "logs").mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[RotatingFileHandler(RAIZ / "dados" / "logs" / "servidor.log", maxBytes=5_000_000, backupCount=3,
                                  encoding="utf-8")])

import uvicorn  # noqa: E402

uvicorn.run("app.api.main:app", host=os.getenv("RTP_HOST", "0.0.0.0"), port=int(os.getenv("RTP_PORTA", "8000")),
            log_config=None)
