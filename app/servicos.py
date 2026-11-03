"""Operações de negócio sobre o banco: empresas, certificados, importação de XML e montagem da análise."""
from __future__ import annotations

import gzip
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

from sqlalchemy import and_, delete, func, insert, not_, or_, select
from sqlalchemy.orm import Session

from app import cobertura, models
from app.calculadora.client import CalculadoraRTC
from app.dfe.certificado import CertificadoA1, CertificadoInvalido
from app.engine.cenarios import Analise, Empresa, analisar
from app.engine.premissas import Premissas
from app.ingest import sped
from app.ingest.arquivos import iterar_xmls
from app.ingest.consolidacao import MODELO_NFCE, ConsolidadorNFCe, MesNFCe, de_json, documento_do_mes, para_json
from app.parser.nfe import Documento, Participante, ler_nfe
from app.seguranca import cifrar, decifrar

CAMPOS_PREMISSAS = ["cbs_referencia", "ibs_uf_referencia", "ibs_mun_referencia", "credito_fornecedor_simples_pct",
                    "credito_fornecedor_mei_pct", "credito_presumido_nao_contribuinte_pct", "repasse_is_revendedor_pct", "aliquota_das_pct", "das_pis_cofins_pct", "das_icms_iss_pct"]


def so_digitos(s: str) -> str:
    return "".join(c for c in s if c.isdigit())


# --- Empresas ----------------------------------------------------------------------------------------------------

def raiz_cnpj(cnpj: str) -> str:
    """Raiz do CNPJ (8 primeiros dígitos): identifica a pessoa jurídica; o resto é o estabelecimento (filial)."""
    return so_digitos(cnpj)[:8]


def estabelecimentos_da_raiz(s: Session, raiz: str) -> list[models.Empresa]:
    """Estabelecimentos (CNPJs) da mesma pessoa jurídica, matriz primeiro (ordem do número da filial)."""
    todas = s.scalars(select(models.Empresa)).all()
    return sorted((e for e in todas if e.cnpj[:8] == raiz), key=lambda e: e.cnpj[8:12])


def criar_empresa(s: Session, cnpj: str, nome: str, regime: str, uf: str, cod_municipio: int,
                  industria: bool = False) -> models.Empresa:
    cnpj = so_digitos(cnpj)
    if len(cnpj) != 14:
        raise ValueError("CNPJ deve ter 14 dígitos.")
    if regime not in ("real", "presumido", "simples"):
        raise ValueError("Regime inválido.")
    if s.scalar(select(models.Empresa).where(models.Empresa.cnpj == cnpj)):
        raise ValueError(f"Empresa {cnpj} já cadastrada.")
    e = models.Empresa(cnpj=cnpj, nome=nome or cnpj, regime=regime, uf=uf.upper(), cod_municipio=cod_municipio,
                       industria=industria, premissas={})
    s.add(e)
    s.flush()
    return e


def premissas_da_empresa(e: models.Empresa, anos: list[int] | None = None) -> Premissas:
    p = Premissas(industria=e.industria)
    for campo in CAMPOS_PREMISSAS:
        if campo in (e.premissas or {}):
            setattr(p, campo, Decimal(str(e.premissas[campo])))
    if anos:
        p.anos = anos
    return p


def atualizar_premissas(e: models.Empresa, valores: dict[str, str]):
    novas = dict(e.premissas or {})
    for campo in CAMPOS_PREMISSAS:
        if valores.get(campo) not in (None, ""):
            novas[campo] = str(Decimal(valores[campo].replace(",", ".")))
    e.premissas = novas


def empresa_do_motor(e: models.Empresa) -> Empresa:
    return Empresa(e.cnpj, e.nome, e.regime, e.uf, e.cod_municipio)


# --- Certificado -------------------------------------------------------------------------------------------------

