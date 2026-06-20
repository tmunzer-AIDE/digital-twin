from digital_twin.ir import ClientEnrichment
from digital_twin.ir.diff import diff_ir
from digital_twin.ir.model import IRBuilder


def _ir_with(enrich: dict[str, ClientEnrichment]):
    b = IRBuilder()
    b.set_client_enrichment(enrich)
    return b.build()


def test_builder_exposes_client_enrichment():
    ir = _ir_with({"aa": ClientEnrichment(hostname="r2d2")})
    assert ir.client_enrichment["aa"].hostname == "r2d2"


def test_empty_default_is_empty_mapping():
    assert dict(IRBuilder().build().client_enrichment) == {}


def test_diff_ignores_client_enrichment():
    # the key non-load-bearing acceptance test: enrichment-only change -> empty diff
    base = _ir_with({"aa": ClientEnrichment(hostname="old")})
    prop = _ir_with(
        {"aa": ClientEnrichment(hostname="new"), "bb": ClientEnrichment(family="Printer")}
    )
    assert diff_ir(base, prop).is_empty()
