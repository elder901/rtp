"""Leitura dos XMLs exportados da tabela de notas do ERP Flex (CSV `xmlnfe`).

A coluna do XML aparece em dois formatos, conforme a época da nota:
- **texto** (notas antigas): o XML inteiro entre aspas, com cada aspas do XML escapada como `'"` (em vez de `""`) e
  vírgulas internas sem proteção — um leitor de CSV comum quebra o registro em colunas erradas;
- **compactado** (notas recentes): gzip codificado em base64 (começa com `H4sI`).

Por isso o arquivo é lido em fluxo, como bytes: cada documento é recortado de `<nfeProc` até `</nfeProc>` (desfazendo
o escape das aspas) ou decodificado do base64/gzip. Arquivos de gigabytes são processados sem carregar tudo na memória.

Também lê resultados de consulta em JSON (linhas com a coluna do XML) tirados direto do banco do Flex:
- ERP, tabela `xmlnfe` (NF-e de entrada e saída), coluna `xmls_texto`, em gzip+base64;
- PDV, tabelas mensais `xmlpdv_MMAA` (NFC-e), coluna `xml_conteudo`, em zlib+base64. Um cupom cancelado continua
  gravado como válido; o cancelamento vem numa linha à parte (consulta de situação com o evento 110111).
"""
from __future__ import annotations

import base64
import binascii
import gzip
import json
import re
import zlib
from collections.abc import Iterator
from pathlib import Path

INICIO = b"<nfeProc"
FIM = b"</nfeProc>"
GZIP_B64 = b"H4sI"                     # base64 do cabeçalho gzip 1f 8b 08
BLOCO = 32 * 1024 * 1024
_B64 = re.compile(rb"[A-Za-z0-9+/=]+")

_CHAVE = re.compile(rb'Id="NFe(\d{44})"')
_MOD = re.compile(rb"<mod>(\d{2})</mod>")
_DH = re.compile(rb"<dhEmi>(\d{4})-(\d{2})-(\d{2})")
_DEMI = re.compile(rb"<dEmi>(\d{4})-(\d{2})-(\d{2})")
_EMIT = re.compile(rb"<emit><CNPJ>(\d{14})</CNPJ>")
_CANCELAMENTO = re.compile(rb"<tpEvento>110111</tpEvento>")
_CHAVE_EVENTO = re.compile(rb"<chNFe>(\d{44})</chNFe>")
COLUNAS_XML = ("xmls_texto", "xml_conteudo")     # xmlnfe (ERP) e xmlpdv_MMAA (PDV)
_DEST = re.compile(rb"<dest><(?:CNPJ|CPF)>(\d{11,14})</(?:CNPJ|CPF)>")


class Contagem:
    def __init__(self):
        self.texto = self.compactados = self.falhas = 0


def _descompactar(token: bytes) -> bytes | None:
    try:
        return gzip.decompress(base64.b64decode(token))
    except (binascii.Error, OSError, EOFError, zlib.error, ValueError):
        return None


def iterar_documentos(caminho: Path, contagem: Contagem | None = None) -> Iterator[bytes]:
    """Devolve cada XML (bytes) encontrado no arquivo, nos dois formatos."""
    contagem = contagem or Contagem()
    resto = b""
    with open(caminho, "rb") as f:
        fim_do_arquivo = False
        while not fim_do_arquivo:
            bloco = f.read(BLOCO)
            fim_do_arquivo = not bloco
            dados = resto + bloco
            resto = b""
            pos = 0
            i_txt = i_gz = -2   # próxima ocorrência de cada formato; só procura de novo quando ficou para trás
            while True:
                if i_txt != -1 and i_txt < pos:
                    i_txt = dados.find(INICIO, pos)
                if i_gz != -1 and i_gz < pos:
                    i_gz = dados.find(GZIP_B64, pos)
                candidatos = [i for i in (i_txt, i_gz) if i >= 0]
                if not candidatos:
                    break
                i = min(candidatos)
                if i == i_txt:
                    j = dados.find(FIM, i)
                    if j < 0 and not fim_do_arquivo:
                        resto = dados[i:]          # documento continua no próximo bloco
                        break
                    if j < 0:
                        contagem.falhas += 1
                        break
                    contagem.texto += 1
                    yield dados[i:j + len(FIM)].replace(b"'\"", b'"')
                    pos = j + len(FIM)
                else:
                    m = _B64.match(dados, i)
                    if m.end() == len(dados) and not fim_do_arquivo:
                        resto = dados[i:]          # base64 continua no próximo bloco
                        break
                    xml = _descompactar(m.group())
                    if xml is None:
                        contagem.falhas += 1
                    else:
                        contagem.compactados += 1
                        yield xml
                    pos = m.end()


def resumo_rapido(xml: bytes) -> dict:
    """Campos de triagem sem montar a árvore XML (para varrer arquivos grandes)."""
    def achar(rx):
        m = rx.search(xml)
        return m.groups() if m else None
    dh = achar(_DH) or achar(_DEMI)
    return {
        "chave": (achar(_CHAVE) or [b""])[0].decode(),
        "modelo": (achar(_MOD) or [b""])[0].decode(),
        "ano_mes": f"{dh[0].decode()}-{dh[1].decode()}" if dh else "",
        "data": f"{dh[0].decode()}-{dh[1].decode()}-{dh[2].decode()}" if dh else "",
        "emitente": (achar(_EMIT) or [b""])[0].decode(),
        "destinatario": (achar(_DEST) or [b""])[0].decode(),
    }


def decodificar(conteudo: str | None) -> bytes | None:
    """XML gravado pelo Flex: texto, gzip+base64 (NF-e) ou zlib+base64 (NFC-e do PDV). None se ilegível."""
    texto = (conteudo or "").strip()
    if texto.startswith("<"):
        return texto.encode("utf-8")
    try:
        dados = base64.b64decode("".join(texto.split()), validate=True)
        if dados[:2] == b"\x1f\x8b":
            return gzip.decompress(dados)
        if dados[:1] == b"\x78":
            return zlib.decompress(dados)
    except (binascii.Error, OSError, EOFError, zlib.error, ValueError):
        pass
    return None


def chave_cancelada(xml: bytes) -> str | None:
    """Chave da nota cancelada, quando o XML é o evento de cancelamento (110111) ou a consulta de situação com ele."""
    if b"<infNFe" in xml or not _CANCELAMENTO.search(xml):
        return None
    m = _CHAVE_EVENTO.search(xml)
    return m.group(1).decode() if m else None


def iterar_resultado_json(caminho: Path, contagem: Contagem | None = None) -> Iterator[bytes]:
    """XMLs de um resultado de consulta em JSON ({"rows": [{...}]}) com a coluna xmls_texto ou xml_conteudo."""
    contagem = contagem or Contagem()
    with open(caminho, encoding="utf-8") as f:
        linhas = json.load(f).get("rows") or []
    for linha in linhas:
        coluna = next((c for c in COLUNAS_XML if c in linha), None)
        if coluna is None:
            raise ValueError(f"{caminho}: nenhuma coluna de XML ({', '.join(COLUNAS_XML)}) no resultado")
        xml = decodificar(linha[coluna])
        if xml is None:
            contagem.falhas += 1
            continue
        if (linha[coluna] or "").lstrip().startswith("<"):
            contagem.texto += 1
        else:
            contagem.compactados += 1
        yield xml
