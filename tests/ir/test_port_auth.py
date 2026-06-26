from digital_twin.ir.entities import PortAuth, admitted_methods, requires_auth, tightens


def test_default_portauth_equality():
    assert PortAuth() == PortAuth()


def test_requires_auth():
    assert requires_auth(None) is False
    assert requires_auth(PortAuth()) is False
    assert requires_auth(PortAuth(port_auth="dot1x")) is True
    assert requires_auth(PortAuth(mac_auth=True)) is True
    assert requires_auth(PortAuth(mac_auth_only=True)) is True


def test_admitted_methods():
    assert admitted_methods(None) is None              # no auth -> all admitted
    assert admitted_methods(PortAuth()) is None
    assert admitted_methods(PortAuth(port_auth="dot1x")) == frozenset({"dot1x"})
    assert admitted_methods(PortAuth(port_auth="dot1x", mac_auth=True)) == frozenset(
        {"dot1x", "mac"}
    )
    # mac-auth-only rejects dot1x supplicants
    assert admitted_methods(PortAuth(port_auth="dot1x", mac_auth_only=True)) == frozenset({"mac"})


def test_tightens_newly_requires_auth():
    assert tightens(None, PortAuth(port_auth="dot1x")) is True
    assert tightens(PortAuth(), PortAuth(mac_auth=True)) is True
    # already required -> still required: not a tightening (no new requirement)
    assert tightens(
        PortAuth(port_auth="dot1x"), PortAuth(port_auth="dot1x", persist_mac=True)
    ) is False
    # loosened: dropped auth
    assert tightens(PortAuth(port_auth="dot1x"), None) is False


def test_tightens_mac_auth_only_enabled():
    # dot1x -> dot1x + mac_auth_only: dot1x supplicants now rejected -> tightened
    assert tightens(PortAuth(port_auth="dot1x"),
                    PortAuth(port_auth="dot1x", mac_auth_only=True)) is True


def test_tightens_fallback_removed():
    # losing a guest/server-fail/server-reject fallback is a tightening
    assert tightens(PortAuth(port_auth="dot1x", guest_network="guest"),
                    PortAuth(port_auth="dot1x")) is True
    # gaining a fallback is NOT a tightening
    assert tightens(PortAuth(port_auth="dot1x"),
                    PortAuth(port_auth="dot1x", guest_network="guest")) is False


def test_persist_mac_only_is_non_default():
    # the false-SAFE guard: a persist_mac-only surface is NOT equal to the
    # all-default surface, so a change to it is detectable downstream
    assert PortAuth(persist_mac=True) != PortAuth()
