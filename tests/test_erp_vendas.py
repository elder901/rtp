import json
from decimal import Decimal as D

from app import db, servicos
from app.ingest import erp_vendas
from app.ingest.consolidacao import MesNFCe, de_json
from app.parser.nfe import Item, Participante

LOJA = Participante("11222333000181", "LOJA LTDA", "MG", "3103504", crt="3")


def _linha(mes, cod, cst, valor, desconto="0", qtde="1", icms="0", pis="0", cofins="0", aliqpis="0", ean="", emb="UN",
           ncm="02013000", descricao="PRODUTO"):
    return {"mes": mes, "cod": cod, "cst": cst, "descricao": descricao, "ean": ean, "emb": emb, "ncm": ncm,
            "valor": valor, "desconto": desconto, "qtde": qtde, "icms": icms, "fcp": "0", "pis": pis, "cofins": cofins,
            "aliqpis": aliqpis}


def test_consolida_como_a_nfce_com_referencia_das_outras_lojas():
    ref = Item(n_item=1, codigo="159212", descricao="Pao De Queijo Kg", ncm="19012090", cfop="5102", unidade="KG",
               quantidade=D("10"), v_prod=D("100"), pis_cst="01", icms_cst="00")
    linhas = [_linha("2026-09", "159212", "000", "32748.07", desconto="927.67", qtde="936.717", icms="2292.86",
                     pis="502.29", cofins="2314.79", aliqpis="1.65"),
              _linha("2026-09", "135364", "060", "120636.68", desconto="289.75", qtde="3531.924", ean="0000000008811",
                     emb="KG", descricao="CARNE ACEM KG"),
              _linha("2026-09", "999", "060", "10.00", ean="7891000315507")]          # EAN válido, sem referência
    mes, sem_ref = erp_vendas.consolidar(linhas, LOJA, {"159212": ref}, cupons=53774)
    itens = {i.codigo: i for i in mes.itens.values()}
    queijo, carne, outro = itens["159212"], itens["135364"], itens["999"]
    assert (queijo.cfop, queijo.icms_cst, queijo.pis_cst, queijo.descricao) == ("5102", "00", "01", "Pao De Queijo Kg")
    assert queijo.valor_operacao == D("32748.07") and queijo.v_desc == D("927.67") and queijo.v_icms == D("2292.86")
    assert (carne.cfop, carne.icms_cst, carne.pis_cst, carne.gtin, carne.unidade) == ("5405", "60", "06", "", "KG")
    assert (outro.gtin, outro.unidade_gtin, outro.quantidade_gtin) == ("7891000315507", "UN", D("1.0000"))
    assert sem_ref == D("120646.68") and mes.cupons == 53774 and mes.valor == D("153394.75")


def test_importa_e_substitui_o_mes(banco, tmp_path):
    with db.sessao() as s:
        outra = servicos.criar_empresa(s, "11222333000262", "LOJA 2", "real", "MG", 3103504)
        e = servicos.criar_empresa(s, "11222333000181", "LOJA 1", "real", "MG", 3103504)
        m = MesNFCe(Participante("11222333000262", "LOJA LTDA", "MG", "3103504", crt="3"), 10)
        m.adicionar_item(Item(n_item=1, codigo="5", descricao="Ref", ncm="22021000", cfop="5405", unidade="UN",
                              quantidade=D("1"), v_prod=D("5"), pis_cst="04", icms_cst="60"))
        servicos.gravar_consolidado_nfce(s, outra, "2026-09", m, "json")
    arquivo = tmp_path / "vendas.json"
    arquivo.write_text(json.dumps({"rows": [_linha("2026-09", "5", "060", "50.00", qtde="10"),
                                            _linha("2026-08", "6", "000", "20.00", icms="3.60")]}), encoding="utf-8")
    with db.sessao() as s:
        rel = servicos.importar_vendas_erp(s, s.get(servicos.models.Empresa, e.id), [arquivo],
                                           {"2026-09": (40, 2)}, progresso=lambda _: None)
        assert [r[:7] for r in rel] == ["2026-08", "2026-09"]
        servicos.importar_vendas_erp(s, s.get(servicos.models.Empresa, e.id), [arquivo], {"2026-09": (40, 2)},
                                     progresso=lambda _: None)                  # de novo: substitui, não duplica
        regs = {c.competencia: c for c in s.scalars(servicos.select(servicos.models.ConsolidadoNFCe)
                                                    .where(servicos.models.ConsolidadoNFCe.empresa_id == e.id))}
        assert set(regs) == {"2026-08", "2026-09"}
        set09 = regs["2026-09"]
        assert (set09.cupons, set09.cancelados, set09.valor) == (40, 2, D("50.00"))
        item = next(iter(de_json(set09.itens_gz).itens.values()))
        assert (item.pis_cst, item.descricao) == ("04", "Ref")                   # tratamento da outra loja
        assert de_json(set09.itens_gz).emitente.nome == "LOJA LTDA"


