from digital_twin.scope.allowlist import (
    NAC_OBJECT_TYPES,
    ORG_OBJECT_TYPES,
    RAW_ALLOWLIST,
    SUPPORTED_OBJECT_TYPES,
)


def test_nac_object_types_are_separate():
    assert NAC_OBJECT_TYPES == ("nacrule",)
    assert "nacrule" not in SUPPORTED_OBJECT_TYPES   # site whitelist
    assert "nacrule" not in ORG_OBJECT_TYPES         # fan-out routing


def test_nacrule_leaves_are_exact_no_subtree():
    leaves = RAW_ALLOWLIST["nacrule"]
    assert "matching.auth_type" in leaves and "matching.nactags" in leaves
    assert "not_matching.vendor" in leaves and "apply_tags" in leaves
    assert "matching.*" not in leaves        # leaf-tightened, no subtree
    assert "name" in leaves and "order" in leaves and "action" in leaves
