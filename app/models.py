"""Modelos persistidos. Toda tabela de negócio carrega empresa_id (base do multi-cliente da fase 4)."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (JSON, Boolean, DateTime, ForeignKey, Index, Integer, LargeBinary, Numeric, String,
                        UniqueConstraint, func)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Empresa(Base):
    __tablename__ = "empresa"

    id: Mapped[int] = mapped_column(primary_key=True)
    cnpj: Mapped[str] = mapped_column(String(14), unique=True)
    nome: Mapped[str] = mapped_column(String(200))
    regime: Mapped[str] = mapped_column(String(10))           # real | presumido | simples
    uf: Mapped[str] = mapped_column(String(2))
    cod_municipio: Mapped[int] = mapped_column(Integer)
    industria: Mapped[bool] = mapped_column(Boolean, default=False)
    # Premissas editáveis (CBS/IBS de referência, crédito de fornecedor do Simples, DAS...).
    premissas: Mapped[dict] = mapped_column(JSON, default=dict)
    # DF-e
    ambiente_dfe: Mapped[int] = mapped_column(Integer, default=1)   # 1 = produção, 2 = homologação
    manifestar_ciencia: Mapped[bool] = mapped_column(Boolean, default=False)
    criado_em: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    certificado: Mapped["Certificado | None"] = relationship(back_populates="empresa", cascade="all, delete-orphan")
    controle_dfe: Mapped["ControleDFe | None"] = relationship(back_populates="empresa", cascade="all, delete-orphan")


class Certificado(Base):
    """Certificado A1 (.pfx) e senha, ambos cifrados (ver app/seguranca.py)."""
    __tablename__ = "certificado"

    id: Mapped[int] = mapped_column(primary_key=True)
    empresa_id: Mapped[int] = mapped_column(ForeignKey("empresa.id"), unique=True)
    pfx_cifrado: Mapped[bytes] = mapped_column(LargeBinary)
    senha_cifrada: Mapped[bytes] = mapped_column(LargeBinary)
    titular: Mapped[str] = mapped_column(String(300))
    cnpj_certificado: Mapped[str] = mapped_column(String(14))
    validade: Mapped[datetime] = mapped_column(DateTime)
    enviado_em: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    empresa: Mapped[Empresa] = relationship(back_populates="certificado")


class ControleDFe(Base):
    __tablename__ = "controle_dfe"

    id: Mapped[int] = mapped_column(primary_key=True)
    empresa_id: Mapped[int] = mapped_column(ForeignKey("empresa.id"), unique=True)
    ult_nsu: Mapped[str] = mapped_column(String(15), default="0")
    max_nsu: Mapped[str] = mapped_column(String(15), default="0")
    proxima_consulta: Mapped[datetime | None] = mapped_column(DateTime)
    ultimo_status: Mapped[str] = mapped_column(String(10), default="")
    ultimo_motivo: Mapped[str] = mapped_column(String(500), default="")
    atualizado_em: Mapped[datetime | None] = mapped_column(DateTime)

    empresa: Mapped[Empresa] = relationship(back_populates="controle_dfe")


class DocumentoFiscal(Base):
    __tablename__ = "documento"
    # (empresa_id, situacao): a assinatura do cache conta as notas completas sem ler a linha inteira (o XML está nela).
    __table_args__ = (UniqueConstraint("empresa_id", "chave"), Index("ix_documento_empresa_situacao", "empresa_id", "situacao"))

    id: Mapped[int] = mapped_column(primary_key=True)
    empresa_id: Mapped[int] = mapped_column(ForeignKey("empresa.id"), index=True)
    chave: Mapped[str] = mapped_column(String(44))
    origem: Mapped[str] = mapped_column(String(10))        # upload | dfe
    situacao: Mapped[str] = mapped_column(String(10))      # completo | resumo | cancelado
    direcao: Mapped[str] = mapped_column(String(10), default="")  # entrada | saida
    nsu: Mapped[str | None] = mapped_column(String(15))
    emissao: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    participante_cnpj: Mapped[str] = mapped_column(String(14), default="")
    participante_nome: Mapped[str] = mapped_column(String(200), default="")
    valor: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=0)
    xml_gz: Mapped[bytes | None] = mapped_column(LargeBinary)  # XML completo compactado (vazio quando resumo)
    manifestado_em: Mapped[datetime | None] = mapped_column(DateTime)
    criado_em: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ConsolidadoNFCe(Base):
    """NFC-e de um mês consolidadas por produto e tratamento fiscal (ingest/consolidacao.py). Guardar cada cupom
    custaria centenas de MB por loja e mês; a análise só usa as somas. A fonte (XML de cada cupom) fica no PDV."""
    __tablename__ = "consolidado_nfce"
    __table_args__ = (UniqueConstraint("empresa_id", "competencia"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    empresa_id: Mapped[int] = mapped_column(ForeignKey("empresa.id"), index=True)
    competencia: Mapped[str] = mapped_column(String(7))        # AAAA-MM
    origem: Mapped[str] = mapped_column(String(40))            # ex.: wrpdv:xmlpdv_0926/001
    cupons: Mapped[int] = mapped_column(Integer)                # válidos consolidados
    cancelados: Mapped[int] = mapped_column(Integer, default=0)
    ilegiveis: Mapped[int] = mapped_column(Integer, default=0)
    valor: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=0)  # soma do valor dos itens
    itens_gz: Mapped[bytes] = mapped_column(LargeBinary)       # JSON compactado (consolidacao.para_json)
    atualizado_em: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class EscrituracaoArquivo(Base):
    """Um SPED importado (ingest/sped.py): EFD ICMS/IPI ('fiscal') ou EFD-Contribuições ('contribuicoes') de um
    estabelecimento num mês. Mês com o arquivo importado tem as notas de entrada e os créditos que a empresa de fato
    escriturou; sem ele, a análise usa a regra a partir do XML do fornecedor."""
    __tablename__ = "escrituracao_arquivo"
    __table_args__ = (UniqueConstraint("empresa_id", "tipo", "competencia"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    empresa_id: Mapped[int] = mapped_column(ForeignKey("empresa.id"), index=True)
    tipo: Mapped[str] = mapped_column(String(15))              # fiscal | contribuicoes
    competencia: Mapped[str] = mapped_column(String(7))        # AAAA-MM
    finalidade: Mapped[str] = mapped_column(String(2))         # 0 original, 1 substituto (retificadora)
    arquivo: Mapped[str] = mapped_column(String(400))
    notas: Mapped[int] = mapped_column(Integer, default=0)
    importado_em: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class EscrituracaoItem(Base):
    """Crédito escriturado de um item de NF-e de entrada (C170). n_item 0 = a nota inteira (soma dos itens, ou o
    C190 quando a nota não traz itens): marca a nota como escriturada e serve de rateio quando os itens não casam."""
    __tablename__ = "escrituracao_item"

    id: Mapped[int] = mapped_column(primary_key=True)
    empresa_id: Mapped[int] = mapped_column(ForeignKey("empresa.id"), index=True)
    tipo: Mapped[str] = mapped_column(String(15))
    competencia: Mapped[str] = mapped_column(String(7), index=True)
    chave: Mapped[str] = mapped_column(String(44), index=True)
    n_item: Mapped[int] = mapped_column(Integer)
    valor: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=0)       # VL_ITEM (casa o item com o XML)
    icms: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=0)        # fiscal: ICMS creditado
    pis_cofins: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=0)  # contribuições: PIS + COFINS creditados


class ClassificacaoProduto(Base):
    """Classificação manual de CBS/IBS por código de produto ou NCM (código tem prioridade)."""
    __tablename__ = "classificacao"
    __table_args__ = (UniqueConstraint("empresa_id", "chave"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    empresa_id: Mapped[int] = mapped_column(ForeignKey("empresa.id"), index=True)
    chave: Mapped[str] = mapped_column(String(60))
    cst: Mapped[str] = mapped_column(String(3))
    cclasstrib: Mapped[str] = mapped_column(String(6))
    validacao: Mapped[str] = mapped_column(String(300), default="")  # resultado da validação na calculadora
    atualizado_em: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class CredencialApuracao(Base):
    """Client ID/Secret da API de apuração da CBS (gerados no portal RTC), cifrados."""
    __tablename__ = "credencial_apuracao"

    id: Mapped[int] = mapped_column(primary_key=True)
    empresa_id: Mapped[int] = mapped_column(ForeignKey("empresa.id"), unique=True)
    client_id_cifrado: Mapped[bytes] = mapped_column(LargeBinary)
    client_secret_cifrado: Mapped[bytes] = mapped_column(LargeBinary)
    ambiente: Mapped[str] = mapped_column(String(3), default="prr")  # prr = produção restrita | pro = produção
    atualizado_em: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class SolicitacaoApuracao(Base):
    """Solicitação assíncrona feita à API (débitos ou créditos)."""
    __tablename__ = "solicitacao_apuracao"

    id: Mapped[int] = mapped_column(primary_key=True)
    empresa_id: Mapped[int] = mapped_column(ForeignKey("empresa.id"), index=True)
    tipo: Mapped[str] = mapped_column(String(10))            # debitos | creditos
    tiquete: Mapped[str | None] = mapped_column(String(100), index=True)
    token_webhook: Mapped[str] = mapped_column(String(64), unique=True)
    estado: Mapped[str] = mapped_column(String(20), default="ABERTA")  # ABERTA, PENDENTE, EM_PROCESSAMENTO, CONCLUIDA, ERRO
    mensagem: Mapped[str] = mapped_column(String(500), default="")
    registros: Mapped[int] = mapped_column(Integer, default=0)
    criado_em: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    concluido_em: Mapped[datetime | None] = mapped_column(DateTime)


class RegistroApuracao(Base):
    """Débito ou crédito de CBS por documento, como a Receita apurou. Atualizado a cada consulta incremental."""
    __tablename__ = "registro_apuracao"
    __table_args__ = (UniqueConstraint("empresa_id", "tipo", "pa", "chave", "origem"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    empresa_id: Mapped[int] = mapped_column(ForeignKey("empresa.id"), index=True)
    tipo: Mapped[str] = mapped_column(String(10))   # debitos | creditos
    pa: Mapped[str] = mapped_column(String(7))      # AAAA-MM
    chave: Mapped[str] = mapped_column(String(50), index=True)
    origem: Mapped[int] = mapped_column(Integer)
    documento: Mapped[int] = mapped_column(Integer)
    emissao: Mapped[datetime | None] = mapped_column(DateTime)
    atualizacao: Mapped[datetime | None] = mapped_column(DateTime)
    apurado: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    saldo: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))   # débito: saldoDevedor | crédito: saldoCredor
    valores: Mapped[dict] = mapped_column(JSON, default=dict)        # grupo cbs completo, como veio


class Usuario(Base):
    """Quem entra na interface web. Papel: admin (todas as empresas; cadastra empresas e usuários), gestor (as empresas
    a que tem acesso: cadastra estabelecimentos, importa, classifica e cadastra usuários delas) e consulta (só vê e
    exporta — ex.: comprador levando a pauta de negociação)."""
    __tablename__ = "usuario"

    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(200), unique=True)
    senha_hash: Mapped[str] = mapped_column(String(300))
    papel: Mapped[str] = mapped_column(String(10))
    ativo: Mapped[bool] = mapped_column(Boolean, default=True)
    criado_em: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    ultimo_acesso: Mapped[datetime | None] = mapped_column(DateTime)

    acessos: Mapped[list["AcessoEmpresa"]] = relationship(back_populates="usuario", cascade="all, delete-orphan")


class AcessoEmpresa(Base):
    """Empresa (raiz do CNPJ: a pessoa jurídica com todos os estabelecimentos) que um usuário não administrador vê."""
    __tablename__ = "acesso_empresa"
    __table_args__ = (UniqueConstraint("usuario_id", "raiz"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    usuario_id: Mapped[int] = mapped_column(ForeignKey("usuario.id"), index=True)
    raiz: Mapped[str] = mapped_column(String(8))

    usuario: Mapped[Usuario] = relationship(back_populates="acessos")


class Sessao(Base):
    """Sessão aberta no login. O cookie leva um token aleatório; aqui fica só o hash dele."""
    __tablename__ = "sessao"

    id: Mapped[int] = mapped_column(primary_key=True)
    usuario_id: Mapped[int] = mapped_column(ForeignKey("usuario.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    criada_em: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    expira_em: Mapped[datetime] = mapped_column(DateTime)
