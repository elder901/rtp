"""Leitura direta do banco do Flex, somente leitura: NFC-e do PDV (banco wrpdv, tabelas mensais xmlpdv_MMAA) e NF-e
do ERP (tabela xmlnfe). Configure no .env (o usuário do banco só precisa de SELECT nessas tabelas):

    RTP_FLEX_PDV_URL=postgresql://usuario:senha@servidor:5432/wrpdv
    RTP_FLEX_ERP_URL=postgresql://usuario:senha@servidor:5432/<banco do ERP>

As NFC-e não são gravadas uma a uma: cada mês vira um consolidado por produto e tratamento fiscal
(ingest/consolidacao.py), que é o que a análise usa. Os cupons cancelados ficam de fora — no PDV o cupom continua
gravado como válido (status V) e o cancelamento vem numa linha à parte (status C, evento 110111).
"""
from __future__ import annotations

import json
import os
from collections import deque
from collections.abc import Iterable, Iterator
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import date

from app.ingest.consolidacao import MesNFCe
from app.ingest.flex import chave_cancelada, decodificar
from app.parser.nfe import ler_nfe

BLOCO = 2000


class ErroFonte(RuntimeError):
    pass


def tabela_nfce(ano: int, mes: int) -> str:
    return f"xmlpdv_{mes:02d}{ano % 100:02d}"


def proximo_mes(ano: int, mes: int) -> tuple[int, int]:
    return (ano + 1, 1) if mes == 12 else (ano, mes + 1)


