"""Operações de negócio sobre o banco: empresas, certificados, importação de XML e montagem da análise."""
from __future__ import annotations

import gzip
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal

from sqlalchemy import and_, func, not_, or_, select
from sqlalchemy.orm import Session

from app import models
from app.calculadora.client import CalculadoraRTC
from app.dfe.certificado import CertificadoA1, CertificadoInvalido
from app.engine.cenarios import Analise, Empresa, analisar
from app.engine.premissas import Premissas
from app.ingest.arquivos import iterar_xmls
from app.ingest.consolidacao import MODELO_NFCE, ConsolidadorNFCe, MesNFCe, de_json, documento_do_mes, para_json
from app.parser.nfe import Documento, ler_nfe
from app.seguranca import cifrar, decifrar

CAMPOS_PREMISSAS = ["cbs_referencia", "ibs_uf_referencia", "ibs_mun_referencia", "credito_fornecedor_simples_pct",
                    "credito_fornecedor_mei_pct", "credito_presumido_nao_contribuinte_pct", "repasse_is_revendedor_pct", "aliquota_das_pct", "das_pis_cofins_pct", "das_icms_iss_pct"]


def so_digitos(s: str) -> str:
    return "".join(c for c in s if c.isdigit())


# --- Empresas ----------------------------------------------------------------------------------------------------

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
    """`regime` permite simular a mesma base em outro regime (ex.: Simples optando por CBS/IBS regular)."""
    empresa = empresa_do_motor(e)
    if regime:
        empresa.regime = regime
    return analisar(empresa, docs if docs is not None else documentos(s, e, inicio, fim),
                    premissas_da_empresa(e, anos), calculadora, classificacoes(s, e))


def comparar_simples(s: Session, e: models.Empresa, calculadora: CalculadoraRTC, a_simples: Analise,
                     inicio: date | None = None, fim: date | None = None) -> list[dict]:
    from app.engine import simples

    a_regular = montar_analise(s, e, calculadora, inicio, fim, a_simples.premissas.anos,
                               docs=list({r.documento.chave: r.documento for r in a_simples.itens}.values()),
                               regime="presumido")
    return simples.comparar(a_simples, a_regular)

