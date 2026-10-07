import base64
import zlib
from datetime import date

from sqlalchemy import select

from app import db, models, servicos
from app.ingest import flex_banco
from app.parser.nfe import ler_nfe
from tests.conftest import CNPJ_EMPRESA, FX


def _zlib(xml: bytes) -> str:
    return base64.b64encode(zlib.compress(xml)).decode()


def _cupom(numero: int) -> bytes:
    """A nota de venda da loja como NFC-e (modelo 65), com chave própria."""
    xml = (FX / "xml" / "saida_venda.xml").read_text(encoding="utf-8").replace("<mod>55</mod>", "<mod>65</mod>")
    chave = ler_nfe(xml.encode()).chave
    nova = chave[:20] + "65" + chave[22:25] + f"{numero:09d}" + chave[34:]   # modelo e número na chave
    return xml.replace(chave, nova).encode()


def _cancelamento(xml: bytes) -> bytes:
    chave = ler_nfe(xml).chave
    return (f"<retConsSitNFe><procEventoNFe><evento><infEvento><chNFe>{chave}</chNFe><tpEvento>110111</tpEvento>"
            f"</infEvento></evento></procEventoNFe></retConsSitNFe>").encode()


class FonteFake(flex_banco.FonteFlex):
    def __init__(self, tabelas, nfe=()):
        super().__init__()
        self.tabelas, self._nfe = tabelas, list(nfe)

    def existe_tabela_nfce(self, ano, mes):
        return (ano, mes) in self.tabelas

    def nfce(self, ano, mes, unidade, status):
        return iter([c for u, st, c in self.tabelas.get((ano, mes), []) if u == unidade and st == status])

    def nfe(self, unidade, inicio, fim):
        return iter(self._nfe)


def test_consolida_mes_sem_cancelados_nem_outra_loja():
    a, b, c, d = (_cupom(n) for n in (1, 2, 3, 4))
    tabelas = {(2026, 3): [("001", "V", _zlib(a)), ("001", "V", _zlib(b)), ("001", "V", _zlib(c)),
                           ("001", "C", _zlib(_cancelamento(a))), ("002", "V", _zlib(d))],
               (2026, 4): [("001", "C", _zlib(_cancelamento(b)))]}       # cancelado depois da meia-noite
    res = flex_banco.consolidar_mes(FonteFake(tabelas), CNPJ_EMPRESA, "001", 2026, 3, paralelo=1)
    assert (res.competencia, res.cupons, res.cancelados, res.ilegiveis) == ("2026-03", 1, 2, 0)
    um = ler_nfe(c)
    assert res.mes.valor == sum(i.valor_operacao for i in um.itens)


def test_consolidado_gravado_entra_na_analise_no_lugar_dos_cupons_avulsos(banco):
    cupons = [_cupom(n) for n in (1, 2, 3)]
    tabelas = {(2026, 3): [("001", "V", _zlib(x)) for x in cupons]}
    res = flex_banco.consolidar_mes(FonteFake(tabelas), CNPJ_EMPRESA, "001", 2026, 3, paralelo=1)
    with db.sessao() as s:
        e = servicos.criar_empresa(s, CNPJ_EMPRESA, "LOJA", "real", "SP", 3550308)
        servicos.importar_arquivos(s, e, [("avulso.xml", cupons[0])])       # o mesmo mês já importado cupom a cupom
        servicos.gravar_consolidado_nfce(s, e, res.competencia, res.mes, "teste")
        s.flush()
        docs = servicos.documentos(s, e, date(2026, 3, 1), date(2026, 3, 31))
        assert [(d.modelo, d.numero) for d in docs] == [("65", "3 cupons")]   # o avulso não conta duas vezes
        um = ler_nfe(cupons[0])
        assert sum(i.v_prod for i in docs[0].itens) == 3 * sum(i.v_prod for i in um.itens)
        assert servicos.documentos(s, e, date(2026, 4, 1), date(2026, 4, 30)) == []
        assert s.scalar(select(models.ConsolidadoNFCe.cupons)) == 3


def test_leitura_em_paralelo_da_o_mesmo_resultado():
    cupons = [_cupom(n) for n in range(1, 6)]
    tabelas = {(2026, 3): [("001", "V", _zlib(x)) for x in cupons]}
    sequencial = flex_banco.consolidar_mes(FonteFake(tabelas), CNPJ_EMPRESA, "001", 2026, 3, paralelo=1)
    original, flex_banco.BLOCO = flex_banco.BLOCO, 2
    try:
        paralelo = flex_banco.consolidar_mes(FonteFake(tabelas), CNPJ_EMPRESA, "001", 2026, 3, paralelo=2)
    finally:
        flex_banco.BLOCO = original
    assert paralelo.cupons == sequencial.cupons == 5
    assert paralelo.mes.valor == sequencial.mes.valor


def test_consolida_mes_a_partir_dos_arquivos_dos_dias(tmp_path, banco):
    import argparse
    import json

    from app import cli

    a, b, c = (_cupom(n) for n in (1, 2, 3))
    dia1 = {"rows": [{"xml_status": "V", "xml_conteudo": _zlib(a)}, {"xml_status": "V", "xml_conteudo": _zlib(b)},
                     {"xml_status": "C", "xml_conteudo": _zlib(_cancelamento(a))}]}
    dia2 = {"rows": [{"xml_status": "V", "xml_conteudo": _zlib(c)}]}
    seguinte = {"rows": [{"xml_status": "C", "xml_conteudo": _zlib(_cancelamento(b))}]}   # cancelado no mês seguinte
    arquivos = []
    for nome, dados in (("d1.json", dia1), ("d2.json", dia2), ("canc.json", seguinte)):
        (tmp_path / nome).write_text(json.dumps(dados), encoding="utf-8")
        arquivos.append(tmp_path / nome)
    with db.sessao() as s:
        servicos.criar_empresa(s, CNPJ_EMPRESA, "LOJA", "real", "SP", 3550308)
    for _ in range(2):                                                                     # reimportar substitui
        cli.cmd_importar_json(argparse.Namespace(cnpj=CNPJ_EMPRESA, arquivos=arquivos, nfce_mes=(2026, 3), paralelo=1))
    with db.sessao() as s:
        [reg] = s.scalars(select(models.ConsolidadoNFCe))
        assert (reg.competencia, reg.cupons, reg.cancelados) == ("2026-03", 1, 2)
