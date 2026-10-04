"""Certificado digital A1 (.pfx/.p12) para TLS mútuo com a SEFAZ e assinatura de eventos."""
from __future__ import annotations

import os
import re
import secrets
import ssl
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone

import certifi
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from cryptography.hazmat.primitives.serialization import pkcs12

OID_CNPJ_ICP = "2.16.76.1.3.3"  # otherName do e-CNPJ ICP-Brasil


class CertificadoInvalido(ValueError):
    pass


@dataclass
class CertificadoA1:
    chave: RSAPrivateKey
    certificado: x509.Certificate
    cadeia: list[x509.Certificate]

    @classmethod
    def carregar(cls, pfx: bytes, senha: str) -> "CertificadoA1":
        try:
            chave, cert, cadeia = pkcs12.load_key_and_certificates(pfx, senha.encode() if senha else None)
        except ValueError as e:
            raise CertificadoInvalido("Não foi possível abrir o certificado: senha incorreta ou arquivo inválido.") from e
        if chave is None or cert is None:
            raise CertificadoInvalido("O arquivo não contém chave privada e certificado.")
        if not isinstance(chave, RSAPrivateKey):
            raise CertificadoInvalido("A SEFAZ exige certificado com chave RSA.")
        return cls(chave, cert, list(cadeia or []))

    @property
    def titular(self) -> str:
        cn = self.certificado.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
        return cn[0].value if cn else self.certificado.subject.rfc4514_string()

    @property
    def validade(self) -> datetime:
        return self.certificado.not_valid_after_utc

    @property
    def vencido(self) -> bool:
        return self.validade < datetime.now(timezone.utc)

    @property
    def cnpj(self) -> str:
        """CNPJ do e-CNPJ: primeiro pelo otherName ICP-Brasil, depois pelo padrão 'RAZÃO:CNPJ' do CN."""
        try:
            san = self.certificado.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            for nome in san.get_values_for_type(x509.OtherName):
                if nome.type_id.dotted_string == OID_CNPJ_ICP:
                    digitos = re.sub(r"\D", "", nome.value.decode("latin-1"))
                    if len(digitos) >= 14:
                        return digitos[-14:]
        except x509.ExtensionNotFound:
            pass
        m = re.search(r"(\d{14})\s*$", self.titular)
        return m.group(1) if m else ""

    def certificado_der_b64(self) -> str:
        import base64

        return base64.b64encode(self.certificado.public_bytes(serialization.Encoding.DER)).decode()

    def contexto_ssl(self) -> ssl.SSLContext:
        """Contexto TLS com o certificado cliente. A chave só toca o disco cifrada e por instantes."""
        ctx = ssl.create_default_context(cafile=certifi.where())
        senha_tmp = secrets.token_urlsafe(32).encode()
        cert_pem = b"".join(c.public_bytes(serialization.Encoding.PEM) for c in [self.certificado, *self.cadeia])
        chave_pem = self.chave.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                             serialization.BestAvailableEncryption(senha_tmp))
        arquivos = []
        try:
            for conteudo in (cert_pem, chave_pem):
                fd, caminho = tempfile.mkstemp(suffix=".pem")
                with os.fdopen(fd, "wb") as f:
                    f.write(conteudo)
                arquivos.append(caminho)
            ctx.load_cert_chain(arquivos[0], arquivos[1], password=senha_tmp)
        finally:
            for caminho in arquivos:
                os.remove(caminho)
        return ctx
