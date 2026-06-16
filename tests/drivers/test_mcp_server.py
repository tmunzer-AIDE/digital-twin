from dataclasses import dataclass
from datetime import UTC, datetime

from digital_twin.drivers.mcp_server import simulate_change
from digital_twin.observability.replay.store import ReplayStore
from digital_twin.providers.base import OrgTemplateContext, RawSiteState, SiteScope, StateMeta
from tests.adapters.mist.fixtures import raw_site


def test_tool_returns_verdict_dict_and_never_raises(tmp_path):
    fixture = ReplayStore(tmp_path).save_raw("fx", raw_site())
    out = simulate_change({"source": "mist", "ops": "garbage"}, replay_fixture=str(fixture))
    assert out["decision"] == "unknown"  # bad plan -> verdict, not an exception


def test_tool_isolates_internal_errors(tmp_path):
    out = simulate_change({"source": "mist"}, replay_fixture=str(tmp_path / "missing.json"))
    assert out["decision"] == "unknown"
    assert any("error" in r.lower() or "fixture" in r.lower() for r in out["decision_reasons"])


def test_error_path_returns_the_full_verdict_document_shape(tmp_path):
    # agents need predictable fields ESPECIALLY on errors: the error path must
    # be a real assembled Verdict, not a hand-rolled 3-key dict
    ok = simulate_change(
        {"source": "mist", "ops": "garbage"},
        replay_fixture=str(ReplayStore(tmp_path).save_raw("fx", raw_site())),
    )
    err = simulate_change({"source": "mist"}, replay_fixture=str(tmp_path / "missing.json"))
    assert set(err.keys()) == set(ok.keys())  # identical document shape


def test_l0_full_object_threads_to_simulate(tmp_path, monkeypatch):
    # the MCP tool exposes l0_full_object; it must reach the engine (default
    # False = changed-roots scope, True = whole-object validation)
    import digital_twin.drivers.mcp_server as srv

    seen = []
    real = srv.simulate

    def spy(plan, **kwargs):
        seen.append(kwargs.get("l0_full_object"))
        return real(plan, **kwargs)

    monkeypatch.setattr(srv, "simulate", spy)
    fixture = str(ReplayStore(tmp_path).save_raw("fx", raw_site()))

    simulate_change({"source": "mist", "ops": "garbage"}, replay_fixture=fixture)
    simulate_change(
        {"source": "mist", "ops": "garbage"}, replay_fixture=fixture, l0_full_object=True
    )
    assert seen == [False, True]


# ---------------------------------------------------------------------------
# ORG (networktemplate) path tests
# ---------------------------------------------------------------------------

def _meta():
    return StateMeta(
        acquired_at=datetime.now(UTC), host="h",
        fetched=("site", "setting", "devices", "wireless_clients", "wired_clients"),
        failures=(),
    )


def _org_site(sid, *, setting, devices, nt):
    return RawSiteState(
        scope=SiteScope("o1", sid),
        site={"id": sid, "networktemplate_id": "nt1"},
        setting=setting,
        networktemplate=nt,
        devices=tuple(devices),
        device_stats=(),
        port_stats=(),
        wireless_clients=(),
        wired_clients=(),
        derived_setting=None,
        meta=_meta(),
    )


@dataclass
class _OrgFakeProvider:
    sites: dict
    template: dict

    def resolve_org_template(self, scope, template_id, object_type="networktemplate"):
        return OrgTemplateContext(template=self.template, assigned_site_ids=tuple(self.sites))

    def fetch_sites(self, scope, site_ids=None, *, include_derived=False):
        ids = list(site_ids) if site_ids is not None else list(self.sites)
        return {sid: self.sites[sid] for sid in ids}

    def fetch_site(self, scope, *, include_derived=False):
        raise NotImplementedError


def test_org_tool_returns_dict_with_decision_and_never_raises(monkeypatch):
    """simulate_change() auto-detects an org plan, calls simulate_org_template,
    and returns a dict with 'decision' — never raises."""
    tmpl = {"id": "nt1", "networks": {}}
    s1 = _org_site("s1", setting={"id": "s1"}, devices=(), nt=tmpl)
    fake = _OrgFakeProvider(sites={"s1": s1}, template=tmpl)

    import digital_twin.providers.mist_api as mist_mod
    monkeypatch.setattr(mist_mod, "MistApiProvider", lambda: fake)

    org_plan = {
        "source": "mist",
        "scope": {"org_id": "o1"},
        "ops": [{"action": "update", "order": 0, "object_type": "networktemplate",
                 "object_id": "nt1", "payload": {"networks": {}}}],
    }
    out = simulate_change(org_plan)
    assert "decision" in out
    assert out["template_id"] == "nt1"
    assert "per_site" in out
    assert out["decision"] in ("safe", "review", "unsafe", "unknown")


def test_org_tool_never_raises_on_internal_error(monkeypatch):
    """Even when simulate_org_template raises, the tool returns a well-formed
    UNKNOWN OrgVerdict dict instead of propagating the exception."""
    import digital_twin.drivers.mcp_server as mcp_mod

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(mcp_mod, "simulate_org_template", _boom)

    org_plan = {
        "source": "mist",
        "scope": {"org_id": "o1"},
        "ops": [{"action": "update", "order": 0, "object_type": "networktemplate",
                 "object_id": "nt1", "payload": {}}],
    }
    # must not raise
    out = simulate_change(org_plan)
    assert out["decision"] == "unknown"
    assert "template_id" in out  # org-shaped error envelope
