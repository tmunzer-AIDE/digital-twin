from digital_twin.ir import Wlan
from digital_twin.ir.diff import diff_ir
from digital_twin.ir.model import IRBuilder


def _ir(*wlans: Wlan):
    b = IRBuilder()
    for w in wlans:
        b.add_wlan(w)
    return b.build()


def test_builder_exposes_wlans():
    ir = _ir(Wlan(id="w1", ssid="corp", enabled=True))
    assert ir.wlans[0].ssid == "corp" and ir.wlans[0].id == "w1"


def test_modeled_change_diffs():
    base = _ir(Wlan(id="w1", ssid="corp", enabled=True, isolation=False))
    prop = _ir(Wlan(id="w1", ssid="corp", enabled=True, isolation=True))
    assert diff_ir(base, prop).touches("wlan")


def test_inherited_only_flip_does_not_diff():
    base = _ir(Wlan(id="w1", ssid="corp", enabled=True, inherited=True))
    prop = _ir(Wlan(id="w1", ssid="corp", enabled=True, inherited=False))
    assert not diff_ir(base, prop).touches("wlan")
