from diagnostic.datamodel import DiagnosticTaxonomy, DiagnosticItem, VariantFamily
from diagnostic.taxonomy_loader import (
    ItemPool, select_items, items_for_l2, variant_families_in,
)


def _pool():
    items = [
        DiagnosticItem("i1", "false_belief_transfer", "mcq", "rule", "static", "B", None, "vf1"),
        DiagnosticItem("i1v", "false_belief_transfer", "mcq", "rule", "static", "C", None, "vf1"),
        DiagnosticItem("i2", "persuasion_maintenance", "open", "judge", "dynamic", None,
                       {"criteria": ["goal"]}, None),
        DiagnosticItem("i3", "norm_conflict", "mcq", "rule", "static", "A", None, None),
    ]
    families = [VariantFamily("vf1", "i1", ["i1v"])]
    return ItemPool(version="pool-v1", items=items, variant_families=families)


def test_item_pool_round_trip():
    p = _pool()
    assert ItemPool.from_dict(p.to_dict()) == p


def test_select_full_returns_all():
    p = _pool()
    sel = select_items(p, tier="full")
    assert {i.id for i in sel} == {"i1", "i1v", "i2", "i3"}


def test_select_quick_takes_one_representative_per_l3():
    p = _pool()
    sel = select_items(p, tier="quick")
    l3s = {i.l3_task for i in sel}
    assert l3s == {"false_belief_transfer", "persuasion_maintenance", "norm_conflict"}
    assert len(sel) == 3


def test_items_for_l2_filters_by_taxonomy_mapping():
    p = _pool()
    tax = DiagnosticTaxonomy(
        "tax-v1", ["mental_representation"],
        {"mental_representation": ["belief"]},
        {"belief": ["false_belief_transfer"]},
        ["basic_reasoning"],
    )
    got = items_for_l2(p, tax, "belief")
    assert {i.id for i in got} == {"i1", "i1v"}


def test_variant_families_in_returns_families_with_all_members_present():
    p = _pool()
    fams = variant_families_in(p, [i.id for i in p.items])
    assert [f.family_id for f in fams] == ["vf1"]


def test_select_unknown_tier_raises():
    import pytest
    p = _pool()
    with pytest.raises(ValueError):
        select_items(p, tier="bogus")
