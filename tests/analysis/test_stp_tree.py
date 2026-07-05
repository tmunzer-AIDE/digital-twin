"""Engine-side pins for the relocated election helper."""
from digital_twin.analysis.stp_tree import ABSTAIN, DEFAULT_PRIORITY, root_of
from digital_twin.ir import IRBuilder
from tests.factories import sw


def test_root_of_semantics_pinned_at_new_home():
    # <2 switches -> None; else min (priority ?? 32768, device_id); assumed flag
    b = IRBuilder()
    b.add_device(sw("aa01", stp_priority=4096)).add_device(sw("bb02"))  # None -> 32768
    ir = b.build()
    assert root_of(ir, frozenset({"aa01"})) is None
    assert root_of(ir, frozenset({"aa01", "bb02"})) == ("aa01", True)
    assert DEFAULT_PRIORITY == 32768 and ABSTAIN == "abstain"
