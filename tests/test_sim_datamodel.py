from simulator.datamodel import Persona


def test_persona_round_trip():
    p = Persona(
        id="pers-001",
        traits=["谨慎", "共情"],
        motivation="维护团队和谐",
        speech_style="温和、迂回",
        values=["集体优先"],
        source="auto",
    )
    d = p.to_dict()
    assert d["id"] == "pers-001"
    assert d["traits"] == ["谨慎", "共情"]
    assert d["source"] == "auto"
    assert Persona.from_dict(d) == p


def test_persona_default_source_is_auto():
    p = Persona(id="p", traits=[], motivation="", speech_style="", values=[])
    assert p.source == "auto"
