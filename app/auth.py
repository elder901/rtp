"""Usuários, senhas e sessões da interface web.

A senha é guardada com scrypt (biblioteca padrão). A sessão é um token aleatório no cookie; no banco fica só o hash
dele — dá para encerrar sessões (sair, troca de senha, usuário desativado) sem chave de assinatura para guardar.

Acesso: o administrador vê todas as empresas; os demais, só as empresas (raiz do CNPJ) liberadas para eles.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import models

PAPEIS = {"admin": "Administrador", "gestor": "Gestor", "consulta": "Consulta"}
SENHA_MINIMA = 8
DURACAO_SESSAO = timedelta(hours=12)
COOKIE = "rtp_sessao"
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass(frozen=True)
class Logado:
    """O usuário da requisição, fora da sessão do banco."""
    id: int
    nome: str
    email: str
    papel: str
    raizes: frozenset[str] | None      # None: todas (administrador)

    @property
    def admin(self) -> bool:
        return self.papel == "admin"

    @property
    def pode_editar(self) -> bool:
        return self.papel in ("admin", "gestor")

    def ve(self, raiz: str) -> bool:
        return self.raizes is None or raiz in self.raizes


def _b64(dados: bytes) -> str:
    return base64.b64encode(dados).decode()


def hash_senha(senha: str) -> str:
    sal = secrets.token_bytes(16)
    h = hashlib.scrypt(senha.encode(), salt=sal, dklen=32, **_SCRYPT)
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${_b64(sal)}${_b64(h)}"


def confere_senha(senha: str, guardado: str) -> bool:
    try:
        _, n, r, p, sal, h = guardado.split("$")
        calculado = hashlib.scrypt(senha.encode(), salt=base64.b64decode(sal), n=int(n), r=int(r), p=int(p),
                                   dklen=len(base64.b64decode(h)))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(calculado, base64.b64decode(h))


def validar_senha(senha: str):
    if len(senha) < SENHA_MINIMA:
        raise ValueError(f"A senha precisa ter pelo menos {SENHA_MINIMA} caracteres.")


def _raizes(raizes) -> set[str]:
    saida = {r.strip() for r in raizes if r and r.strip()}
    if any(not (len(r) == 8 and r.isdigit()) for r in saida):
        raise ValueError("Empresa inválida (raiz do CNPJ com 8 dígitos).")
    return saida


def criar_usuario(s: Session, nome: str, email: str, senha: str, papel: str, raizes=()) -> models.Usuario:
    email = email.strip().lower()
    if not nome.strip():
        raise ValueError("Informe o nome.")
    if not _EMAIL.match(email):
        raise ValueError("E-mail inválido.")
    if papel not in PAPEIS:
        raise ValueError("Perfil inválido.")
    validar_senha(senha)
    if s.scalar(select(models.Usuario).where(models.Usuario.email == email)):
        raise ValueError(f"Já existe usuário com o e-mail {email}.")
    u = models.Usuario(nome=nome.strip(), email=email, senha_hash=hash_senha(senha), papel=papel, ativo=True)
    u.acessos = [models.AcessoEmpresa(raiz=r) for r in sorted(_raizes(raizes))]
    s.add(u)
    s.flush()
    return u


def definir_acessos(u: models.Usuario, raizes):
    novas = _raizes(raizes)
    u.acessos = [a for a in u.acessos if a.raiz in novas] + \
                [models.AcessoEmpresa(raiz=r) for r in sorted(novas - {a.raiz for a in u.acessos})]


def trocar_senha(s: Session, u: models.Usuario, senha: str):
    """Troca a senha e encerra as sessões abertas do usuário."""
    validar_senha(senha)
    u.senha_hash = hash_senha(senha)
    encerrar_sessoes(s, u.id)


def logado(u: models.Usuario) -> Logado:
    return Logado(u.id, u.nome, u.email, u.papel,
                  None if u.papel == "admin" else frozenset(a.raiz for a in u.acessos))


def verificar_login(s: Session, email: str, senha: str) -> models.Usuario | None:
    u = s.scalar(select(models.Usuario).where(models.Usuario.email == email.strip().lower()))
    if u is None:
        hash_senha(senha)               # mesmo custo com e sem usuário: não revela quais e-mails existem
        return None
    return u if u.ativo and confere_senha(senha, u.senha_hash) else None


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def abrir_sessao(s: Session, u: models.Usuario) -> str:
    token = secrets.token_urlsafe(32)
    agora = datetime.now()
    s.add(models.Sessao(usuario_id=u.id, token_hash=_hash_token(token), expira_em=agora + DURACAO_SESSAO))
    u.ultimo_acesso = agora
    s.execute(delete(models.Sessao).where(models.Sessao.expira_em < agora))     # limpa as vencidas
    return token


def usuario_da_sessao(s: Session, token: str) -> Logado | None:
    """O usuário dono do token, se a sessão está válida e ele ativo. A sessão se renova enquanto é usada."""
    sessao = s.scalar(select(models.Sessao).where(models.Sessao.token_hash == _hash_token(token)))
    agora = datetime.now()
    if sessao is None or sessao.expira_em < agora:
        return None
    u = s.get(models.Usuario, sessao.usuario_id)
    if u is None or not u.ativo:
        return None
    if sessao.expira_em - agora < DURACAO_SESSAO / 2:
        sessao.expira_em = agora + DURACAO_SESSAO
    return logado(u)


def encerrar_sessao(s: Session, token: str):
    s.execute(delete(models.Sessao).where(models.Sessao.token_hash == _hash_token(token)))


def encerrar_sessoes(s: Session, usuario_id: int):
    s.execute(delete(models.Sessao).where(models.Sessao.usuario_id == usuario_id))


def ha_usuarios(s: Session) -> bool:
    return s.scalar(select(models.Usuario.id).limit(1)) is not None