def salvar_certificado(s: Session, e: models.Empresa, pfx: bytes, senha: str) -> CertificadoA1:
    cert = CertificadoA1.carregar(pfx, senha)
    if cert.vencido:
        raise CertificadoInvalido(f"Certificado vencido em {cert.validade:%d/%m/%Y}.")
    if cert.cnpj and cert.cnpj[:8] != e.cnpj[:8]:
        raise CertificadoInvalido(f"O certificado é do CNPJ {cert.cnpj}, de outra raiz que não a da empresa {e.cnpj}.")
    if e.certificado:
        s.delete(e.certificado)
        s.flush()
    e.certificado = models.Certificado(pfx_cifrado=cifrar(pfx), senha_cifrada=cifrar(senha.encode()),
                                       titular=cert.titular[:300], cnpj_certificado=cert.cnpj,
                                       validade=cert.validade.replace(tzinfo=None))
    return cert


def carregar_certificado(e: models.Empresa) -> CertificadoA1:
    if not e.certificado:
        raise CertificadoInvalido("Empresa sem certificado A1 cadastrado.")
    return CertificadoA1.carregar(decifrar(e.certificado.pfx_cifrado), decifrar(e.certificado.senha_cifrada).decode())


# --- Documentos --------------------------------------------------------------------------------------------------

@dataclass
class ResultadoImportacao:
    novos: int = 0
    atualizados: int = 0
    ja_existentes: int = 0
    sem_participacao: int = 0
    rejeitados: list[str] = field(default_factory=list)


def gravar_documento(s: Session, e: models.Empresa, doc: Documento, xml: bytes, origem: str,
                     nsu: str | None = None) -> str:
    """Grava/atualiza uma NF-e completa. Retorna 'novo', 'atualizado', 'existente' ou 'sem_participacao'."""
    direcao = doc.direcao_para(e.cnpj)
    if direcao is None:
        return "sem_participacao"
    participante = doc.contraparte_para(e.cnpj)
    existente = s.scalar(select(models.DocumentoFiscal).where(
        models.DocumentoFiscal.empresa_id == e.id, models.DocumentoFiscal.chave == doc.chave))
    if existente and existente.situacao != "resumo":
        return "existente"
    reg = existente or models.DocumentoFiscal(empresa_id=e.id, chave=doc.chave)
    reg.origem = origem if not existente else existente.origem
    reg.situacao = "completo"
    reg.direcao = direcao
    reg.nsu = nsu or reg.nsu
    reg.emissao = doc.emissao.replace(tzinfo=None) if doc.emissao != datetime.min else None
    reg.participante_cnpj = participante.cnpj if participante else ""
    reg.participante_nome = (participante.nome if participante else "")[:200]
    reg.valor = sum((i.valor_operacao + i.tributos_por_fora for i in doc.itens), Decimal(0))
    reg.xml_gz = gzip.compress(xml)
    if not existente:
        s.add(reg)
    return "atualizado" if existente else "novo"


def cancelar_documentos(s: Session, e: models.Empresa, chaves: set[str], origem: str) -> int:
    """Marca as notas como canceladas (saem da análise). Chave ainda não importada fica registrada como cancelada,
    para que a nota não entre depois."""
    marcadas, lista = 0, sorted(chaves)
    for i in range(0, len(lista), 500):
        parte = lista[i:i + 500]
        existentes = {r.chave: r for r in s.scalars(select(models.DocumentoFiscal).where(
            models.DocumentoFiscal.empresa_id == e.id, models.DocumentoFiscal.chave.in_(parte)))}
        for chave in parte:
            reg = existentes.get(chave)
            if reg is None:
                s.add(models.DocumentoFiscal(empresa_id=e.id, chave=chave, origem=origem, situacao="cancelado"))
            elif reg.situacao != "cancelado":
                reg.situacao = "cancelado"
                marcadas += 1
    return marcadas


