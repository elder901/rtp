"""Conciliação: CBS destacada nos XMLs da base x débitos/créditos apurados pela Receita, nota a nota.

Responde: a base de XMLs está completa (há notas que a Receita apurou e não temos?) e o destaque bate com o que a
Receita considerou? Saídas são comparadas com os débitos; entradas com os créditos. Só o registro de origem 0
(NORMAL) entra na comparação; ajustes (devoluções, cancelamentos, perecimento...) aparecem à parte.
"""
from __future__ import annotations

import gzip
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.parser.nfe import ler_nfe

ZERO = Decimal("0")
TOLERANCIA = Decimal("0.01")
ORIGEM_NORMAL = 0
SITUACOES = ("divergente", "so_receita", "cancelada", "so_xml", "ok")


@dataclass
class LinhaConciliacao:
    tipo: str            # debitos | creditos
    pa: str
    chave: str
    participante: str
    cbs_xml: Decimal | None
    cbs_receita: Decimal | None
    ajustes: Decimal = ZERO
    situacao: str = ""   # ok | divergente | so_receita | so_xml | cancelada

    @property
    def diferenca(self) -> Decimal | None:
        if self.cbs_xml is None or self.cbs_receita is None:
            return None
        return self.cbs_receita - self.cbs_xml


@dataclass
class Conciliacao:
    linhas: list[LinhaConciliacao] = field(default_factory=list)

    def resumo(self) -> list[dict]:
        saida = []
        for tipo in ("debitos", "creditos"):
            ls = [l for l in self.linhas if l.tipo == tipo]
            saida.append({
                "tipo": tipo, "notas": len(ls),
                "cbs_xml": sum((l.cbs_xml or ZERO for l in ls), ZERO),
                "cbs_receita": sum((l.cbs_receita or ZERO for l in ls), ZERO),
                "ajustes": sum((l.ajustes for l in ls), ZERO),
                **{s: sum(1 for l in ls if l.situacao == s) for s in SITUACOES},
            })
        return saida


def conciliar(s: Session, e: models.Empresa, pa_inicio: str | None = None, pa_fim: str | None = None) -> Conciliacao:
    """pa_* no formato AAAA-MM. Sem filtro, usa os períodos que a Receita já retornou."""
    registros = list(s.scalars(select(models.RegistroApuracao).where(models.RegistroApuracao.empresa_id == e.id)))
    if pa_inicio:
        registros = [r for r in registros if r.pa >= pa_inicio]
    if pa_fim:
        registros = [r for r in registros if r.pa <= pa_fim]
    if not registros:
        return Conciliacao()  # sem dados da Receita no período não há o que conciliar
    periodos = {r.pa for r in registros}
    receita: dict[tuple[str, str], dict] = defaultdict(lambda: {"normal": None, "ajustes": ZERO, "pa": ""})
    for r in registros:
        k = (r.tipo, r.chave)
        if r.origem == ORIGEM_NORMAL:
            receita[k]["normal"] = (receita[k]["normal"] or ZERO) + r.apurado
            receita[k]["pa"] = r.pa
        else:
            receita[k]["ajustes"] += r.apurado
            receita[k]["pa"] = receita[k]["pa"] or r.pa

    # CBS destacada nos XMLs dos mesmos períodos
    nossos: dict[tuple[str, str], tuple[Decimal, str, str]] = {}
    docs = s.scalars(select(models.DocumentoFiscal).where(
        models.DocumentoFiscal.empresa_id == e.id, models.DocumentoFiscal.situacao == "completo"))
    for d in docs:
        if not d.emissao:
            continue
        pa = f"{d.emissao:%Y-%m}"
        if (periodos and pa not in periodos) or (pa_inicio and pa < pa_inicio) or (pa_fim and pa > pa_fim):
            continue
        doc = ler_nfe(gzip.decompress(d.xml_gz))
        cbs = sum((i.ibscbs.vcbs for i in doc.itens if i.ibscbs), ZERO)
        tipo = "debitos" if d.direcao == "saida" else "creditos"
        nossos[(tipo, d.chave)] = (cbs, pa, d.participante_nome)

    canceladas = set(s.scalars(select(models.DocumentoFiscal.chave).where(
        models.DocumentoFiscal.empresa_id == e.id, models.DocumentoFiscal.situacao == "cancelado")))

    resultado = Conciliacao()
    for k in sorted(set(receita) | set(nossos)):
        tipo, chave = k
        rec = receita.get(k)
        xml = nossos.get(k)
        if xml and xml[0] == ZERO and not rec:
            continue  # nota sem CBS destacada e que a Receita não apurou: nada a conciliar
        linha = LinhaConciliacao(
            tipo=tipo, pa=(rec or {}).get("pa") or (xml[1] if xml else ""), chave=chave,
            participante=xml[2] if xml else "", cbs_xml=xml[0] if xml else None,
            cbs_receita=rec["normal"] if rec else None, ajustes=rec["ajustes"] if rec else ZERO)
        if xml is None:
            linha.situacao = "cancelada" if chave in canceladas else "so_receita"
        elif rec is None or rec["normal"] is None:
            linha.situacao = "so_xml"
        else:
            linha.situacao = "ok" if abs(linha.diferenca) <= TOLERANCIA else "divergente"
        resultado.linhas.append(linha)
    resultado.linhas.sort(key=lambda l: (SITUACOES.index(l.situacao), l.tipo, l.pa, l.chave))
    return resultado
