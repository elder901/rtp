import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12

os.environ.setdefault("RTP_CHAVE_CRIPTO", Fernet.generate_key().decode())

from app import db  # noqa: E402
from app.calculadora.client import AliquotaEfetiva  # noqa: E402

FX = Path(__file__).parent / "fixtures"
CNPJ_EMPRESA = "11222333000181"


class CalculadoraFake:
    """Imita a calculadora: cClassTrib 200003 (cesta básica) tem redução de 100%; o resto é integral."""

    def __init__(self):
        self.chaves = set()

    def aliquotas(self, chaves, nominais_por_ano, uf, municipio):
        self.chaves |= chaves
        out = {}
        for c in chaves:
            n = nominais_por_ano[c.ano]
            if c.cclasstrib == "200003":
                out[c] = AliquotaEfetiva(D(0), D(0), D(0), D(100))
            elif c.cclasstrib == "200035":  # higiene e limpeza (Anexo VIII): redução de 60%
                out[c] = AliquotaEfetiva(D(str(n["cbs"])) * D("0.4"), D(str(n["ibsEstadual"])) * D("0.4"),
                                         D(str(n["ibsMunicipal"])) * D("0.4"), D(60))
            else:
                out[c] = AliquotaEfetiva(D(str(n["cbs"])), D(str(n["ibsEstadual"])), D(str(n["ibsMunicipal"])), D(0))
        return out

    def situacoes_tributarias(self, data="2027-01-01"):
        return {"000": {"descricao": "Tributação integral", "classificacoes": {"000001": "Integral"}},
                "200": {"descricao": "Alíquota reduzida", "classificacoes": {"200003": "Cesta básica"}}}

    def ncm_aplicavel(self, cclasstrib, ncm, data="2027-01-01"):
        return not (cclasstrib == "200003" and not ncm.startswith("1006"))

    def versao(self):
        return {"versaoApp": "fake"}

    def identificacao(self):
        return {"url": "fake", "versao_app": "1.0-teste", "versao_base": "V0000", "data_base": "2026-01-01",
                "ambiente": "teste"}


@pytest.fixture
def banco(tmp_path):
    db.configurar(f"sqlite:///{tmp_path / 'teste.db'}")
    yield
    db.engine.dispose()


def gerar_pfx(cnpj: str = CNPJ_EMPRESA, senha: str = "segredo", dias: int = 365) -> bytes:
    """Certificado autoassinado no formato do e-CNPJ (CN 'RAZÃO:CNPJ')."""
    chave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    nome = x509.Name([x509.NameAttribute(x509.NameOID.COUNTRY_NAME, "BR"),
                      x509.NameAttribute(x509.NameOID.COMMON_NAME, f"LOJA TESTE LTDA:{cnpj}")])
    agora = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(nome).issuer_name(nome).public_key(chave.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(agora - timedelta(days=1))
            .not_valid_after(agora + timedelta(days=dias)).sign(chave, hashes.SHA256()))
    return pkcs12.serialize_key_and_certificates(b"teste", chave, cert, None,
                                                 serialization.BestAvailableEncryption(senha.encode()))
