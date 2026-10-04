"""Orquestração da apuração assistida: credenciais, solicitações (limite diário), webhook, consulta de situação e
gravação incremental dos débitos/créditos apurados pela Receita."""
from __future__ import annotations

import logging
import re
import secrets
from datetime import datetime, time
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import models
from app.apuracao.cliente import TIPOS, ClienteApuracao, ErroApuracao
from app.config import settings
from app.seguranca import cifrar, decifrar

log = logging.getLogger(__name__)
ABERTAS = ("ABERTA", "PENDENTE", "EM_PROCESSAMENTO")


def salvar_credencial(s: Session, e: models.Empresa, client_id: str, client_secret: str, ambiente: str):
    if not client_id.strip() or not client_secret.strip():
        raise ValueError("Informe Client ID e Client Secret.")
    if ambiente not in ("prr", "pro"):
        raise ValueError("Ambiente inválido.")
    c = s.scalar(select(models.CredencialApuracao).where(models.CredencialApuracao.empresa_id == e.id))
    c = c or models.CredencialApuracao(empresa_id=e.id)
    c.client_id_cifrado, c.client_secret_cifrado = cifrar(client_id.strip().encode()), cifrar(client_secret.strip().encode())
    c.ambiente = ambiente
    s.add(c)


def credencial(s: Session, e: models.Empresa) -> models.CredencialApuracao | None:
    return s.scalar(select(models.CredencialApuracao).where(models.CredencialApuracao.empresa_id == e.id))


def cliente_da_empresa(s: Session, e: models.Empresa) -> ClienteApuracao:
    c = credencial(s, e)
    if not c:
        raise ErroApuracao("Empresa sem credencial da API de apuração.")
    return ClienteApuracao(decifrar(c.client_id_cifrado).decode(), decifrar(c.client_secret_cifrado).decode(), c.ambiente)


def url_webhook(token: str) -> str:
    return f"{settings.url_publica.rstrip('/')}/webhooks/apuracao-cbs/{token}"


def solicitacoes_hoje(s: Session, e: models.Empresa, tipo: str) -> int:
    hoje = datetime.combine(datetime.now().date(), time.min)
    return s.scalar(select(func.count()).select_from(models.SolicitacaoApuracao).where(
        models.SolicitacaoApuracao.empresa_id == e.id, models.SolicitacaoApuracao.tipo == tipo,
        models.SolicitacaoApuracao.tiquete.is_not(None), models.SolicitacaoApuracao.criado_em >= hoje)) or 0


def solicitar(s: Session, e: models.Empresa, tipo: str, cliente: ClienteApuracao | None = None) -> models.SolicitacaoApuracao:
    if tipo not in TIPOS:
        raise ValueError(f"Tipo inválido: {tipo}")
    if not settings.url_publica.startswith("https://"):
        raise ErroApuracao("A Receita só aceita solicitações com webhook HTTPS público. Configure RTP_URL_PUBLICA "
                           "(servidor publicado) ou use a importação manual do arquivo JSON.")
    if solicitacoes_hoje(s, e, tipo) >= settings.apuracao_limite_diario:
        raise ErroApuracao(f"Limite de {settings.apuracao_limite_diario} solicitações de {tipo} por dia atingido.")
    sol = models.SolicitacaoApuracao(empresa_id=e.id, tipo=tipo, token_webhook=secrets.token_urlsafe(32))
    s.add(sol)
    s.flush()
    try:
        cliente = cliente or cliente_da_empresa(s, e)
        sol.tiquete, tea = cliente.solicitar(tipo, e.cnpj, url_webhook(sol.token_webhook))
        sol.estado = "PENDENTE"
        sol.mensagem = f"Tempo estimado: {tea // 60} min" if tea else ""
    except ErroApuracao as erro:
        sol.estado, sol.mensagem = "ERRO", str(erro)[:500]
    return sol


# --- Arquivo de retorno ------------------------------------------------------------------------------------------

def _data(valor: str | None) -> datetime | None:
    if not valor:
        return None
    valor = re.sub(r"(\.\d{6})\d+", r"\1", valor.replace("Z", "+00:00"))  # frações com mais de 6 dígitos
    return datetime.fromisoformat(valor).replace(tzinfo=None)


def _pa(valor: str) -> str:
    mes, ano = valor.split("/")
    return f"{ano}-{int(mes):02d}"


