"""Assinatura XMLDSig no padrão da NF-e (enveloped, C14N, RSA-SHA1), usada nos eventos de manifestação."""
from __future__ import annotations

import base64
import hashlib

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from lxml import etree

from app.dfe.certificado import CertificadoA1

DS = "http://www.w3.org/2000/09/xmldsig#"
C14N = "http://www.w3.org/TR/2001/REC-xml-c14n-20010315"


def _c14n(el) -> bytes:
    return etree.tostring(el, method="c14n")


def assinar(elemento_pai, elemento_assinado, cert: CertificadoA1):
    """Assina `elemento_assinado` (que deve ter atributo Id) e anexa <Signature> em `elemento_pai`."""
    uri = "#" + elemento_assinado.get("Id")
    digest = base64.b64encode(hashlib.sha1(_c14n(elemento_assinado)).digest()).decode()

    sig = etree.SubElement(elemento_pai, f"{{{DS}}}Signature", nsmap={None: DS})
    si = etree.SubElement(sig, f"{{{DS}}}SignedInfo")
    etree.SubElement(si, f"{{{DS}}}CanonicalizationMethod", Algorithm=C14N)
    etree.SubElement(si, f"{{{DS}}}SignatureMethod", Algorithm=f"{DS}rsa-sha1")
    ref = etree.SubElement(si, f"{{{DS}}}Reference", URI=uri)
    tr = etree.SubElement(ref, f"{{{DS}}}Transforms")
    etree.SubElement(tr, f"{{{DS}}}Transform", Algorithm=f"{DS}enveloped-signature")
    etree.SubElement(tr, f"{{{DS}}}Transform", Algorithm=C14N)
    etree.SubElement(ref, f"{{{DS}}}DigestMethod", Algorithm=f"{DS}sha1")
    etree.SubElement(ref, f"{{{DS}}}DigestValue").text = digest

    valor = cert.chave.sign(_c14n(si), padding.PKCS1v15(), hashes.SHA1())
    etree.SubElement(sig, f"{{{DS}}}SignatureValue").text = base64.b64encode(valor).decode()
    ki = etree.SubElement(sig, f"{{{DS}}}KeyInfo")
    x509d = etree.SubElement(ki, f"{{{DS}}}X509Data")
    etree.SubElement(x509d, f"{{{DS}}}X509Certificate").text = cert.certificado_der_b64()
    return sig


def verificar(elemento_assinado, sig) -> bool:
    """Confere digest e assinatura (usado nos testes)."""
    from cryptography import x509

    ns = {"ds": DS}
    digest = base64.b64encode(hashlib.sha1(_c14n(elemento_assinado)).digest()).decode()
    if digest != sig.findtext(".//ds:DigestValue", namespaces=ns):
        return False
    der = base64.b64decode(sig.findtext(".//ds:X509Certificate", namespaces=ns))
    chave = x509.load_der_x509_certificate(der).public_key()
    try:
        chave.verify(base64.b64decode(sig.findtext("ds:SignatureValue", namespaces=ns)),
                     _c14n(sig.find("ds:SignedInfo", namespaces=ns)), padding.PKCS1v15(), hashes.SHA1())
        return True
    except Exception:  # noqa: BLE001
        return False
