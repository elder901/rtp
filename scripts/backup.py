"""Cópia diária do banco para E:\\RTP\\backup (guarda as 7 últimas), com log em dados/logs/backup.log.

Usado pela tarefa agendada "RTP - backup diário". À mão:
    .venv\\Scripts\\python scripts\\backup.py
RTP_BACKUP_DIR muda o destino.
"""
import logging
import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
os.chdir(RAIZ)
sys.path.insert(0, str(RAIZ))
(RAIZ / "dados" / "logs").mkdir(parents=True, exist_ok=True)
logging.basicConfig(filename=RAIZ / "dados" / "logs" / "backup.log", level=logging.INFO, encoding="utf-8",
                    format="%(asctime)s %(levelname)s %(message)s")

from app.manutencao import fazer_backup  # noqa: E402

try:
    arquivo = fazer_backup(Path(os.getenv("RTP_BACKUP_DIR", "E:/RTP/backup")), manter=7)
    logging.info("Backup gravado em %s (%.0f MB)", arquivo, arquivo.stat().st_size / 1e6)
except Exception:  # noqa: BLE001 — a falha fica no log, que é o que se consulta depois
    logging.exception("Backup falhou")
    sys.exit(1)
