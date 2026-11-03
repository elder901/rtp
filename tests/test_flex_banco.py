import base64
import zlib
from datetime import date

from sqlalchemy import select

from app import db, models, servicos
from app.ingest import flex_banco
from app.ingest.consolidacao import de_json
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


def _cupom_de_outubro(numero: int, dia: int) -> bytes:
    """Cupom emitido no dia de out/2026, o mês da chave do arquivo de exemplo, com código de produto numérico como no
    Flex."""
    return _cupom(numero).replace(b"<dhEmi>2026-03-20", f"<dhEmi>2026-10-{dia:02d}".encode()).replace(
        b"<cProd>V-NB01", b"<cProd>1001")


def _gravar_backup(pasta, dia: int, xml: bytes, cancelamento: bool = False):
    """Como o PDV guarda: AAAAMMDD/Autorização/<chave>-nfce-dist.xml e .../Cancelamento/<chave>-nfce-canc.xml."""
    chave = ler_nfe(xml if not cancelamento else _cupom(1)).chave if not cancelamento else None
    sub, sufixo = ("Cancelamento", "canc") if cancelamento else ("Autorização", "dist")
    destino = pasta / f"202610{dia:02d}" / sub
    destino.mkdir(parents=True, exist_ok=True)
    nome = chave or xml.split(b"<chNFe>")[1][:44].decode()
    (destino / f"NFe{nome}-nfce-{sufixo}.xml").write_bytes(xml)


def test_pasta_do_backup_do_pdv(tmp_path):
    a, b, c = _cupom_de_outubro(1, 1), _cupom_de_outubro(2, 1), _cupom_de_outubro(3, 2)
    for dia, xml in ((1, a), (1, b), (2, c)):
        _gravar_backup(tmp_path, dia, xml)
    _gravar_backup(tmp_path / "copia", 1, a)                          # o mesmo cupom em outra subpasta conta uma vez
    _gravar_backup(tmp_path, 2, _cancelamento(b), cancelamento=True)
    res = flex_banco.consolidar_mes(flex_banco.FontePasta(tmp_path), CNPJ_EMPRESA, "", 2026, 10, paralelo=1)
    assert (res.cupons, res.cancelados, res.ilegiveis) == (2, 1, 0)
    assert res.mes.valor == 2 * sum(i.valor_operacao for i in ler_nfe(a).itens)
    so_dia_1 = flex_banco.consolidar_mes(flex_banco.FontePasta(tmp_path, ate=date(2026, 10, 1)), CNPJ_EMPRESA, "",
                                         2026, 10, paralelo=1)
    assert so_dia_1.cupons == 1
    assert flex_banco.consolidar_mes(flex_banco.FontePasta(tmp_path), CNPJ_EMPRESA, "", 2026, 9, paralelo=1).mes is None


def test_mes_da_pasta_completado_pelo_erp_e_conferido_com_o_total(tmp_path, banco):
    import json
    from decimal import Decimal as D

    import pytest

    from tests.test_erp_vendas import _linha

    a, b = _cupom_de_outubro(1, 1), _cupom_de_outubro(2, 2)
    _gravar_backup(tmp_path / "xml", 1, a)
    _gravar_backup(tmp_path / "xml", 2, b)                            # dia 2 veio pela metade: fica de fora
    um = ler_nfe(a)
    codigo = um.itens[0].codigo
    erp = tmp_path / "erp.json"                                       # dia 2 pela venda do ERP
    erp.write_text(json.dumps({"rows": [_linha("2026-10", codigo, "060", "7.00", descricao="NOME DO CADASTRO")]}),
                   encoding="utf-8")
    valor_xml = sum(i.valor_operacao for i in um.itens)
    with db.sessao() as s:
        e = servicos.criar_empresa(s, CNPJ_EMPRESA, "LOJA", "real", "SP", 3550308)
        with pytest.raises(ValueError, match="NÃO gravado"):
            servicos.importar_nfce_pasta(s, e, tmp_path / "xml", 2026, 10, ate=date(2026, 10, 1), erp=[erp],
                                         cupons=(2, 0, "1.00"), paralelo=1, progresso=lambda _: None)
        assert not list(s.scalars(select(models.ConsolidadoNFCe)))
        servicos.importar_nfce_pasta(s, e, tmp_path / "xml", 2026, 10, ate=date(2026, 10, 1), erp=[erp],
                                     cupons=(2, 0, str(valor_xml + D("7.00"))), paralelo=1, progresso=lambda _: None)
        [reg] = s.scalars(select(models.ConsolidadoNFCe))
        assert (reg.competencia, reg.cupons, reg.origem) == ("2026-10", 2, "pasta:1 XMLs+erp")
        do_erp = [i for i in de_json(reg.itens_gz).itens.values() if i.valor_operacao == D("7.00")]
        assert do_erp[0].descricao == um.itens[0].descricao             # o tratamento do próprio cupom da loja
