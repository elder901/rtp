"""Linha de comando.

  python -m app.cli empresa criar --cnpj 11222333000181 --nome "Loja" --regime real --uf SP --municipio 3550308
  python -m app.cli empresa listar
  python -m app.cli importar --cnpj 11222333000181 --xml C:/xmls/cliente
  python -m app.cli importar-flex --cnpj 11222333000181 --csv C:/xmlnfe.csv --inicio 2022-12-01 --fim 2022-12-31
  python -m app.cli importar-banco --cnpj 11222333000181 --inicio 2026-01 --fim 2026-09 [--unidade 001]
      (direto do banco do Flex, configurado no .env: NFC-e do PDV consolidadas por mês + NF-e do ERP)
  python -m app.cli flex-testar      (confere a conexão com o banco do Flex)
  python -m app.cli importar-json --cnpj 11222333000181 resultado1.json [resultado2.json ...]
      (resultados de consulta às tabelas xmlnfe do ERP e xmlpdv_MMAA do PDV do Flex)
  python -m app.cli certificado --cnpj 11222333000181 --pfx C:/certs/loja.pfx      (senha pedida no terminal)
  python -m app.cli dfe [--cnpj 11222333000181]                                     (ideal para o Agendador do Windows)
  python -m app.cli relatorio --cnpj 11222333000181 --inicio 2026-01-01 --fim 2026-06-30 --saida relatorios/loja.xlsx
  python -m app.cli apuracao credencial --cnpj ... --client-id ... [--ambiente prr|pro]   (secret pedido no terminal)
  python -m app.cli apuracao solicitar --cnpj ... --tipo debitos|creditos
  python -m app.cli apuracao verificar [--cnpj ...]
  python -m app.cli apuracao importar --cnpj ... --arquivo retorno.json
  python -m app.cli apuracao conciliar --cnpj ... [--pa-inicio 2026-01] [--pa-fim 2026-03] [--saida conciliacao.xlsx]
  python -m app.cli calculadora status | atualizar [--forcar] | iniciar | parar | fonte | executar
      (executar: roda a calculadora local no terminal atual, em primeiro plano)
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
    print(f"\nCalculadora RTC: {analise.versao_calculadora}")
    print(f"Relatório: {exportar(analise, destino).resolve()}")


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


def _mes(texto: str) -> tuple[int, int]:
    ano, _, mes = texto.partition("-")
    if not (ano.isdigit() and mes.isdigit() and 1 <= int(mes) <= 12):
        raise argparse.ArgumentTypeError(f"mês inválido: {texto} (use AAAA-MM)")
    return int(ano), int(mes)


def cmd_importar_banco(a):
    """Importa do banco do Flex: NFC-e (PDV) consolidadas por mês e NF-e (ERP) do período."""
    import calendar
    import time

    from app.config import settings
    from app.ingest.flex import decodificar
    from app.ingest.flex_banco import ErroFonte, FonteFlex, consolidar_mes, proximo_mes, tabela_nfce

    cnpj = servicos.so_digitos(a.cnpj)
    meses, m = [], a.inicio
    while m <= a.fim:
        meses.append(m)
        m = proximo_mes(*m)
    try:
        with FonteFlex(settings.flex_pdv_url, settings.flex_erp_url) as fonte:
            unidade = a.unidade or fonte.unidade_da_empresa(cnpj)
            if not unidade:
                sys.exit(f"Unidade do CNPJ {cnpj} não encontrada na xmlnfe; informe --unidade.")
            print(f"CNPJ {cnpj} = unidade {unidade} no Flex | meses: {len(meses)}")
            for ano, mes in meses:
                t0 = time.perf_counter()
                if not a.sem_nfe:
                    fim_mes = date(ano, mes, calendar.monthrange(ano, mes)[1])
                    with db.sessao() as s:
                        e = _empresa(s, cnpj)
                        r = servicos.importar_arquivos(s, e, ((f"xmlnfe-{i}.xml", x) for i, x in enumerate(
                            filter(None, map(decodificar, fonte.nfe(unidade, date(ano, mes, 1), fim_mes))))))
                    print(f"{ano}-{mes:02d} NF-e: novas {r.novos} | já existentes {r.ja_existentes} | "
                          f"sem participação {r.sem_participacao} | rejeitadas {len(r.rejeitados)}")
                if not a.sem_nfce:
                    res = consolidar_mes(fonte, cnpj, unidade, ano, mes, a.paralelo)
                    if res.mes:
                        with db.sessao() as s:
                            servicos.gravar_consolidado_nfce(s, _empresa(s, cnpj), res.competencia, res.mes,
                                                             f"wrpdv:{tabela_nfce(ano, mes)}/{unidade}",
                                                             res.cancelados, res.ilegiveis)
                    print(f"{res.competencia} NFC-e: {res.cupons} cupons consolidados em "
                          f"{len(res.mes.itens) if res.mes else 0} itens | cancelados {res.cancelados} | ilegíveis "
                          f"{res.ilegiveis} | de outro CNPJ {res.outro_emitente} | repetidos {res.duplicados} | "
                          f"valor {res.mes.valor if res.mes else 0:,.2f}")
                print(f"   ({time.perf_counter() - t0:.0f}s)")
    except ErroFonte as erro:
        sys.exit(str(erro))


def cmd_flex_testar(a):
    """Confere a conexão com os bancos do Flex configurados no .env e o que há para importar."""
    from datetime import date as _date

    from app.config import settings
    from app.ingest.flex_banco import ErroFonte, FonteFlex

    hoje = _date.today()
    with FonteFlex(settings.flex_pdv_url, settings.flex_erp_url) as fonte:
        for qual, url in (("pdv", settings.flex_pdv_url), ("erp", settings.flex_erp_url)):
            if not url:
                print(f"{qual.upper()}: não configurado (RTP_FLEX_{qual.upper()}_URL no .env)")
                continue
            try:
                with fonte._conexao(qual).cursor() as cur:
                    cur.execute("SELECT current_database(), current_user, split_part(version(), ',', 1)")
                    banco, usuario, versao = cur.fetchone()
                print(f"{qual.upper()}: conectado ao banco {banco} como {usuario} ({versao})")
            except ErroFonte as erro:
                print(f"{qual.upper()}: {erro}")
                continue
            if qual == "pdv":
                print(f"   tabela de NFC-e do mês atual existe: {fonte.existe_tabela_nfce(hoje.year, hoje.month)}")
            else:
                with db.sessao() as s:
                    for e in s.scalars(select(models.Empresa)):
                        print(f"   {e.nome} ({e.cnpj}): unidade {fonte.unidade_da_empresa(e.cnpj) or '-'} no Flex")


def cmd_importar_json(a):
    """Importa NF-e e NFC-e de resultados de consulta em JSON ao banco do Flex; aplica os cancelamentos."""
    import time

    from app.ingest.flex import Contagem, chave_cancelada, iterar_resultado_json

    cnpj = servicos.so_digitos(a.cnpj)
    if getattr(a, "nfce_mes", None):
        return _importar_json_nfce_mes(cnpj, a)
    contagem, cancelados, t0 = Contagem(), set(), time.perf_counter()
    total = servicos.ResultadoImportacao()
    with db.sessao() as s:
        e = _empresa(s, cnpj)
        for arquivo in a.arquivos:
            documentos = []
            for i, xml in enumerate(iterar_resultado_json(arquivo, contagem)):
                chave = chave_cancelada(xml)
                if chave:
                    cancelados.add(chave)
                else:
                    documentos.append((f"{arquivo.stem}-{i}.xml", xml))
            r = servicos.importar_arquivos(s, e, documentos)
            for campo in ("novos", "atualizados", "ja_existentes", "sem_participacao"):
                setattr(total, campo, getattr(total, campo) + getattr(r, campo))
            total.rejeitados += r.rejeitados
            s.flush()
            print(f"{arquivo.name}: {len(documentos)} XMLs | novos {r.novos} | já existentes {r.ja_existentes}")
        marcadas = servicos.cancelar_documentos(s, e, cancelados, "flex")
    print(f"XMLs: {contagem.compactados} compactados, {contagem.texto} em texto, {contagem.falhas} ilegíveis | "
          f"cancelamentos: {len(cancelados)} ({marcadas} nota(s) já importada(s) marcada(s) como cancelada)")
    print(f"Novos: {total.novos} | completaram resumo: {total.atualizados} | já existentes: {total.ja_existentes} | "
          f"sem participação da empresa: {total.sem_participacao} | rejeitados: {len(total.rejeitados)} | "
          f"tempo: {time.perf_counter() - t0:.1f}s")
    for motivo in total.rejeitados[:5]:
        print("  rejeitado:", motivo)


def _importar_json_nfce_mes(cnpj: str, a):
    """Consolida as NFC-e de um mês a partir dos arquivos JSON dos dias e grava (substitui) o consolidado do mês."""
    import time

    from app.ingest.flex_banco import FonteArquivos, consolidar_mes

    ano, mes = a.nfce_mes
    t0 = time.perf_counter()
    res = consolidar_mes(FonteArquivos(a.arquivos, ano, mes), cnpj, "", ano, mes, a.paralelo)
    if not res.mes:
        sys.exit(f"{res.competencia}: nenhum cupom válido do CNPJ {cnpj} nos arquivos.")
    with db.sessao() as s:
        servicos.gravar_consolidado_nfce(s, _empresa(s, cnpj), res.competencia, res.mes,
                                         f"json:{len(a.arquivos)} arquivos", res.cancelados, res.ilegiveis)
    print(f"{res.competencia} NFC-e: {res.cupons} cupons consolidados em {len(res.mes.itens)} itens | cancelados "
          f"{res.cancelados} | ilegíveis {res.ilegiveis} | de outro CNPJ {res.outro_emitente} | repetidos "
          f"{res.duplicados} | valor {res.mes.valor:,.2f} | {time.perf_counter() - t0:.0f}s")


def cmd_importar_flex(a):
    """Importa as NF-e da tabela de XMLs do ERP Flex exportada em CSV, filtrando por CNPJ e período de emissão."""
    import time

    from app.ingest.flex import Contagem, iterar_documentos, resumo_rapido

    cnpj = servicos.so_digitos(a.cnpj)
    inicio, fim = (a.inicio.isoformat() if a.inicio else ""), (a.fim.isoformat() if a.fim else "9999")
    contagem, lidos, selecionados = Contagem(), 0, 0
    t0 = time.perf_counter()

    def selecionar():
        nonlocal lidos, selecionados
        for xml in iterar_documentos(a.csv, contagem):
            lidos += 1
            r = resumo_rapido(xml)
            if cnpj in (r["emitente"], r["destinatario"]) and inicio <= r["data"] <= fim:
                selecionados += 1
                yield f"{r['chave'] or lidos}.xml", xml

    with db.sessao() as s:
        e = _empresa(s, cnpj)
        r = servicos.importar_arquivos(s, e, selecionar())
    print(f"Arquivo: {lidos} documentos ({contagem.texto} em texto, {contagem.compactados} compactados, "
          f"{contagem.falhas} ilegíveis) | do CNPJ e período: {selecionados}")
    print(f"Novos: {r.novos} | completaram resumo: {r.atualizados} | já existentes: {r.ja_existentes} | "
          f"sem participação da empresa: {r.sem_participacao} | rejeitados: {len(r.rejeitados)} | "
          f"tempo: {time.perf_counter() - t0:.1f}s")
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


def cmd_calculadora(a):
    from app.calculadora import offline

    try:
        if a.acao == "status":
            info = offline.status()
            print(f"Instalada: {info.get('instalada') or 'não'} | rodando: {'sim' if info['rodando'] else 'não'} "
                  f"({info['url_local']})")
            if info.get("versao"):
                v = info["versao"]
                print(f"Versão local: app {v.get('versaoApp')} · base {v.get('versaoDb')} ({v.get('dataVersaoDb')})")
            st = info.get("versao_status") or {}
            if st:
                ok = st.get("aplicacaoAtualizada") and st.get("dbAtualizada")
                print(f"Segundo a própria calculadora: {'atualizada' if ok else 'DESATUALIZADA'} "
                      f"(remota: app {st.get('versaoAplicacaoRemota')} · base {st.get('versaoDbRemota')})")
            if "pacote_remoto" in info:
                r = info["pacote_remoto"]
                print(f"Pacote oficial: {r['ultima_modificacao']} ({r['tamanho'] / 1e6:.0f} MB) — "
                      f"{'ATUALIZAÇÃO DISPONÍVEL' if info['atualizacao_disponivel'] else 'igual ao instalado'}")
            for chave in ("erro_versao", "erro_remoto"):
                if info.get(chave):
                    print("!", info[chave])
        elif a.acao == "atualizar":
            offline.atualizar(forcar=a.forcar)
        elif a.acao == "iniciar":
            print(f"Calculadora local em {offline.url_local()} (PID {offline.iniciar_atual()}).")
        elif a.acao == "parar":
            offline.parar_atual()
            print("Calculadora local parada.")
        elif a.acao == "executar":
            print(f"Calculadora local em {offline.url_local()} — Ctrl+C encerra.")
            offline.executar_primeiro_plano()
        elif a.acao == "fonte":
            zips = sorted((offline.PASTA / "pacotes").glob("calculadora-*.zip"))
            if not zips:
                sys.exit("Nenhum pacote baixado. Rode: python -m app.cli calculadora atualizar")
            estado = offline.estado()
            versao = {"versaoApp": estado.get("versao_app"), "versaoDb": estado.get("versao_base"),
                      "dataVersaoDb": estado.get("data_base")}
            pacote = offline.Pacote("", estado.get("etag", ""), estado.get("ultima_modificacao", ""), 0)
            print(f"Código-fonte extraído em {offline.extrair_fonte(zips[-1], versao=versao, pacote=pacote).resolve()}")
    except offline.ErroOffline as erro:
        sys.exit(str(erro))


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

    pf = sub.add_parser("importar-flex", help="importar NF-e do CSV da tabela de XMLs do ERP Flex")
    pf.add_argument("--cnpj", required=True)
    pf.add_argument("--csv", required=True, type=Path)
    pf.add_argument("--inicio", type=date.fromisoformat, help="data de emissão inicial (AAAA-MM-DD)")
    pf.add_argument("--fim", type=date.fromisoformat, help="data de emissão final (AAAA-MM-DD)")
    pf.set_defaults(func=cmd_importar_flex)

    pb = sub.add_parser("importar-banco", help="importar NFC-e (consolidadas) e NF-e direto do banco do Flex")
    pb.add_argument("--cnpj", required=True)
    pb.add_argument("--inicio", required=True, type=_mes, help="mês inicial (AAAA-MM)")
    pb.add_argument("--fim", required=True, type=_mes, help="mês final (AAAA-MM)")
    pb.add_argument("--unidade", help="código da loja no Flex (ex.: 001); sem ele, descobre pela xmlnfe")
    pb.add_argument("--sem-nfe", action="store_true", help="só as NFC-e")
    pb.add_argument("--sem-nfce", action="store_true", help="só as NF-e")
    pb.add_argument("--paralelo", type=int, help="processos para ler os cupons (padrão: núcleos - 1)")
    pb.set_defaults(func=cmd_importar_banco)

    pt = sub.add_parser("flex-testar", help="conferir a conexão com o banco do Flex (.env)")
    pt.set_defaults(func=cmd_flex_testar)

    pj = sub.add_parser("importar-json", help="importar NF-e/NFC-e de resultados de consulta (JSON) ao banco do Flex")
    pj.add_argument("--cnpj", required=True)
    pj.add_argument("arquivos", nargs="+", type=Path)
    pj.add_argument("--nfce-mes", type=_mes, help="consolidar as NFC-e do mês AAAA-MM (arquivos de xmlpdv_MMAA) em vez "
                                                  "de gravar cupom a cupom")
    pj.add_argument("--paralelo", type=int, help="processos para ler os cupons (padrão: núcleos - 1)")
    pj.set_defaults(func=cmd_importar_json)

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

    pk = sub.add_parser("calculadora", help="calculadora RTC local: status, atualizar, iniciar, parar")
    pk.add_argument("acao", choices=["status", "atualizar", "iniciar", "parar", "fonte", "executar"])
    pk.add_argument("--forcar", action="store_true", help="atualizar mesmo sem versão nova ou com divergência")
    pk.set_defaults(func=cmd_calculadora)

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
    from app.calculadora.client import CalculadoraIndisponivel
    try:
        a.func(a)
    except CalculadoraIndisponivel as erro:
        sys.exit(f"{erro}\nAnálise interrompida: sem a calculadora oficial não há cálculo. "
                 f"Inicie-a com: python -m app.cli calculadora executar")


if __name__ == "__main__":
    main()