def importar_arquivos(s: Session, e: models.Empresa, arquivos: list[tuple[str, bytes]]) -> ResultadoImportacao:
    res = ResultadoImportacao()
    for nome, xml in iterar_xmls(arquivos):
        try:
            doc = ler_nfe(xml, arquivo=nome)
        except ValueError as erro:
            res.rejeitados.append(str(erro))
            continue
        situacao = gravar_documento(s, e, doc, xml, "upload")
        if situacao == "novo":
            res.novos += 1
        elif situacao == "atualizado":
            res.atualizados += 1
        elif situacao == "existente":
            res.ja_existentes += 1
        else:
            res.sem_participacao += 1
        s.flush()
    return res


# --- SPED (notas escrituradas e créditos tomados) ----------------------------------------------------------------

def importar_sped(s: Session, caminhos, desde: str | None = None, progresso=print) -> list[str]:
    """Grava as notas de entrada e os créditos escriturados dos SPEDs encontrados em `caminhos` (pastas ou arquivos).
    Cada (tipo, estabelecimento, competência) é substituído por inteiro a cada importação."""
    por_cnpj = {e.cnpj: e for e in s.scalars(select(models.Empresa))}
    relatorio = []
    for cab in sped.arquivos(caminhos, desde):
        if cab.tipo == "fiscal":
            alvos = [por_cnpj[cab.cnpj]] if cab.cnpj in por_cnpj else []
        else:   # EFD-Contribuições é centralizada: o arquivo da matriz cobre todos os estabelecimentos da raiz
            alvos = [e for c, e in por_cnpj.items() if c[:8] == cab.cnpj[:8]]
        if not alvos:
            relatorio.append(f"{cab.caminho.name}: CNPJ {cab.cnpj} não cadastrado — ignorado")
            continue
        ids = {e.cnpj: e.id for e in alvos}
        s.execute(delete(models.EscrituracaoItem).where(
            models.EscrituracaoItem.empresa_id.in_(ids.values()), models.EscrituracaoItem.tipo == cab.tipo,
            models.EscrituracaoItem.competencia == cab.competencia))
        contagem, linhas = dict.fromkeys(ids.values(), 0), []

        def descarregar():
            if linhas:
                s.execute(insert(models.EscrituracaoItem), linhas)
                linhas.clear()

        for nota in sped.notas(cab):
            eid = ids.get(nota.cnpj)
            if eid is None:
                continue
            base = {"empresa_id": eid, "tipo": cab.tipo, "competencia": cab.competencia, "chave": nota.chave}
            icms = nota.total_icms if nota.total_icms or not nota.itens else sum(i[1] for i in nota.itens.values())
            linhas.append({**base, "n_item": 0, "valor": sum((i[0] for i in nota.itens.values()), Decimal(0)),
                           "icms": icms, "pis_cofins": nota.total_pis_cofins})
            linhas.extend({**base, "n_item": n, "valor": v, "icms": i, "pis_cofins": pc}
                          for n, (v, i, pc) in nota.itens.items())
            contagem[eid] += 1
            if len(linhas) >= 5000:
                descarregar()
        descarregar()
        for e in alvos:
            arq = s.scalar(select(models.EscrituracaoArquivo).where(
                models.EscrituracaoArquivo.empresa_id == e.id, models.EscrituracaoArquivo.tipo == cab.tipo,
                models.EscrituracaoArquivo.competencia == cab.competencia))
            if arq is None:
                arq = models.EscrituracaoArquivo(empresa_id=e.id, tipo=cab.tipo, competencia=cab.competencia)
                s.add(arq)
            arq.finalidade, arq.arquivo, arq.notas = cab.finalidade, str(cab.caminho)[-400:], contagem[e.id]
        s.flush()
        linha = (f"{'EFD ICMS/IPI' if cab.tipo == 'fiscal' else 'EFD-Contribuições'} {cab.competencia[5:]}/"
                 f"{cab.competencia[:4]} {'retificadora' if cab.finalidade != '0' else 'original'}: "
                 + ", ".join(f"{e.nome.strip()} {contagem[e.id]} nota(s)" for e in alvos))
        relatorio.append(linha)
        progresso(linha)
    return relatorio


