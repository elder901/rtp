"""Linha de comando.

  python -m app.cli empresa criar --cnpj 11222333000181 --nome "Loja" --regime real --uf SP --municipio 3550308
  python -m app.cli empresa listar
  python -m app.cli importar --cnpj 11222333000181 --xml C:/xmls/cliente
  python -m app.cli certificado --cnpj 11222333000181 --pfx C:/certs/loja.pfx      (senha pedida no terminal)
  python -m app.cli dfe [--cnpj 11222333000181]                                     (ideal para o Agendador do Windows)
  python -m app.cli relatorio --cnpj 11222333000181 --inicio 2026-01-01 --fim 2026-06-30 --saida relatorios/loja.xlsx
  python -m app.cli apuracao credencial --cnpj ... --client-id ... [--ambiente prr|pro]   (secret pedido no terminal)
  python -m app.cli apuracao solicitar --cnpj ... --tipo debitos|creditos
  python -m app.cli apuracao verificar [--cnpj ...]
  python -m app.cli apuracao importar --cnpj ... --arquivo retorno.json
  python -m app.cli apuracao conciliar --cnpj ... [--pa-inicio 2026-01] [--pa-fim 2026-03] [--saida conciliacao.xlsx]
  python -m app.cli analisar ...   (análise avulsa de uma pasta, sem gravar no banco)
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from app import db, models, servicos
from app.calculadora.client import CalculadoraRTC
from app.engine.cenarios import Analise, Empresa, analisar
from app.engine.premissas import TRANSICAO, Premissas
from app.ingest.arquivos import arquivos_do_caminho, ler_caminho, ler_classificacoes
from app.relatorios import agregacao
from app.relatorios.excel import exportar


def _anos(texto: str | None) -> list[int]:
    if not texto:
        return list(TRANSICAO)
    anos = [int(x) for x in texto.split(",")]
    invalidos = [x for x in anos if x not in TRANSICAO]
    if invalidos:
        sys.exit(f"Anos fora da transição simulável (2027-2033): {invalidos}")
    return anos


def _imprimir_resumo(analise: Analise, destino: Path):
    print("\nResumo (saldo a recolher = débitos nas saídas - créditos nas entradas):")
    for l in agregacao.resumo(analise):
        var = f"  ({l['diferenca_vs_atual']:+,.2f} vs atual)" if l["diferenca_vs_atual"] is not None else ""
        print(f"  {l['cenario']:>6}: débitos {l['debitos']:>14,.2f}  créditos {l['creditos']:>14,.2f}  "
              f"saldo {l['saldo_a_recolher']:>14,.2f}{var}")
    for t in analise.alertas:
        print("  ! " + t)
    print(f"\nRelatório: {exportar(analise, destino).resolve()}")


def _empresa(s, cnpj: str) -> models.Empresa:
    e = s.scalar(select(models.Empresa).where(models.Empresa.cnpj == servicos.so_digitos(cnpj)))
    if not e:
        sys.exit(f"Empresa {cnpj} não cadastrada. Use: python -m app.cli empresa criar ...")
    return e


def cmd_empresa(a):
    with db.sessao() as s:
        if a.acao == "criar":
            try:
                e = servicos.criar_empresa(s, a.cnpj, a.nome, a.regime, a.uf, a.municipio, a.industria)
            except ValueError as erro:
                sys.exit(str(erro))
            print(f"Empresa {e.cnpj} criada (id {e.id}).")
        else:
            for e in s.scalars(select(models.Empresa).order_by(models.Empresa.nome)):
                cert = f"certificado até {e.certificado.validade:%d/%m/%Y}" if e.certificado else "sem certificado"
                print(f"{e.cnpj}  {e.nome:<40} {e.regime:<10} {e.uf}  {cert}")


def cmd_importar(a):
    with db.sessao() as s:
        e = _empresa(s, a.cnpj)
        r = servicos.importar_arquivos(s, e, arquivos_do_caminho(a.xml))
    print(f"Novos: {r.novos} | completaram resumo: {r.atualizados} | já existentes: {r.ja_existentes} | "
          f"sem participação da empresa: {r.sem_participacao} | rejeitados: {len(r.rejeitados)}")
    for m in r.rejeitados[:10]:
        print("  -", m)


def cmd_certificado(a):
    senha = os.getenv(a.senha_env) if a.senha_env else getpass.getpass("Senha do certificado: ")
    with db.sessao() as s:
        e = _empresa(s, a.cnpj)
        try:
            cert = servicos.salvar_certificado(s, e, a.pfx.read_bytes(), senha or "")
        except ValueError as erro:
            sys.exit(str(erro))
    print(f"Certificado de {cert.titular} gravado (válido até {cert.validade:%d/%m/%Y}).")


def cmd_dfe(a):
    from app.dfe.sincronizar import sincronizar, sincronizar_pendentes

    if a.cnpj:
        with db.sessao() as s:
            resultados = [(a.cnpj, sincronizar(s, _empresa(s, a.cnpj)))]
    else:
        resultados = sincronizar_pendentes(db.sessao)
    for cnpj, r in resultados:
        if r.executada:
            print(f"{cnpj}: {r.completos} NF-e completas, {r.resumos} resumos, {r.cancelados} canceladas, "
                  f"{r.manifestados} ciências, {r.xml_obtidos} XML obtidos após ciência")
        for m in r.mensagens:
            print(f"{cnpj}: {m}")


def cmd_relatorio(a):
    with db.sessao() as s:
        e = _empresa(s, a.cnpj)
        analise = servicos.montar_analise(s, e, CalculadoraRTC(a.calculadora_url), a.inicio, a.fim, _anos(a.anos))
    _imprimir_resumo(analise, a.saida)


def cmd_apuracao(a):
    import json

    from app.apuracao import conciliacao
    from app.apuracao import servico as apuracao
    from app.apuracao.cliente import ErroApuracao

    with db.sessao() as s:
        if a.acao == "verificar" and not a.cnpj:
            empresas = list(s.scalars(select(models.Empresa).join(models.SolicitacaoApuracao).where(
                models.SolicitacaoApuracao.estado.in_(apuracao.ABERTAS)).distinct()))
        else:
            if not a.cnpj:
                sys.exit("Informe --cnpj")
            empresas = [_empresa(s, a.cnpj)]
        try:
            for e in empresas:
                if a.acao == "credencial":
                    segredo = os.getenv(a.secret_env) if a.secret_env else getpass.getpass("Client Secret: ")
                    apuracao.salvar_credencial(s, e, a.client_id or "", segredo or "", a.ambiente)
                    print("Credencial gravada (cifrada).")
                elif a.acao == "solicitar":
                    sol = apuracao.solicitar(s, e, a.tipo)
                    print(f"{e.cnpj} {a.tipo}: {sol.estado} {sol.mensagem}")
                elif a.acao == "verificar":
                    for m in apuracao.verificar_pendentes(s, e):
                        print(f"{e.cnpj} {m}")
                elif a.acao == "importar":
                    n = apuracao.processar_arquivo(s, e, json.loads(a.arquivo.read_text(encoding="utf-8-sig")))
                    print(f"{n} registro(s) importado(s).")
                elif a.acao == "conciliar":
                    conc = conciliacao.conciliar(s, e, a.pa_inicio, a.pa_fim)
                    for r in conc.resumo():
                        print(f"{r['tipo']:>9}: {r['notas']} notas | XML {r['cbs_xml']:,.2f} | Receita {r['cbs_receita']:,.2f} | "
                              f"divergentes {r['divergente']} | só Receita {r['so_receita']} | só XML {r['so_xml']} | "
                              f"canceladas {r['cancelada']} | ok {r['ok']}")
                    if a.saida:
                        _exportar_conciliacao(conc, a.saida)
                        print(f"Planilha: {a.saida.resolve()}")
        except (ErroApuracao, ValueError) as erro:
            sys.exit(str(erro))


def _exportar_conciliacao(conc, destino: Path):
    from openpyxl import Workbook

    from app.relatorios.excel import _aba

    wb = Workbook()
    wb.remove(wb.active)
    _aba(wb, "Resumo", conc.resumo())
    _aba(wb, "Notas", [{"situacao": l.situacao, "lado": l.tipo, "pa": l.pa, "chave": l.chave,
                        "participante": l.participante, "cbs_xml": l.cbs_xml, "cbs_receita": l.cbs_receita,
                        "diferenca": l.diferenca, "ajustes": l.ajustes} for l in conc.linhas])
    destino.parent.mkdir(parents=True, exist_ok=True)
    wb.save(destino)


def cmd_analisar(a):
    premissas = Premissas(industria=a.industria, anos=_anos(a.anos))
    for campo, valor in [("cbs_referencia", a.cbs), ("ibs_uf_referencia", a.ibs_uf),
                         ("ibs_mun_referencia", a.ibs_mun), ("credito_fornecedor_simples_pct", a.credito_simples),
                         ("aliquota_das_pct", a.das)]:
        if valor is not None:
            setattr(premissas, campo, valor)
    leitura = ler_caminho(a.xml)
    print(f"XMLs lidos: {len(leitura.documentos)} | duplicados: {leitura.duplicados} | rejeitados: {len(leitura.rejeitados)}")
    for r in leitura.rejeitados[:10]:
        print("  -", r)
    overrides = ler_classificacoes(a.classificacao.read_text(encoding="utf-8-sig")) if a.classificacao else {}
    empresa = Empresa(servicos.so_digitos(a.cnpj), a.nome or a.cnpj, a.regime, a.uf.upper(), a.municipio)
    _imprimir_resumo(analisar(empresa, leitura.documentos, premissas, CalculadoraRTC(a.calculadora_url), overrides),
                     a.saida)


def main(argv: list[str] | None = None):
    p = argparse.ArgumentParser(description="RTP — impacto da reforma tributária a partir de NF-e")
    sub = p.add_subparsers(dest="comando", required=True)

    pe = sub.add_parser("empresa", help="cadastrar ou listar empresas")
    pe.add_argument("acao", choices=["criar", "listar"])
    pe.add_argument("--cnpj")
    pe.add_argument("--nome", default="")
    pe.add_argument("--regime", choices=["real", "presumido", "simples"])
    pe.add_argument("--uf")
    pe.add_argument("--municipio", type=int, help="código IBGE")
    pe.add_argument("--industria", action="store_true")
    pe.set_defaults(func=cmd_empresa)

    pi = sub.add_parser("importar", help="importar pasta/ZIP de XMLs para uma empresa")
    pi.add_argument("--cnpj", required=True)
    pi.add_argument("--xml", required=True, type=Path)
    pi.set_defaults(func=cmd_importar)

    pc = sub.add_parser("certificado", help="gravar o certificado A1 (cifrado) de uma empresa")
    pc.add_argument("--cnpj", required=True)
    pc.add_argument("--pfx", required=True, type=Path)
    pc.add_argument("--senha-env", help="nome da variável de ambiente com a senha (senão, é perguntada)")
    pc.set_defaults(func=cmd_certificado)

    pd = sub.add_parser("dfe", help="baixar NF-e da SEFAZ (todas as empresas com consulta liberada, ou uma)")
    pd.add_argument("--cnpj")
    pd.set_defaults(func=cmd_dfe)

    pr = sub.add_parser("relatorio", help="Excel a partir das notas gravadas de uma empresa")
    pr.add_argument("--cnpj", required=True)
    pr.add_argument("--inicio", type=date.fromisoformat)
    pr.add_argument("--fim", type=date.fromisoformat)
    pr.add_argument("--anos")
    pr.add_argument("--saida", type=Path, default=Path("relatorios/analise.xlsx"))
    pr.add_argument("--calculadora-url")
    pr.set_defaults(func=cmd_relatorio)

    pp = sub.add_parser("apuracao", help="API de apuração da CBS da Receita e conciliação")
    pp.add_argument("acao", choices=["credencial", "solicitar", "verificar", "importar", "conciliar"])
    pp.add_argument("--cnpj")
    pp.add_argument("--client-id")
    pp.add_argument("--secret-env", help="variável de ambiente com o Client Secret (senão, é perguntado)")
    pp.add_argument("--ambiente", choices=["prr", "pro"], default="prr")
    pp.add_argument("--tipo", choices=["debitos", "creditos"])
    pp.add_argument("--arquivo", type=Path)
    pp.add_argument("--pa-inicio", help="AAAA-MM")
    pp.add_argument("--pa-fim", help="AAAA-MM")
    pp.add_argument("--saida", type=Path)
    pp.set_defaults(func=cmd_apuracao)

    pa = sub.add_parser("analisar", help="análise avulsa de uma pasta/ZIP, sem gravar no banco")
    pa.add_argument("--cnpj", required=True)
    pa.add_argument("--nome", default="")
    pa.add_argument("--regime", required=True, choices=["real", "presumido", "simples"])
    pa.add_argument("--uf", required=True)
    pa.add_argument("--municipio", required=True, type=int, help="código IBGE")
    pa.add_argument("--xml", required=True, type=Path)
    pa.add_argument("--classificacao", type=Path, help="CSV codigo_ou_ncm;cst;cclasstrib")
    pa.add_argument("--saida", type=Path, default=Path("relatorios/analise.xlsx"))
    pa.add_argument("--anos")
    pa.add_argument("--industria", action="store_true")
    pa.add_argument("--cbs", type=Decimal)
    pa.add_argument("--ibs-uf", type=Decimal)
    pa.add_argument("--ibs-mun", type=Decimal)
    pa.add_argument("--credito-simples", type=Decimal)
    pa.add_argument("--das", type=Decimal)
    pa.add_argument("--calculadora-url")
    pa.set_defaults(func=cmd_analisar)

    a = p.parse_args(argv)
    if a.comando == "apuracao" and ((a.acao == "solicitar" and not a.tipo) or (a.acao == "importar" and not a.arquivo)
                                    or (a.acao == "credencial" and not a.client_id)):
        p.error("apuracao: solicitar exige --tipo; importar exige --arquivo; credencial exige --client-id")
    if a.comando == "empresa" and a.acao == "criar" and not all([a.cnpj, a.regime, a.uf, a.municipio]):
        p.error("empresa criar exige --cnpj, --regime, --uf e --municipio")
    db.criar_tabelas()
    a.func(a)


if __name__ == "__main__":
    main()
