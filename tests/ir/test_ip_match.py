"""same_ip: family-aware, /prefix-tolerant IP equality with honest unknowns.

Lives in the IR layer because the ingester needs it (non-winning-row
conflict rule) and adapters never import checks (GS22-GW spec r4)."""

from digital_twin.ir import same_ip, same_subnet


def test_equal_and_prefix_tolerant():
    assert same_ip("10.0.0.1", "10.0.0.1") is True
    assert same_ip("10.0.0.1", "10.0.0.1/24") is True
    assert same_ip("10.0.0.1/32", "10.0.0.1/24") is True


def test_different_ips_are_false():
    assert same_ip("10.0.0.1", "10.0.0.2") is False


def test_mixed_families_are_not_equal():
    # GS25 lesson: never compare bare ints across families; v4 != v6 even
    # when the integer values collide (0.0.0.255 vs ::ff)
    assert same_ip("0.0.0.255", "::ff") is False


def test_unknown_inputs_return_none_never_a_guess():
    assert same_ip(None, None) is None
    assert same_ip(None, "10.0.0.1") is None
    assert same_ip("10.0.0.1", None) is None
    assert same_ip("foo", "10.0.0.1") is None
    assert same_ip("10.0.0.1", "{{gw}}") is None


def test_same_subnet_equal_normalizes_host_bits():
    assert same_subnet("10.0.10.0/24", "10.0.10.0/24") is True
    assert same_subnet("10.0.10.5/24", "10.0.10.0/24") is True  # strict=False
    assert same_subnet("10.0.10.0", "10.0.10.0") is True        # bare host -> /32


def test_same_subnet_different_networks_are_false():
    assert same_subnet("10.0.10.0/24", "10.0.11.0/24") is False
    assert same_subnet("10.0.10.0/24", "10.0.10.0/25") is False


def test_same_subnet_mixed_families_are_false():
    assert same_subnet("10.0.10.0/24", "2001:db8::/32") is False


def test_same_subnet_unknown_is_none():
    assert same_subnet(None, "10.0.10.0/24") is None
    assert same_subnet("10.0.10.0/24", None) is None
    assert same_subnet("not-a-subnet", "10.0.10.0/24") is None
    assert same_subnet("10.0.10.0/24", "{{subnet}}") is None