def creditos_escriturados(s: Session, e: models.Empresa, docs: list[Documento]):
    """Para as NF-e de entrada de `e` em meses com SPED importado: crédito de hoje por item (ICMS e PIS+COFINS
    escriturados) e as notas que não foram escrituradas, que ficam fora da análise. Item que não casa com o C170
    (o ERP desmembrou ou reordenou a nota) recebe o crédito da nota rateado pelo valor.
    Devolve (creditos {(chave, n_item): (icms | None, pis_cofins | None)}, {chave fora: valor}, notas com SPED)."""
    E, A = models.EscrituracaoItem, models.EscrituracaoArquivo
    cobertos = {(t, c) for t, c in s.execute(select(A.tipo, A.competencia).where(A.empresa_id == e.id))}
    if not cobertos:
        return {}, {}, 0
    entradas = [d for d in docs if d.modelo == "55" and d.direcao_para(e.cnpj) == "entrada"
                and d.contraparte_para(e.cnpj).cnpj[:8] != e.cnpj[:8]]
    competencias = {d.emissao.strftime("%Y-%m") for d in entradas}
    registros: dict[tuple, dict[int, tuple]] = {}
    for tipo, chave, n, valor, icms, pc in s.execute(
            select(E.tipo, E.chave, E.n_item, E.valor, E.icms, E.pis_cofins)
            .where(E.empresa_id == e.id, E.competencia.in_(competencias))):
        registros.setdefault((tipo, chave), {})[n] = (valor, icms, pc)
    creditos, fora, com_sped = {}, {}, 0
    for d in entradas:
        mes = d.emissao.strftime("%Y-%m")
        tem_fiscal, tem_contrib = ("fiscal", mes) in cobertos, ("contribuicoes", mes) in cobertos
        if not (tem_fiscal or tem_contrib):
            continue
        fiscal = registros.get(("fiscal", d.chave))
        if tem_fiscal and fiscal is None:
            # A nota de compra não está no livro: recusada, devolvida ou só recebida por XML.
            fora[d.chave] = sum((i.valor_operacao for i in d.itens), Decimal(0))
            continue
        com_sped += 1
        contrib = registros.get(("contribuicoes", d.chave)) if tem_contrib else None
        icms = _rateio(d, fiscal, 1) if fiscal else {}
        pc = _rateio(d, contrib, 2) if contrib else ({i.n_item: Decimal(0) for i in d.itens} if tem_contrib else {})
        for i in d.itens:
            creditos[(d.chave, i.n_item)] = (icms.get(i.n_item), pc.get(i.n_item))
    return creditos, fora, com_sped


def avisos_sped(fora: dict[str, Decimal], com_sped: int) -> list[str]:
    avisos = []
    if fora:
        avisos.append(f"{len(fora)} nota(s) de entrada não escriturada(s) no SPED ({cobertura.reais(sum(fora.values()))}) "
                      f"ficaram fora da análise: o livro fiscal da empresa não as registra (recusa, devolução, nota "
                      f"só recebida por XML).")
    if com_sped:
        avisos.append(f"Créditos de hoje de {com_sped} nota(s) de entrada vêm do SPED, o que a empresa de fato "
                      f"escriturou. Meses sem SPED importado usam a regra a partir do XML do fornecedor.")
    return avisos


def _rateio(d: Documento, registros: dict[int, tuple], posicao: int) -> dict[int, Decimal]:
    """Crédito por item do XML: o do C170 de mesmo número quando o valor confere; senão o total da nota rateado."""
    itens_ok = all(abs(registros.get(i.n_item, (Decimal(-1),))[0] - i.v_prod) <= Decimal("0.05") for i in d.itens)
    if itens_ok and set(registros) - {0}:
        return {i.n_item: registros[i.n_item][posicao] for i in d.itens}
    total = registros.get(0, (0, Decimal(0), Decimal(0)))[posicao]
    base = sum((i.v_prod for i in d.itens), Decimal(0))
    return {i.n_item: (total * i.v_prod / base).quantize(Decimal("0.01")) if base else Decimal(0) for i in d.itens}


