"""Cifragem de segredos (certificado A1 e senha) com Fernet.

A chave vem de RTP_CHAVE_CRIPTO. Sem ela, em desenvolvimento, é gerada uma chave local em dados/chave.key
(fora do git). Em produção use variável de ambiente vinda de um cofre/KMS — perder a chave torna os
certificados guardados ilegíveis (basta reenviá-los).
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet

ARQUIVO_CHAVE = Path("dados/chave.key")


@lru_cache
def _fernet() -> Fernet:
    chave = os.getenv("RTP_CHAVE_CRIPTO")
    if not chave:
        if not ARQUIVO_CHAVE.exists():
            ARQUIVO_CHAVE.parent.mkdir(parents=True, exist_ok=True)
            ARQUIVO_CHAVE.write_bytes(Fernet.generate_key())
        chave = ARQUIVO_CHAVE.read_bytes().decode()
    return Fernet(chave)


def cifrar(dados: bytes) -> bytes:
    return _fernet().encrypt(dados)


def decifrar(dados: bytes) -> bytes:
    return _fernet().decrypt(dados)