def processar_arquivo(s: Session, e: models.Empresa, dados: dict, tipo: str | None = None) -> int:
    """Grava (insere ou atualiza) os registros do JSON de débitos ou créditos. Retorna quantos registros vieram."""
    ni = re.sub(r"\D", "", str(dados.get("ni", "")))
    if ni and ni[:8] != e.cnpj[:8]:
        raise ValueError(f"O arquivo é do CNPJ base {ni[:8]}, não da empresa {e.cnpj[:8]}.")
    total = 0
    for ap in dados.get("apuracao", []):
        lista_tipo = tipo or ("debitos" if "debitos" in ap else "creditos" if "creditos" in ap else None)
        if lista_tipo not in TIPOS:
            continue
        pa = _pa(ap["pa"])
        for item in ap.get(lista_tipo, []):
            cbs = item.get("cbs") or {}
            if lista_tipo == "debitos":
                saldo = cbs.get("saldoDevedor")
            else:
                saldo = (((cbs.get("apropriacao") or {}).get("utilizacao") or {}).get("naoUtilizado") or {}).get("saldoCredor")
            chave, origem = str(item.get("chave", "")), int(item.get("origem", 0))
            reg = s.scalar(select(models.RegistroApuracao).where(
                models.RegistroApuracao.empresa_id == e.id, models.RegistroApuracao.tipo == lista_tipo,
                models.RegistroApuracao.pa == pa, models.RegistroApuracao.chave == chave,
                models.RegistroApuracao.origem == origem))
            reg = reg or models.RegistroApuracao(empresa_id=e.id, tipo=lista_tipo, pa=pa, chave=chave, origem=origem)
            reg.documento = int(item.get("documento", 0))
            reg.emissao, reg.atualizacao = _data(item.get("emissao")), _data(item.get("atualizacao"))
            reg.apurado = Decimal(str(cbs.get("apurado", 0)))
            reg.saldo = Decimal(str(saldo)) if saldo is not None else None
            reg.valores = cbs
            s.add(reg)
            total += 1
        s.flush()
    return total


def _concluir(s: Session, e: models.Empresa, sol: models.SolicitacaoApuracao, cliente: ClienteApuracao, url: str):
    try:
        sol.registros = processar_arquivo(s, e, cliente.baixar(url), sol.tipo)
        sol.estado, sol.mensagem, sol.concluido_em = "CONCLUIDA", "", datetime.now()
    except (ErroApuracao, ValueError, KeyError) as erro:
        sol.mensagem = f"Arquivo pronto, mas não processado: {erro}"[:500]
        log.warning("Apuração %s (%s): %s", sol.tiquete, sol.tipo, sol.mensagem)


def receber_webhook(s: Session, token: str, payload: dict, cliente: ClienteApuracao | None = None) -> bool:
    """Trata o aviso da Receita. Retorna False se o token/tíquete não confere (responder 404)."""
    sol = s.scalar(select(models.SolicitacaoApuracao).where(models.SolicitacaoApuracao.token_webhook == token))
    if not sol or not sol.tiquete or payload.get("tiqueteSolicitacao") != sol.tiquete or sol.estado not in ABERTAS:
        return False
    e = s.get(models.Empresa, sol.empresa_id)
    if payload.get("codigoErro"):
        sol.estado = "ERRO"
        sol.mensagem = f"{payload.get('codigoErro')}: {payload.get('mensagemErro', '')}"[:500]
        return True
    if payload.get("urlAssinada"):
        _concluir(s, e, sol, cliente or cliente_da_empresa(s, e), payload["urlAssinada"])
    return True


def verificar_pendentes(s: Session, e: models.Empresa, cliente: ClienteApuracao | None = None) -> list[str]:
    """Plano B do webhook: consulta a situação das solicitações em aberto e baixa as concluídas."""
    mensagens = []
    pendentes = list(s.scalars(select(models.SolicitacaoApuracao).where(
        models.SolicitacaoApuracao.empresa_id == e.id, models.SolicitacaoApuracao.estado.in_(ABERTAS),
        models.SolicitacaoApuracao.tiquete.is_not(None))))
    if not pendentes:
        return ["Nenhuma solicitação em aberto."]
    cliente = cliente or cliente_da_empresa(s, e)
    for sol in pendentes:
        try:
            sit = cliente.situacao(sol.tiquete)
        except ErroApuracao as erro:
            mensagens.append(f"{sol.tipo}: {erro}")
            continue
        if sit.estado == "CONCLUIDA" and sit.url_assinada:
            _concluir(s, e, sol, cliente, sit.url_assinada)
        elif sit.estado == "ERRO":
            sol.estado, sol.mensagem = "ERRO", sit.erro[:500]
        elif sit.estado:
            sol.estado = sit.estado
        mensagens.append(f"{sol.tipo}: {sol.estado}" + (f" — {sol.registros} registros" if sol.estado == "CONCLUIDA" else "")
                         + (f" — {sol.mensagem}" if sol.mensagem else ""))
    return mensagens
