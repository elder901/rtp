"""Leitura de XMLs a partir de pasta, ZIP ou bytes enviados (fallback quando não há DF-e)."""
from __future__ import annotations

import csv
import io
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from app.parser.nfe import Documento, XMLInvalido, ler_nfe


@dataclass
class ResultadoLeitura:
    documentos: list[Documento] = field(default_factory=list)
    duplicados: int = 0
    rejeitados: list[str] = field(default_factory=list)


def iterar_xmls(arquivos: list[tuple[str, bytes]]) -> Iterator[tuple[str, bytes]]:
    """Abre XMLs soltos e ZIPs (inclusive com pastas dentro) e devolve (nome, conteúdo) de cada XML."""
    for nome, conteudo in arquivos:
        if nome.lower().endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(conteudo)) as z:
                for info in z.infolist():
                    if not info.is_dir() and info.filename.lower().endswith(".xml"):
                        yield f"{nome}/{info.filename}", z.read(info)
        elif nome.lower().endswith(".xml"):
            yield nome, conteudo


def ler_conteudos(arquivos: list[tuple[str, bytes]]) -> ResultadoLeitura:
    res, vistos = ResultadoLeitura(), set()
    for nome, conteudo in iterar_xmls(arquivos):
        try:
            doc = ler_nfe(conteudo, arquivo=nome)
        except XMLInvalido as e:
            res.rejeitados.append(str(e))
            continue
        if doc.chave in vistos:
            res.duplicados += 1
            continue
        vistos.add(doc.chave)
        res.documentos.append(doc)
    return res


def arquivos_do_caminho(caminho: Path) -> list[tuple[str, bytes]]:
    if caminho.is_dir():
        return [(str(p.relative_to(caminho)), p.read_bytes())
                for p in sorted(caminho.rglob("*")) if p.suffix.lower() in (".xml", ".zip")]
    return [(caminho.name, caminho.read_bytes())]


def ler_caminho(caminho: Path) -> ResultadoLeitura:
    return ler_conteudos(arquivos_do_caminho(caminho))


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