def test_resultado_cortado_perde_o_ultimo_mes_e_total_divergente_nao_grava(banco, tmp_path):
    arquivo = tmp_path / "cortado.json"
    arquivo.write_text(json.dumps({"truncated": True, "rows": [_linha("2026-01", "1", "000", "10.00"),
                                                               _linha("2026-02", "1", "000", "3.00")]}), encoding="utf-8")
    avisos = []
    assert set(erp_vendas.ler_resultados([arquivo], avisos)) == {"2026-01"} and "2026-02 ignorado" in avisos[0]
    with db.sessao() as s:
        e = servicos.criar_empresa(s, "11222333000181", "LOJA 1", "real", "MG", 3103504)
        rel = servicos.importar_vendas_erp(s, e, [arquivo], {"2026-01": (5, 0, "99.00")}, progresso=lambda _: None)
        assert any("NÃO gravado" in r for r in rel)
        assert not list(s.scalars(servicos.select(servicos.models.ConsolidadoNFCe)))


def test_tratamento_dos_cupons_da_propria_loja_vale_mais_que_o_das_outras(banco, tmp_path):
    def mes(cnpj, pis_cst, descricao):
        m = MesNFCe(Participante(cnpj, "LOJA LTDA", "MG", "3103504", crt="3"), 1)
        m.adicionar_item(Item(n_item=1, codigo="5", descricao=descricao, ncm="22021000", cfop="5405", unidade="UN",
                              quantidade=D("1"), v_prod=D("500"), pis_cst=pis_cst, icms_cst="60"))
        return m

    with db.sessao() as s:
        outra = servicos.criar_empresa(s, "11222333000262", "LOJA 2", "real", "MG", 3103504)
        e = servicos.criar_empresa(s, "11222333000181", "LOJA 1", "real", "MG", 3103504)
        servicos.gravar_consolidado_nfce(s, outra, "2026-09", mes(outra.cnpj, "04", "Da outra"), "json")
        servicos.gravar_consolidado_nfce(s, e, "2026-09", mes(e.cnpj, "06", "Da loja"), "pasta:1 XMLs")
        servicos.gravar_consolidado_nfce(s, e, "2026-07", mes(e.cnpj, "01", "Do ERP"), "erp:movprodd")  # não serve
    arquivo = tmp_path / "vendas.json"
    arquivo.write_text(json.dumps({"rows": [_linha("2026-08", "5", "060", "50.00")]}), encoding="utf-8")
    with db.sessao() as s:
        e = s.get(servicos.models.Empresa, e.id)
        servicos.importar_vendas_erp(s, e, [arquivo], progresso=lambda _: None)
        reg = s.scalar(servicos.select(servicos.models.ConsolidadoNFCe).where(
            servicos.models.ConsolidadoNFCe.empresa_id == e.id, servicos.models.ConsolidadoNFCe.competencia == "2026-08"))
        item = next(iter(de_json(reg.itens_gz).itens.values()))
        assert (item.pis_cst, item.descricao) == ("06", "Da loja")