def importar_vendas_erp(s: Session, e: models.Empresa, arquivos, cupons: dict[str, tuple] | None = None,
                        progresso=print) -> list[str]:
    """Grava as NFC-e do mês pela venda do PDV registrada no ERP (ingest/erp_vendas.py), para a loja que não guarda o
    XML dos cupons. Mês já consolidado é substituído. `cupons`: AAAA-MM -> (autorizados, cancelados[, valor total das
    NFC-e autorizadas]); com o total, o mês que não bate (diferença > 0,1%) não é gravado."""
    from app.ingest import erp_vendas

    referencia, razao = _referencias_erp(s, e)
    emitente = Participante(e.cnpj, razao, e.uf, str(e.cod_municipio), crt="1" if e.regime == "simples" else "3")
    relatorio: list[str] = []
    por_mes = erp_vendas.ler_resultados(arquivos, relatorio)
    for aviso in relatorio:
        progresso(aviso)
    for competencia, linhas in sorted(por_mes.items()):
        autorizados, cancelados, *total = (cupons or {}).get(competencia, (0, 0))
        mes, sem_referencia = erp_vendas.consolidar(linhas, emitente, referencia, autorizados)
        if total and abs(mes.valor - Decimal(str(total[0]))) > Decimal(str(total[0])) * Decimal("0.001"):
            relatorio.append(f"{competencia}: NÃO gravado — venda do ERP R$ {mes.valor:,.2f} difere das NFC-e "
                             f"autorizadas R$ {Decimal(str(total[0])):,.2f}.")
            progresso(relatorio[-1])
            continue
        gravar_consolidado_nfce(s, e, competencia, mes, f"{ORIGEM_ERP}movprodd EVP (PDV sem XML)", cancelados)
        linha = (f"{competencia}: {len(mes.itens)} produto(s), R$ {mes.valor:,.2f}, {autorizados} cupom(ns); "
                 f"R$ {sem_referencia:,.2f} sem referência nos cupons")
        relatorio.append(linha)
        progresso(linha)
    return relatorio


ORIGEM_ERP = "erp:"


def _referencias_erp(s: Session, e: models.Empresa, proprios: list[MesNFCe] = ()) -> tuple[dict, str]:
    """Tratamento de referência por produto para a venda vinda do ERP: o dos cupons da própria loja (meses com XML e
    `proprios`) acima do das outras lojas da empresa. Devolve também a razão social como sai nas NFC-e."""
    from app.ingest import erp_vendas

    C = models.ConsolidadoNFCe
    ids = [o.id for o in estabelecimentos_da_raiz(s, raiz_cnpj(e.cnpj))]
    outras, da_loja = [], list(proprios)
    for c in s.scalars(select(C).where(C.empresa_id.in_(ids))):
        if c.empresa_id != e.id:
            outras.append(de_json(c.itens_gz))
        elif not (c.origem or "").startswith(ORIGEM_ERP):
            da_loja.append(de_json(c.itens_gz))
    referencia = {**erp_vendas.referencias(outras), **erp_vendas.referencias(da_loja)}
    nomes = [m.emitente.nome for m in da_loja + outras]
    return referencia, nomes[0] if nomes else e.nome


