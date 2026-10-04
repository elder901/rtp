"""Cliente da Calculadora de Tributos RTC (Receita Federal).

Contrato: POST {base}/calculadora/regime-geral (OperacaoInput -> ROCDomain), Swagger em
{base}/swagger-ui/index.html.

Como o cálculo de CBS/IBS ad valorem é linear na base, a calculadora é consultada uma vez por combinação
(NCM, CST, cClassTrib, ano) com base 1000 e o resultado vira uma alíquota efetiva aplicada a cada item.
Isso reduz milhares de itens a poucas dezenas de chamadas. Monofasia (combustíveis, ad rem) não é linear
e é sinalizada para tratamento à parte.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import httpx

from app.config import settings

BASE_REF = Decimal("1000")
LOTE = 50


@dataclass(frozen=True)
class Chave:
    ncm: str
    cst: str
    cclasstrib: str
    ano: int


@dataclass
class AliquotaEfetiva:
    cbs: Decimal          # % efetivo sobre o valor líquido
    ibs_uf: Decimal
    ibs_mun: Decimal
    reducao_pct: Decimal  # % de redução aplicada pela classificação (0 = tributação integral)
    memoria: str = ""
    erro: str = ""

    @property
    def total(self) -> Decimal:
        return self.cbs + self.ibs_uf + self.ibs_mun


class ErroCalculadora(RuntimeError):
    pass


def _dec(v) -> Decimal:
    return Decimal(str(v)) if v not in (None, "") else Decimal("0")


class CalculadoraRTC:
    def __init__(self, base_url: str | None = None, client: httpx.Client | None = None):
        self.base_url = (base_url or settings.calculadora_url).rstrip("/")
        self.http = client or httpx.Client(timeout=settings.calculadora_timeout)
        self._cache: dict[Chave, AliquotaEfetiva] = {}

    def versao(self) -> dict:
        r = self.http.get(f"{self.base_url}/calculadora/dados-abertos/versao")
        r.raise_for_status()
        return r.json()

    def aliquotas(self, chaves: set[Chave], nominais_por_ano: dict[int, dict[str, float]],
                  uf: str, municipio: int) -> dict[Chave, AliquotaEfetiva]:
        faltantes = sorted((c for c in chaves if c not in self._cache),
                           key=lambda c: (c.ano, c.ncm, c.cst, c.cclasstrib))
        por_ano: dict[int, list[Chave]] = {}
        for c in faltantes:
            por_ano.setdefault(c.ano, []).append(c)
        for ano, lista in por_ano.items():
            for i in range(0, len(lista), LOTE):
                self._consultar_lote(lista[i:i + LOTE], ano, nominais_por_ano[ano], uf, municipio)
        return {c: self._cache[c] for c in chaves}

    def _consultar_lote(self, lote: list[Chave], ano: int, nominais: dict, uf: str, municipio: int):
        try:
            resultado = self._regime_geral(lote, ano, nominais, uf, municipio)
            for chave, obj in zip(lote, resultado):
                self._cache[chave] = obj
        except ErroCalculadora as e:
            if len(lote) == 1:
                self._cache[lote[0]] = AliquotaEfetiva(Decimal(0), Decimal(0), Decimal(0), Decimal(0), erro=str(e))
                return
            # Um item inválido derruba o lote inteiro: refaz um a um para isolar o erro.
            for chave in lote:
                self._consultar_lote([chave], ano, nominais, uf, municipio)

    def _regime_geral(self, lote: list[Chave], ano: int, nominais: dict, uf: str, municipio: int):
        payload = {
            "id": f"rtp-{ano}",
            "versao": "0.0.1",
            "dhFatoGerador": f"{ano}-01-15T12:00:00-03:00",
            "municipio": municipio,
            "uf": uf,
            "tpDoc": 55,
            "itens": [
                {
                    "numero": n,
                    "ncm": c.ncm,
                    "cst": c.cst,
                    "cClassTrib": c.cclasstrib,
                    "baseCalculo": float(BASE_REF),
                    "quantidade": 1,
                    "unidade": "UN",
                    "aliquotasNominais": nominais,
                }
                for n, c in enumerate(lote, start=1)
            ],
        }
        try:
            r = self.http.post(f"{self.base_url}/calculadora/regime-geral", json=payload)
        except httpx.HTTPError as e:
            raise ErroCalculadora(f"falha de comunicação com a calculadora: {e}") from e
        if r.status_code != 200:
            try:
                detalhe = r.json().get("detail") or r.text
            except ValueError:
                detalhe = r.text
            raise ErroCalculadora(f"HTTP {r.status_code}: {str(detalhe)[:300]}")

        objetos = {o["nObj"]: o for o in r.json().get("objetos", [])}
        saida = []
        for n in range(1, len(lote) + 1):
            g = (((objetos.get(n) or {}).get("tribCalc") or {}).get("IBSCBS") or {}).get("gIBSCBS") or {}
            cbs, uf_, mun = g.get("gCBS") or {}, g.get("gIBSUF") or {}, g.get("gIBSMun") or {}
            red = (cbs.get("gRed") or {}).get("pRedAliq")
            saida.append(AliquotaEfetiva(
                cbs=_dec(cbs.get("vCBS")) / BASE_REF * 100,
                ibs_uf=_dec(uf_.get("vIBSUF")) / BASE_REF * 100,
                ibs_mun=_dec(mun.get("vIBSMun")) / BASE_REF * 100,
                reducao_pct=_dec(red),
                memoria=cbs.get("memoriaCalculo", ""),
            ))
        return saida
