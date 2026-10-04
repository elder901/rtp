"""Sincronização DF-e de uma empresa: baixa NF-e de interesse, registra cancelamentos e, se autorizado,
manifesta Ciência da Operação para obter o XML completo das notas que chegaram só como resumo.

Regras da NT 2014.002 respeitadas: quando não há mais documentos (cStat 137 ou ultNSU = maxNSU) a próxima
consulta só ocorre depois de 1 hora; cStat 656 (consumo indevido) também bloqueia por 1 hora.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from lxml import etree
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models, servicos
from app.config import settings
from app.dfe.sefaz import (CONSUMO_INDEVIDO, DOCUMENTOS_LOCALIZADOS, NENHUM_DOCUMENTO, ClienteSefaz, DocDistribuido,
                           ErroSefaz)
from app.parser.nfe import XMLInvalido, ler_nfe

log = logging.getLogger(__name__)
ESPERA = timedelta(hours=1)
MAX_LOTES_POR_EXECUCAO = 50   # 50 x 50 documentos
CANCELAMENTO = "110111"


@dataclass
class ResultadoSincronizacao:
    executada: bool = True
    completos: int = 0
    resumos: int = 0
    cancelados: int = 0
    manifestados: int = 0
    xml_obtidos: int = 0
    mensagens: list[str] = field(default_factory=list)


def _campos(xml: bytes) -> dict[str, str]:
    raiz = etree.fromstring(xml, parser=etree.XMLParser(resolve_entities=False, no_network=True))
    return {etree.QName(el).localname: (el.text or "").strip()
            for el in raiz.iter() if isinstance(el.tag, str) and len(el) == 0}


def _documento(s: Session, e: models.Empresa, chave: str) -> models.DocumentoFiscal | None:
    return s.scalar(select(models.DocumentoFiscal).where(
        models.DocumentoFiscal.empresa_id == e.id, models.DocumentoFiscal.chave == chave))


def _processar(s: Session, e: models.Empresa, d: DocDistribuido, res: ResultadoSincronizacao):
    if d.tipo == "procNFe":
        try:
            doc = ler_nfe(d.xml, arquivo=f"NSU {d.nsu}")
        except XMLInvalido as erro:
            res.mensagens.append(str(erro))
            return
        if servicos.gravar_documento(s, e, doc, d.xml, "dfe", d.nsu) in ("novo", "atualizado"):
            res.completos += 1
    elif d.tipo == "resNFe":
        c = _campos(d.xml)
        if _documento(s, e, c["chNFe"]):
            return
        s.add(models.DocumentoFiscal(
            empresa_id=e.id, chave=c["chNFe"], origem="dfe", nsu=d.nsu, direcao="entrada",
            situacao="cancelado" if c.get("cSitNFe") == "3" else "resumo",
            emissao=datetime.fromisoformat(c["dhEmi"]).replace(tzinfo=None) if c.get("dhEmi") else None,
            participante_cnpj=c.get("CNPJ", ""), participante_nome=c.get("xNome", "")[:200],
            valor=Decimal(c.get("vNF") or 0)))
        res.resumos += 1
    elif d.tipo in ("procEventoNFe", "resEvento"):
        c = _campos(d.xml)
        if c.get("tpEvento") == CANCELAMENTO:
            reg = _documento(s, e, c.get("chNFe", ""))
            if reg and reg.situacao != "cancelado":
                reg.situacao = "cancelado"
                res.cancelados += 1
    s.flush()


def _baixar_novos(s: Session, e: models.Empresa, cliente: ClienteSefaz, ctrl: models.ControleDFe,
                  res: ResultadoSincronizacao):
    for _ in range(MAX_LOTES_POR_EXECUCAO):
        ret = cliente.distribuicao(e.cnpj, e.uf, ult_nsu=ctrl.ult_nsu)
        ctrl.ultimo_status, ctrl.ultimo_motivo = ret.cstat, ret.motivo[:500]
        ctrl.atualizado_em = datetime.now()
        if ret.cstat == DOCUMENTOS_LOCALIZADOS:
            for d in ret.documentos:
                _processar(s, e, d, res)
            ctrl.ult_nsu, ctrl.max_nsu = ret.ult_nsu, ret.max_nsu
            s.commit()  # guarda o progresso a cada lote
            if int(ret.ult_nsu) >= int(ret.max_nsu):
                ctrl.proxima_consulta = datetime.now() + ESPERA
                return
            continue
        if ret.cstat in (NENHUM_DOCUMENTO, CONSUMO_INDEVIDO):
            ctrl.ult_nsu = ret.ult_nsu if ret.cstat == NENHUM_DOCUMENTO else ctrl.ult_nsu
            ctrl.max_nsu = ret.max_nsu if ret.cstat == NENHUM_DOCUMENTO else ctrl.max_nsu
            ctrl.proxima_consulta = datetime.now() + ESPERA
            if ret.cstat == CONSUMO_INDEVIDO:
                res.mensagens.append(f"SEFAZ bloqueou por consumo indevido (656): {ret.motivo}")
            return
        res.mensagens.append(f"SEFAZ {ret.cstat}: {ret.motivo}")
        ctrl.proxima_consulta = datetime.now() + ESPERA
        return
    res.mensagens.append("Ainda há documentos a baixar; a próxima execução continua de onde parou.")


def _completar_resumos(s: Session, e: models.Empresa, cliente: ClienteSefaz, res: ResultadoSincronizacao):
    """Manifesta Ciência nos resumos e busca o XML completo dos já manifestados."""
    resumos = list(s.scalars(select(models.DocumentoFiscal).where(
        models.DocumentoFiscal.empresa_id == e.id, models.DocumentoFiscal.situacao == "resumo")
        .order_by(models.DocumentoFiscal.emissao)))
    consultas = 0
    for reg in resumos:
        if reg.manifestado_em is None:
            ret = cliente.manifestar_ciencia(e.cnpj, reg.chave)
            if ret.aceito:
                reg.manifestado_em = datetime.now()
                res.manifestados += 1
            else:
                res.mensagens.append(f"Ciência da NF-e {reg.chave} recusada ({ret.cstat}): {ret.motivo}")
                continue
        if consultas >= settings.dfe_max_consultas_chave:
            continue
        consultas += 1
        ret = cliente.distribuicao(e.cnpj, e.uf, chave=reg.chave)
        for d in ret.documentos:
            if d.tipo == "procNFe":
                _processar(s, e, d, res)
                res.xml_obtidos += 1
        if ret.cstat == CONSUMO_INDEVIDO:
            res.mensagens.append("Consulta por chave bloqueada (656); continua na próxima execução.")
            break


def sincronizar(s: Session, e: models.Empresa, cliente: ClienteSefaz | None = None,
                agora: datetime | None = None) -> ResultadoSincronizacao:
    res = ResultadoSincronizacao()
    ctrl = e.controle_dfe or models.ControleDFe(empresa_id=e.id, ult_nsu="0", max_nsu="0")
    if e.controle_dfe is None:
        e.controle_dfe = ctrl
    agora = agora or datetime.now()
    if ctrl.proxima_consulta and ctrl.proxima_consulta > agora:
        res.executada = False
        res.mensagens.append(f"Próxima consulta permitida pela SEFAZ às {ctrl.proxima_consulta:%d/%m %H:%M}.")
        return res

    try:
        cliente = cliente or ClienteSefaz(servicos.carregar_certificado(e), e.ambiente_dfe)
        _baixar_novos(s, e, cliente, ctrl, res)
        if e.manifestar_ciencia:
            _completar_resumos(s, e, cliente, res)
        else:
            pendentes = s.scalar(select(models.DocumentoFiscal.id).where(
                models.DocumentoFiscal.empresa_id == e.id, models.DocumentoFiscal.situacao == "resumo").limit(1))
            if pendentes:
                res.mensagens.append("Há notas recebidas só como resumo. Ative a Ciência da Operação para obter o "
                                     "XML completo, ou importe esses XMLs manualmente.")
    except (ErroSefaz, ValueError) as erro:
        ctrl.ultimo_motivo = str(erro)[:500]
        ctrl.atualizado_em = datetime.now()
        res.mensagens.append(str(erro))
    return res


def sincronizar_pendentes(sessao_factory) -> list[tuple[str, ResultadoSincronizacao]]:
    """Roda para todas as empresas com certificado cuja próxima consulta já é permitida (usado pelo agendador)."""
    saida = []
    with sessao_factory() as s:
        ids = [e.id for e in s.scalars(select(models.Empresa).join(models.Certificado))]
    for id_ in ids:
        with sessao_factory() as s:
            e = s.get(models.Empresa, id_)
            r = sincronizar(s, e)
            if r.executada:
                log.info("DF-e %s: %s", e.cnpj, r)
            saida.append((e.cnpj, r))
    return saida