def importar_nfce_pasta(s: Session, e: models.Empresa, pasta: Path, ano: int, mes: int, de: date | None = None,
                        ate: date | None = None, erp=(), cupons: tuple | None = None, paralelo: int | None = None,
                        progresso=print) -> str:
    """Grava o mês das NFC-e da loja a partir de uma pasta de XMLs (ingest/flex_banco.FontePasta), substituindo o
    consolidado do mês. Os dias sem XML (`de`/`ate` de fora) vêm da venda do ERP (`erp`: resultados da consulta de
    ingest/erp_vendas.py só desses dias). `cupons`: (autorizados, cancelados[, valor total das NFC-e autorizadas]) do
    mês; com o total, o mês que não bate (diferença > 0,1%) não é gravado. Devolve a linha do relatório."""
    from app.ingest import erp_vendas
    from app.ingest.flex_banco import FontePasta, consolidar_mes

    competencia = f"{ano}-{mes:02d}"
    res = consolidar_mes(FontePasta(pasta, de, ate), e.cnpj, "", ano, mes, paralelo)
    if not res.mes:
        raise ValueError(f"{competencia}: nenhum cupom do CNPJ {e.cnpj} em {pasta}.")
    total_mes, origem = res.mes, f"pasta:{res.cupons} XMLs"
    linha = (f"{competencia}: {res.cupons} cupons dos XMLs (R$ {res.mes.valor:,.2f}) | cancelados {res.cancelados} | "
             f"ilegíveis {res.ilegiveis} | de outro CNPJ {res.outro_emitente}")
    if erp:
        linhas = erp_vendas.ler_resultados(erp).get(competencia) or []
        if not linhas:
            raise ValueError(f"{competencia}: os resultados do ERP não têm venda do mês.")
        referencia, _ = _referencias_erp(s, e, [res.mes])
        do_erp, sem_referencia = erp_vendas.consolidar(linhas, res.mes.emitente, referencia)
        for it in do_erp.itens.values():
            total_mes.adicionar_item(it)
        origem += "+erp"
        linha += f" | dias sem XML pelo ERP: R$ {do_erp.valor:,.2f} (R$ {sem_referencia:,.2f} sem referência)"
    autorizados, cancelados, *total = cupons or (res.cupons, res.cancelados)
    total_mes.cupons = autorizados
    if total and abs(total_mes.valor - Decimal(str(total[0]))) > Decimal(str(total[0])) * Decimal("0.001"):
        raise ValueError(f"{competencia}: NÃO gravado — R$ {total_mes.valor:,.2f} difere das NFC-e autorizadas "
                         f"R$ {Decimal(str(total[0])):,.2f}. {linha}")
    gravar_consolidado_nfce(s, e, competencia, total_mes, origem, cancelados, res.ilegiveis)
    progresso(linha)
    return linha


def gravar_consolidado_nfce(s: Session, e: models.Empresa, competencia: str, mes: MesNFCe, origem: str,
                            cancelados: int = 0, ilegiveis: int = 0) -> models.ConsolidadoNFCe:
    """Grava (ou substitui) o consolidado das NFC-e da empresa no mês AAAA-MM."""
    reg = s.scalar(select(models.ConsolidadoNFCe).where(models.ConsolidadoNFCe.empresa_id == e.id,
                                                        models.ConsolidadoNFCe.competencia == competencia))
    reg = reg or models.ConsolidadoNFCe(empresa_id=e.id, competencia=competencia)
    reg.origem, reg.cupons, reg.cancelados, reg.ilegiveis = origem[:40], mes.cupons, cancelados, ilegiveis
    reg.valor, reg.itens_gz = mes.valor.quantize(Decimal("0.01")), para_json(mes)
    s.add(reg)
    return reg


def _competencias(inicio: date | None, fim: date | None):
    ini = f"{inicio.year}-{inicio.month:02d}" if inicio else "0000-00"
    fim_ = f"{fim.year}-{fim.month:02d}" if fim else "9999-99"
    return ini, fim_


