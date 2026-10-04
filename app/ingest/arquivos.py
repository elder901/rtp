"""Leitura de XMLs a partir de pasta, ZIP ou bytes enviados (fallback quando não há DF-e)."""
from __future__ import annotations

import csv
import io
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from app.parser.nfe import Documento, XMLInvalido, ler_nfe


@dataclass
class ResultadoLeitura:
    documentos: list[Documento] = field(default_factory=list)
    duplicados: int = 0
    rejeitados: list[str] = field(default_factory=list)


def _adicionar(res: ResultadoLeitura, vistos: set[str], conteudo: bytes, nome: str):
    try:
        doc = ler_nfe(conteudo, arquivo=nome)
    except XMLInvalido as e:
        res.rejeitados.append(str(e))
        return
    if doc.chave in vistos:
        res.duplicados += 1
        return
    vistos.add(doc.chave)
    res.documentos.append(doc)


def ler_conteudos(arquivos: list[tuple[str, bytes]]) -> ResultadoLeitura:
    """Aceita XMLs soltos e ZIPs (inclusive aninhados em pastas dentro do ZIP)."""
    res, vistos = ResultadoLeitura(), set()
    for nome, conteudo in arquivos:
        if nome.lower().endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(conteudo)) as z:
                for info in z.infolist():
                    if not info.is_dir() and info.filename.lower().endswith(".xml"):
                        _adicionar(res, vistos, z.read(info), f"{nome}/{info.filename}")
        elif nome.lower().endswith(".xml"):
            _adicionar(res, vistos, conteudo, nome)
    return res


def ler_caminho(caminho: Path) -> ResultadoLeitura:
    if caminho.is_dir():
        arquivos = [p for p in caminho.rglob("*") if p.suffix.lower() in (".xml", ".zip")]
    else:
        arquivos = [caminho]
    return ler_conteudos([(str(p.relative_to(caminho) if caminho.is_dir() else p.name), p.read_bytes())
                          for p in sorted(arquivos)])


def ler_classificacoes(conteudo: str) -> dict[str, tuple[str, str]]:
    """CSV (; ou ,) com colunas codigo_ou_ncm, cst, cclasstrib. Código do produto tem prioridade sobre NCM."""
    dialeto = csv.Sniffer().sniff(conteudo.splitlines()[0], delimiters=";,")
    leitor = csv.DictReader(io.StringIO(conteudo), dialect=dialeto)
    saida = {}
    for linha in leitor:
        chave = (linha.get("codigo_ou_ncm") or "").strip()
        cst, cct = (linha.get("cst") or "").strip(), (linha.get("cclasstrib") or "").strip()
        if chave and cst and cct:
            saida[chave] = (cst.zfill(3), cct.zfill(6))
    return saida
