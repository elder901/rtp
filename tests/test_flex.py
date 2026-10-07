import base64
import gzip
import zlib

import pytest
from sqlalchemy import select

from app.ingest import flex
from app.parser.nfe import ler_nfe
from tests.conftest import FX


def _como_texto_flex(xml: bytes) -> bytes:
    """Como a exportação do Flex grava as notas antigas: entre aspas, com cada aspas do XML virando '"."""
    return b'"' + xml.replace(b'"', b"'\"") + b'"'


def _como_compactado_flex(xml: bytes) -> bytes:
    return base64.b64encode(gzip.compress(xml))


@pytest.fixture
def notas():
    a = (FX / "xml" / "entrada_fornecedor_normal.xml").read_bytes()
    b = (FX / "xml" / "saida_venda.xml").read_bytes()
    corpo = lambda x: x[x.index(b"<nfeProc"):].strip()          # sem a declaração <?xml ...?>
    return corpo(a), corpo(b)


def _csv(tmp_path, notas):
    a, b = notas
    linhas = [b"xmls_idnfe,xmls_status,xmls_cnpjemit,xmls_texto,xmls_modelo",
              b"1,N,44555666000177," + _como_texto_flex(a) + b",55,0.60N,VENDA, COM VIRGULA",
              b"2,N,11222333000181," + _como_compactado_flex(b) + b",55",
              b"3,N,,,,"]
    caminho = tmp_path / "xmlnfe.csv"
    caminho.write_bytes(b"\n".join(linhas))
    return caminho


def test_le_os_dois_formatos(tmp_path, notas):
    cont = flex.Contagem()
    docs = list(flex.iterar_documentos(_csv(tmp_path, notas), cont))
    assert (cont.texto, cont.compactados, cont.falhas) == (1, 1, 0)
    assert [ler_nfe(d).numero for d in docs] == ["1234", "10"]
    assert docs[0] == notas[0]                                        # aspas restauradas, XML idêntico


def test_nota_cortada_entre_blocos(tmp_path, notas, monkeypatch):
    monkeypatch.setattr(flex, "BLOCO", 700)                           # força notas atravessando blocos
    docs = list(flex.iterar_documentos(_csv(tmp_path, notas)))
    assert [ler_nfe(d).numero for d in docs] == ["1234", "10"]


def test_resumo_rapido(notas):
    r = flex.resumo_rapido(notas[0])
    assert r["chave"].startswith("3526104455566600017755") and r["modelo"] == "55"
    assert (r["data"], r["emitente"], r["destinatario"]) == ("2026-03-10", "44555666000177", "11222333000181")


def _evento_cancelamento(chave: str) -> bytes:
    """Consulta de situação de uma NFC-e cancelada, como o PDV do Flex grava na linha de status C."""
    return (f'<?xml version="1.0" encoding="UTF-8"?><retConsSitNFe versao="4.00"><tpAmb>1</tpAmb><cStat>101</cStat>'
            f'<procEventoNFe versao="1.00"><evento><infEvento Id="ID110111{chave}01"><chNFe>{chave}</chNFe>'
            f'<tpEvento>110111</tpEvento></infEvento></evento></procEventoNFe></retConsSitNFe>').encode()


def test_decodifica_os_formatos_do_banco(notas):
    a, _ = notas
    assert flex.decodificar(a.decode()) == a                                         # texto
    assert flex.decodificar(base64.b64encode(gzip.compress(a)).decode()) == a        # xmlnfe (ERP)
    zlib_b64 = base64.b64encode(zlib.compress(a)).decode()
    assert flex.decodificar(zlib_b64[:60] + "\r\n" + zlib_b64[60:]) == a             # xmlpdv (PDV), com quebras
    assert flex.decodificar("isto não é xml") is None and flex.decodificar(None) is None


def test_importa_json_do_banco_e_aplica_cancelamento(tmp_path, notas, banco):
    import argparse
    import json

    from app import cli, db, models, servicos

    entrada, venda = notas
    chave_venda = ler_nfe(venda).chave
    linhas_pdv = [{"xml_status": "V", "xml_conteudo": base64.b64encode(zlib.compress(venda)).decode()},
                  {"xml_status": "C", "xml_conteudo": base64.b64encode(zlib.compress(_evento_cancelamento(chave_venda))).decode()}]
    linhas_erp = [{"xmls_texto": base64.b64encode(gzip.compress(entrada)).decode()}]
    pdv, erp = tmp_path / "pdv.json", tmp_path / "erp.json"
    pdv.write_text(json.dumps({"rows": linhas_pdv}), encoding="utf-8")
    erp.write_text(json.dumps({"rows": linhas_erp}), encoding="utf-8")
    with db.sessao() as s:
        servicos.criar_empresa(s, "11222333000181", "LOJA", "real", "SP", 3550308)

    cli.cmd_importar_json(argparse.Namespace(cnpj="11222333000181", arquivos=[erp, pdv]))
    cli.cmd_importar_json(argparse.Namespace(cnpj="11222333000181", arquivos=[pdv]))   # reimportar não ressuscita
    with db.sessao() as s:
        situacoes = {d.direcao or "?": d.situacao for d in s.scalars(select(models.DocumentoFiscal))}
        e = s.scalar(select(models.Empresa))
        assert situacoes == {"entrada": "completo", "saida": "cancelado"}
        assert [d.chave for d in servicos.documentos(s, e)] == [ler_nfe(entrada).chave]  # cancelada fora da análise
