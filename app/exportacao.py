"""Exporta os XMLs gravados no banco para uma pasta por tenant (pessoa jurídica = raiz do CNPJ):

    <destino>/<raiz>-<nome>/
        tenant.json                                   estabelecimentos, contagens e data da exportação
        <cnpj>-<nome do estabelecimento>/
            NFe/<AAAA-MM>/entrada/<chave>.xml
            NFe/<AAAA-MM>/saida/<chave>.xml
            NFCe/<AAAA-MM>/<chave>.xml                cupons gravados um a um
            NFCe-consolidado/<AAAA-MM>.json           meses importados já consolidados (o XML de cada cupom fica no PDV)

Nota cancelada sai com o sufixo "-cancelada". Arquivo que já existe com o mesmo tamanho não é regravado: rodar de novo
só acrescenta as notas novas.
"""
from __future__ import annotations

import gzip
import json
import re
import unicodedata
from dataclasses import asdict, dataclass
from datetime import date, datetime, time
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models, servicos
from app.ingest.consolidacao import MODELO_NFCE


@dataclass
class ContagemEstabelecimento:
    cnpj: str
    nome: str
    pasta: str
    nfe_entrada: int = 0
    nfe_saida: int = 0
    nfce: int = 0
    canceladas: int = 0
    nfce_consolidado_meses: int = 0
    gravados: int = 0
    ja_existiam: int = 0


def nome_de_pasta(texto: str) -> str:
    sem_acento = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", sem_acento.lower()).strip("-") or "sem-nome"


def _gravar(caminho: Path, conteudo: bytes, c: ContagemEstabelecimento):
    if caminho.exists() and caminho.stat().st_size == len(conteudo):
        c.ja_existiam += 1
        return
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_bytes(conteudo)
    c.gravados += 1


def _exportar_estabelecimento(s: Session, e: models.Empresa, pasta: Path, inicio: date | None,
                              fim: date | None) -> ContagemEstabelecimento:
    D, C = models.DocumentoFiscal, models.ConsolidadoNFCe
    c = ContagemEstabelecimento(e.cnpj, e.nome.strip(), pasta.name)
    q = select(D.id).where(D.empresa_id == e.id, D.xml_gz.is_not(None))
    if inicio:
        q = q.where(D.emissao >= datetime.combine(inicio, time.min))
    if fim:
        q = q.where(D.emissao <= datetime.combine(fim, time.max))
    ids = list(s.scalars(q.order_by(D.emissao, D.id)))
    for i in range(0, len(ids), 500):       # em blocos: o XML de todas as notas não cabe na memória de uma vez
        for chave, situacao, direcao, emissao, xml_gz in s.execute(
                select(D.chave, D.situacao, D.direcao, D.emissao, D.xml_gz).where(D.id.in_(ids[i:i + 500]))):
            mes = emissao.strftime("%Y-%m") if emissao else "sem-data"
            arquivo = f"{chave}-cancelada.xml" if situacao == "cancelado" else f"{chave}.xml"
            if chave[20:22] == MODELO_NFCE:
                destino = pasta / "NFCe" / mes / arquivo
                c.nfce += 1
            else:
                lado = direcao if direcao in ("entrada", "saida") else "outras"
                destino = pasta / "NFe" / mes / lado / arquivo
                if lado == "entrada":
                    c.nfe_entrada += 1
                elif lado == "saida":
                    c.nfe_saida += 1
            c.canceladas += situacao == "cancelado"
            _gravar(destino, gzip.decompress(xml_gz), c)
    competencias = select(C).where(C.empresa_id == e.id)
    if inicio:
        competencias = competencias.where(C.competencia >= inicio.strftime("%Y-%m"))
    if fim:
        competencias = competencias.where(C.competencia <= fim.strftime("%Y-%m"))
    for m in s.scalars(competencias.order_by(C.competencia)):
        dados = {"competencia": m.competencia, "origem": m.origem, "cupons": m.cupons, "cancelados": m.cancelados,
                 "ilegiveis": m.ilegiveis, "valor": str(m.valor), **json.loads(gzip.decompress(m.itens_gz))}
        _gravar(pasta / "NFCe-consolidado" / f"{m.competencia}.json",
                json.dumps(dados, ensure_ascii=False).encode("utf-8"), c)
        c.nfce_consolidado_meses += 1
    return c


def exportar_tenant(s: Session, raiz: str, destino: Path, inicio: date | None = None,
                    fim: date | None = None, progresso=print) -> Path:
    """Grava a pasta do tenant (todos os estabelecimentos cadastrados com a raiz) e devolve o caminho dela."""
    empresas = servicos.estabelecimentos_da_raiz(s, raiz)
    if not empresas:
        raise ValueError(f"Nenhuma empresa cadastrada com a raiz de CNPJ {raiz}.")
    matriz = empresas[0]
    pasta_tenant = Path(destino) / f"{raiz}-{nome_de_pasta(matriz.nome)}"
    contagens = []
    for e in empresas:
        c = _exportar_estabelecimento(s, e, pasta_tenant / f"{e.cnpj}-{nome_de_pasta(e.nome)}", inicio, fim)
        progresso(f"{c.nome} ({c.cnpj}): NF-e {c.nfe_entrada} entrada + {c.nfe_saida} saída | NFC-e {c.nfce} | "
                  f"consolidados {c.nfce_consolidado_meses} mês(es) | gravados {c.gravados}, já existiam {c.ja_existiam}")
        contagens.append(c)
    resumo = {"raiz": raiz, "nome": matriz.nome.strip(), "gerado_em": datetime.now().isoformat(timespec="seconds"),
              "periodo": {"inicio": inicio.isoformat() if inicio else None, "fim": fim.isoformat() if fim else None},
              "estabelecimentos": [asdict(c) for c in contagens]}
    pasta_tenant.mkdir(parents=True, exist_ok=True)
    (pasta_tenant / "tenant.json").write_text(json.dumps(resumo, ensure_ascii=False, indent=2), encoding="utf-8")
    return pasta_tenant
