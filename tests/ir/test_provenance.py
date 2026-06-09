from digital_twin.ir.confidence import ConfidenceLevel
from digital_twin.ir.provenance import (
    CONFIG_META,
    OBSERVED_META,
    FactMeta,
    Provenance,
    fact_meta,
)


def test_authoritative_provenances_are_high():
    for prov in (
        Provenance.CONFIG,
        Provenance.DESIGNATED,
        Provenance.LLDP_TWO_SIDED,
        Provenance.OBSERVED,
    ):
        assert fact_meta(prov).confidence.level is ConfidenceLevel.HIGH


def test_inferred_is_medium_and_one_sided_lldp_is_low():
    assert fact_meta(Provenance.INFERRED).confidence.level is ConfidenceLevel.MEDIUM
    assert fact_meta(Provenance.LLDP_ONE_SIDED).confidence.level is ConfidenceLevel.LOW


def test_fact_meta_carries_reasons():
    m = fact_meta(Provenance.LLDP_ONE_SIDED, ("seen from S only",))
    assert m.confidence.reasons == ("seen from S only",)


def test_default_metas():
    assert CONFIG_META.provenance is Provenance.CONFIG
    assert CONFIG_META.confidence.level is ConfidenceLevel.HIGH
    assert OBSERVED_META.provenance is Provenance.OBSERVED


def test_factmeta_constructs():
    m = FactMeta(Provenance.CONFIG, fact_meta(Provenance.CONFIG).confidence)
    assert isinstance(m, FactMeta)