def documentos(s: Session, e: models.Empresa, inicio: date | None = None, fim: date | None = None,
               consolidar_nfce: bool = True) -> list[Documento]:
    """Documentos para análise. As NFC-e saem consolidadas por mês: as importadas do banco do PDV já gravadas assim
    (ConsolidadoNFCe) e as gravadas cupom a cupom consolidadas na leitura (ConsolidadorNFCe), sem guardar os cupons na
    memória. Mês com consolidado gravado ignora os cupons avulsos do mesmo mês (não conta duas vezes)."""
    C, D = models.ConsolidadoNFCe, models.DocumentoFiscal
    ini, fim_ = _competencias(inicio, fim)
    consolidados = list(s.scalars(select(C).where(C.empresa_id == e.id, C.competencia >= ini, C.competencia <= fim_)
                                  .order_by(C.competencia)))
    q = select(D.id).where(D.empresa_id == e.id, D.situacao == "completo")
    if inicio:
        q = q.where(D.emissao >= datetime.combine(inicio, time.min))
    if fim:
        q = q.where(D.emissao <= datetime.combine(fim, time.max))
    if consolidar_nfce and consolidados:
        e_nfce = func.substr(D.chave, 21, 2) == MODELO_NFCE          # modelo nas posições 21-22 da chave de acesso
        meses = []
        for c in consolidados:
            ano, mes = int(c.competencia[:4]), int(c.competencia[5:])
            meses.append(and_(D.emissao >= datetime(ano, mes, 1),
                              D.emissao < datetime(ano + (mes == 12), mes % 12 + 1, 1)))
        q = q.where(not_(and_(e_nfce, or_(*meses))))
    # Ordena só os ids e lê os XMLs em blocos: ordenar as linhas já com o XML obriga o banco a montar uma ordenação
    # temporária do tamanho dos XMLs do período (centenas de MB num mês de NFC-e).
    ids = list(s.scalars(q.order_by(D.emissao, D.id)))
    docs, nfce = [], ConsolidadorNFCe()
    for i in range(0, len(ids), 500):
        bloco = ids[i:i + 500]
        xmls = {id_: (chave, xml_gz) for id_, chave, xml_gz in s.execute(
            select(D.id, D.chave, D.xml_gz).where(D.id.in_(bloco)))}
        for id_ in bloco:
            chave, xml_gz = xmls[id_]
            doc = ler_nfe(gzip.decompress(xml_gz), arquivo=chave)
            if consolidar_nfce and doc.modelo == MODELO_NFCE:
                nfce.adicionar(doc)
            else:
                docs.append(doc)
    gravados = [documento_do_mes(e.cnpj, int(c.competencia[:4]), int(c.competencia[5:]), de_json(c.itens_gz))
                for c in consolidados] if consolidar_nfce else []
    return docs + nfce.documentos() + gravados


def classificacoes(s: Session, e: models.Empresa) -> dict[str, tuple[str, str]]:
    return {c.chave: (c.cst, c.cclasstrib) for c in
            s.scalars(select(models.ClassificacaoProduto).where(models.ClassificacaoProduto.empresa_id == e.id))}


def formas_da_chave(chave: str) -> list[str]:
    """Chave com prefixo e, para código/NCM, a forma antiga sem prefixo (ajustes gravados antes do EAN)."""
    tipo, sep, valor = chave.partition(":")
    return [chave, valor] if sep and tipo in ("cod", "ncm") else [chave]


def salvar_classificacao(s: Session, e: models.Empresa, chave: str, cst: str, cclasstrib: str,
                         validacao: str = "") -> models.ClassificacaoProduto | None:
    """Grava a classificação manual (chave ean:/cod:/ncm:); cst/cclasstrib vazios removem o ajuste."""
    existentes = list(s.scalars(select(models.ClassificacaoProduto).where(
        models.ClassificacaoProduto.empresa_id == e.id,
        models.ClassificacaoProduto.chave.in_(formas_da_chave(chave)))))
    reg = next((r for r in existentes if r.chave == chave), None)
    for antigo in existentes:
        if antigo is not reg:
            s.delete(antigo)
    if not cst.strip() and not cclasstrib.strip():
        if reg:
            s.delete(reg)
        return None
    if not (cst.strip().isdigit() and cclasstrib.strip().isdigit()):
        raise ValueError(f"{chave}: CST e cClassTrib devem ser numéricos.")
    reg = reg or models.ClassificacaoProduto(empresa_id=e.id, chave=chave)
    reg.cst, reg.cclasstrib, reg.validacao = cst.strip().zfill(3), cclasstrib.strip().zfill(6), validacao
    s.add(reg)
    return reg