class FonteFlex:
    """Conexões de leitura com os bancos do Flex (abertas sob demanda)."""

    def __init__(self, url_pdv: str = "", url_erp: str = ""):
        self.url_pdv, self.url_erp = url_pdv, url_erp
        self._conexoes = {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        for c in self._conexoes.values():
            c.close()

    def _conexao(self, qual: str):
        if qual not in self._conexoes:
            import psycopg

            url = self.url_pdv if qual == "pdv" else self.url_erp
            if not url:
                raise ErroFonte(f"Conexão com o banco do {'PDV' if qual == 'pdv' else 'ERP'} não configurada: defina "
                                f"RTP_FLEX_{qual.upper()}_URL no arquivo .env.")
            try:
                conn = psycopg.connect(url, connect_timeout=15)
            except psycopg.Error as erro:
                raise ErroFonte(f"Não foi possível conectar ao banco do {qual.upper()}: {erro}") from erro
            conn.read_only = True
            self._conexoes[qual] = conn
        return self._conexoes[qual]

    def _linhas(self, qual: str, sql, params) -> Iterator[str]:
        with self._conexao(qual).cursor(name=f"rtp_{qual}") as cur:   # cursor no servidor: lê em fluxo
            cur.itersize = BLOCO
            cur.execute(sql, params)
            for (conteudo,) in cur:
                yield conteudo

    def existe_tabela_nfce(self, ano: int, mes: int) -> bool:
        with self._conexao("pdv").cursor() as cur:
            cur.execute("SELECT to_regclass(%s) IS NOT NULL", (tabela_nfce(ano, mes),))
            return cur.fetchone()[0]

    def nfce(self, ano: int, mes: int, unidade: str, status: str) -> Iterator[str]:
        from psycopg import sql

        consulta = sql.SQL("SELECT xml_conteudo FROM {} WHERE xml_unidade = %s AND xml_status = %s").format(
            sql.Identifier(tabela_nfce(ano, mes)))
        return self._linhas("pdv", consulta, (unidade, status))

    def nfe(self, unidade: str, inicio: date, fim: date) -> Iterator[str]:
        return self._linhas("erp", "SELECT xmls_texto FROM xmlnfe WHERE xmls_unid_codigo = %s "
                                   "AND xmls_dtemissao BETWEEN %s AND %s", (unidade, inicio, fim))

    def unidade_da_empresa(self, cnpj: str) -> str | None:
        """Código da unidade (loja) no Flex, pelas notas da tabela xmlnfe."""
        with self._conexao("erp").cursor() as cur:
            cur.execute("SELECT xmls_unid_codigo, count(*) FROM xmlnfe WHERE (xmls_entsai = 'S' AND xmls_cnpjemit = %s)"
                        " OR (xmls_entsai = 'E' AND xmls_cnpjdest = %s) GROUP BY 1 ORDER BY 2 DESC LIMIT 1",
                        (cnpj, cnpj))
            linha = cur.fetchone()
            return linha[0] if linha else None


class FonteArquivos(FonteFlex):
    """No lugar da conexão: resultados de consulta em JSON ({"rows": [{"xml_status", "xml_conteudo"}]}) já filtrados
    pela loja, com os cupons de um mês da tabela xmlpdv_MMAA (ex.: trazidos dia a dia pelo gateway de dados). Para pegar
    cancelamentos feitos depois da virada do mês, inclua o arquivo com as linhas C do mês seguinte."""

    def __init__(self, arquivos, ano: int, mes: int):
        super().__init__()
        self.arquivos, self.competencia = list(arquivos), (ano, mes)

    def existe_tabela_nfce(self, ano: int, mes: int) -> bool:
        return (ano, mes) == self.competencia

    def nfce(self, ano: int, mes: int, unidade: str, status: str) -> Iterator[str]:
        consultas = set()
        for arquivo in self.arquivos:
            with open(arquivo, encoding="utf-8") as f:
                dados = json.load(f)
            consulta = " ".join((dados.get("query") or str(arquivo)).split())
            if consulta in consultas:          # a mesma consulta trazida duas vezes não conta em dobro
                continue
            consultas.add(consulta)
            for linha in dados.get("rows") or []:
                if linha.get("xml_status") == status and linha.get("xml_conteudo"):
                    yield linha["xml_conteudo"]


@dataclass
class ResultadoMes:
    competencia: str
    cupons: int = 0
    cancelados: int = 0
    ilegiveis: int = 0
    outro_emitente: int = 0
    duplicados: int = 0
    mes: MesNFCe | None = field(default=None, repr=False)


def _blocos(linhas: Iterable[str], tamanho: int) -> Iterator[list[str]]:
    bloco = []
    for linha in linhas:
        bloco.append(linha)
        if len(bloco) >= tamanho:
            yield bloco
            bloco = []
    if bloco:
        yield bloco


def _processar_bloco(conteudos: list[str], cancelados: frozenset[str], cnpj: str):
    """Decodifica, lê e consolida um bloco de cupons (roda em outro processo)."""
    mes, chaves, cont = None, [], {"ilegiveis": 0, "cancelados": 0, "outro_emitente": 0}
    for conteudo in conteudos:
        xml = decodificar(conteudo)
        try:
            doc = ler_nfe(xml) if xml else None
        except ValueError:
            doc = None
        if doc is None:
            cont["ilegiveis"] += 1
            continue
        if doc.chave in cancelados:
            cont["cancelados"] += 1
            continue
        if doc.emitente.cnpj != cnpj:
            cont["outro_emitente"] += 1
            continue
        mes = mes or MesNFCe(doc.emitente)
        mes.cupons += 1
        chaves.append(doc.chave)
        for it in doc.itens:
            mes.adicionar_item(it)
    return mes, chaves, cont


def consolidar_mes(fonte: FonteFlex, cnpj: str, unidade: str, ano: int, mes: int,
                   paralelo: int | None = None) -> ResultadoMes:
    """Consolida as NFC-e válidas da unidade no mês (tabela xmlpdv_MMAA), sem os cupons cancelados."""
    res = ResultadoMes(f"{ano}-{mes:02d}")
    if not fonte.existe_tabela_nfce(ano, mes):
        return res
    cancelados = set()
    for a, m in ((ano, mes), proximo_mes(ano, mes)):     # cupom do último dia cancelado depois da meia-noite
        if a == ano and m == mes or fonte.existe_tabela_nfce(a, m):
            for conteudo in fonte.nfce(a, m, unidade, "C"):
                xml = decodificar(conteudo)
                chave = chave_cancelada(xml) if xml else None
                if chave:
                    cancelados.add(chave)
    cancelados = frozenset(cancelados)
    vistos: set[str] = set()

    def juntar(parcial):
        m, chaves, cont = parcial
        res.ilegiveis += cont["ilegiveis"]
        res.cancelados += cont["cancelados"]
        res.outro_emitente += cont["outro_emitente"]
        repetidos = vistos.intersection(chaves)
        res.duplicados += len(repetidos)
        vistos.update(chaves)
        if m:
            if res.mes is None:
                res.mes = m
            else:
                res.mes.cupons += m.cupons
                for it in m.itens.values():
                    res.mes.adicionar_item(it)

    blocos = _blocos(fonte.nfce(ano, mes, unidade, "V"), BLOCO)
    paralelo = paralelo if paralelo is not None else max(1, (os.cpu_count() or 2) - 1)
    if paralelo <= 1:
        for bloco in blocos:
            juntar(_processar_bloco(bloco, cancelados, cnpj))
    else:
        with ProcessPoolExecutor(max_workers=paralelo) as pool:
            pendentes = deque()
            for bloco in blocos:
                pendentes.append(pool.submit(_processar_bloco, bloco, cancelados, cnpj))
                if len(pendentes) >= paralelo * 2:
                    juntar(pendentes.popleft().result())
            while pendentes:
                juntar(pendentes.popleft().result())
    res.cupons = res.mes.cupons if res.mes else 0
    return res
