"""Premissas da transição (EC 132/2023, ADCT arts. 125-133, e LC 214/2025).

As alíquotas de referência de CBS e IBS ainda são estimativas: a calculadora RTC exige que, a partir de
01/01/2027, quem chama informe as alíquotas nominais. Por isso todas ficam editáveis aqui.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

D = Decimal


@dataclass(frozen=True)
class AnoTransicao:
    ano: int
    fator_icms_iss: Decimal     # fração do ICMS/ISS atual que ainda existe
    fator_pis_cofins: Decimal   # PIS/COFINS: extintos em 2027
    fator_ipi: Decimal          # IPI zerado em 2027 (exceto produtos com industrialização na ZFM)
    fator_ibs: Decimal          # fração da alíquota de referência do IBS
    cbs_reducao_pp: Decimal     # 2027-2028: CBS reduzida em 0,1 p.p.
    ibs_fixo_uf: Decimal | None = None   # 2027-2028: IBS fixo de 0,05% (UF) + 0,05% (município)
    ibs_fixo_mun: Decimal | None = None


TRANSICAO: dict[int, AnoTransicao] = {
    2027: AnoTransicao(2027, D("1"), D("0"), D("0"), D("0"), D("0.1"), D("0.05"), D("0.05")),
    2028: AnoTransicao(2028, D("1"), D("0"), D("0"), D("0"), D("0.1"), D("0.05"), D("0.05")),
    2029: AnoTransicao(2029, D("0.9"), D("0"), D("0"), D("0.1"), D("0")),
    2030: AnoTransicao(2030, D("0.8"), D("0"), D("0"), D("0.2"), D("0")),
    2031: AnoTransicao(2031, D("0.7"), D("0"), D("0"), D("0.3"), D("0")),
    2032: AnoTransicao(2032, D("0.6"), D("0"), D("0"), D("0.4"), D("0")),
    2033: AnoTransicao(2033, D("0"), D("0"), D("0"), D("1"), D("0")),
}


@dataclass
class Premissas:
    # Alíquotas de referência plenas (%), usadas nos exemplos do Swagger da calculadora (total 26,5%).
    cbs_referencia: Decimal = D("8.8")
    ibs_uf_referencia: Decimal = D("8.85")
    ibs_mun_referencia: Decimal = D("8.85")
    # Crédito de CBS/IBS que o adquirente aproveita de fornecedor do Simples (% do valor líquido).
    # Corresponde à parcela de CBS/IBS recolhida dentro do DAS; varia por anexo e faixa — estimativa.
    credito_fornecedor_simples_pct: Decimal = D("4.0")
    credito_fornecedor_mei_pct: Decimal = D("0")
    # Crédito presumido de CBS/IBS em compras de não contribuinte (ex.: produtor rural pessoa física), previsto na
    # LC 214/2025 com percentuais a regulamentar. Padrão conservador: zero.
    credito_presumido_nao_contribuinte_pct: Decimal = D("0")
    # Imposto Seletivo embutido no preço de revendedores (cobrado antes, no fabricante): % da alíquota aplicada
    # sobre o preço do revendedor. 100% superestima um pouco, pois o IS incidiu sobre o preço menor do fabricante.
    repasse_is_revendedor_pct: Decimal = D("100")
    # Alíquota efetiva do DAS da empresa analisada (só para empresas do Simples).
    aliquota_das_pct: Decimal = D("6.0")
    # Repartição do DAS (% do DAS): parcelas que CBS e IBS substituem. Padrão: Anexo I (comércio), 1ª faixa.
    das_pis_cofins_pct: Decimal = D("15.50")
    das_icms_iss_pct: Decimal = D("34.00")
    # Empresa industrial credita IPI hoje.
    industria: bool = False
    anos: list[int] = field(default_factory=lambda: list(TRANSICAO))

    def aliquotas_do_ano(self, ano: int, calculadora=None, codigo_uf: int | None = None,
                         cod_municipio: int | None = None) -> tuple[dict[str, float], dict[str, str]]:
        """Alíquotas nominais do ano e a origem de cada uma ("oficial" = base de regras da calculadora;
        "premissa" = alíquota de referência informada aqui, enquanto a oficial não for publicada)."""
        nominais = self.aliquotas_nominais(ano)
        origens = {k: "premissa" for k in nominais}
        if calculadora is None or not hasattr(calculadora, "aliquota_oficial"):
            return nominais, origens
        data = f"{ano}-01-15"
        for campo, esfera, codigo in (("cbs", "uniao", None), ("ibsEstadual", "uf", codigo_uf),
                                      ("ibsMunicipal", "municipio", cod_municipio)):
            if esfera != "uniao" and not codigo:
                continue
            try:
                oficial = calculadora.aliquota_oficial(esfera, data, codigo)
            except Exception as erro:  # noqa: BLE001
                if type(erro).__name__ == "CalculadoraIndisponivel":
                    raise
                # resposta inesperada: mantém a premissa, com a origem indicada
                origens[campo] = "premissa (calculadora indisponível)"
                continue
            if oficial is not None:
                nominais[campo], origens[campo] = float(oficial), "oficial"
        return nominais, origens

    def aliquotas_nominais(self, ano: int) -> dict[str, float]:
        """Alíquotas nominais (%) a enviar à calculadora para o ano informado."""
        t = TRANSICAO[ano]
        cbs = self.cbs_referencia - t.cbs_reducao_pp
        if t.ibs_fixo_uf is not None:
            uf, mun = t.ibs_fixo_uf, t.ibs_fixo_mun
        else:
            uf, mun = self.ibs_uf_referencia * t.fator_ibs, self.ibs_mun_referencia * t.fator_ibs
        return {"cbs": float(cbs), "ibsEstadual": float(uf), "ibsMunicipal": float(mun)}