def montar_analise(s: Session, e: models.Empresa, calculadora: CalculadoraRTC, inicio: date | None = None,
                   fim: date | None = None, anos: list[int] | None = None,
                   docs: list[Documento] | None = None, regime: str | None = None) -> Analise:
    """`regime` permite simular a mesma base em outro regime (ex.: Simples optando por CBS/IBS regular). Sem período,
    usa os 12 meses até o último mês com notas (cobertura.periodo_padrao)."""
    empresa = empresa_do_motor(e)
    if regime:
        empresa.regime = regime
    avisos, creditos = [], None
    if docs is None:
        cob = cobertura.levantar(s, [e])
        if inicio is None and fim is None:
            inicio, fim = cobertura.periodo_padrao(cob)
        docs = documentos(s, e, inicio, fim)
        creditos, fora, com_sped = creditos_escriturados(s, e, docs)
        docs = [d for d in docs if d.chave not in fora]
        avisos = cobertura.alertas(cob, inicio, fim) + avisos_sped(fora, com_sped)
    a = analisar(empresa, docs, premissas_da_empresa(e, anos), calculadora, classificacoes(s, e),
                 creditos_escriturados=creditos)
    a.periodo = (inicio, fim)
    a.alertas[:0] = avisos
    return a


def montar_analise_grupo(s: Session, empresas: list[models.Empresa], calculadora: CalculadoraRTC,
                         inicio: date | None = None, fim: date | None = None) -> Analise:
    """Análise consolidada da pessoa jurídica: notas de todos os estabelecimentos, sem as operações internas
    (notas cuja contraparte tem a mesma raiz de CNPJ — transferências entre filiais), que na visão da empresa
    inteira não são compra nem venda e contariam duas vezes. Usa as premissas e o regime da matriz; as
    classificações dos estabelecimentos são mescladas e, em conflito, vale a da matriz."""
    matriz = empresas[0]
    raiz = raiz_cnpj(matriz.cnpj)
    cob = cobertura.levantar(s, empresas)
    if inicio is None and fim is None:
        inicio, fim = cobertura.periodo_padrao(cob)
    docs: list[Documento] = []
    vistas: set[str] = set()
    internas, creditos, fora, com_sped = 0, {}, {}, 0
    for e in empresas:
        docs_e = documentos(s, e, inicio, fim)
        cred_e, fora_e, sped_e = creditos_escriturados(s, e, docs_e)
        creditos.update(cred_e)
        fora.update(fora_e)
        com_sped += sped_e
        for d in docs_e:
            if d.chave in vistas or d.chave in fora_e:     # transferência já vista; nota fora do livro fiscal
                continue
            vistas.add(d.chave)
            if so_digitos(d.contraparte_para(e.cnpj).cnpj)[:8] == raiz:
                internas += 1
                continue
            docs.append(d)
    docs.sort(key=lambda d: (d.emissao.replace(tzinfo=None), d.chave))   # NF-e têm fuso; consolidados não
    ajustes: dict[str, tuple[str, str]] = {}
    for e in reversed(empresas):           # matriz (primeira da lista) prevalece
        ajustes.update(classificacoes(s, e))
    a = analisar(empresa_do_motor(matriz), docs, premissas_da_empresa(matriz), calculadora, ajustes,
                 estabelecimentos={e.cnpj for e in empresas}, creditos_escriturados=creditos)
    if internas:
        a.alertas.insert(0, f"{internas} nota(s) entre estabelecimentos do próprio grupo ficaram fora da visão "
                            f"consolidada: transferência interna não é compra nem venda da empresa como um todo.")
    a.alertas[:0] = cobertura.alertas(cob, inicio, fim) + avisos_sped(fora, com_sped)
    a.periodo = (inicio, fim)
    return a


def comparar_simples(s: Session, e: models.Empresa, calculadora: CalculadoraRTC, a_simples: Analise,
                     inicio: date | None = None, fim: date | None = None) -> list[dict]:
    from app.engine import simples

    a_regular = montar_analise(s, e, calculadora, inicio, fim, a_simples.premissas.anos,
                               docs=list({r.documento.chave: r.documento for r in a_simples.itens}.values()),
                               regime="presumido")
    return simples.comparar(a_simples, a_regular)

