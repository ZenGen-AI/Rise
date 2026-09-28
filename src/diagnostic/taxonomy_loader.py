from __future__ import annotations

from dataclasses import dataclass

from diagnostic.datamodel import DiagnosticItem, DiagnosticTaxonomy, VariantFamily


@dataclass
class ItemPool:
    version: str
    items: list[DiagnosticItem]
    variant_families: list[VariantFamily]

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "items": [i.to_dict() for i in self.items],
            "variant_families": [f.to_dict() for f in self.variant_families],
        }

    @classmethod
    def from_dict(cls, d: dict) -> ItemPool:
        return cls(
            version=d["version"],
            items=[DiagnosticItem.from_dict(i) for i in d["items"]],
            variant_families=[VariantFamily.from_dict(f) for f in d["variant_families"]],
        )


def select_items(pool: ItemPool, tier: str) -> list[DiagnosticItem]:
    """
    Select items from pool based on tier.

    - tier == "full": return all items in order
    - tier == "quick": return one representative per distinct l3_task (first encountered)
    - else: raise ValueError
    """
    if tier == "full":
        return list(pool.items)
    elif tier == "quick":
        seen: dict[str, DiagnosticItem] = {}
        for item in pool.items:
            if item.l3_task not in seen:
                seen[item.l3_task] = item
        return list(seen.values())
    else:
        raise ValueError(f"unknown tier: {tier}")


def items_for_l2(pool: ItemPool, taxonomy: DiagnosticTaxonomy, l2: str) -> list[DiagnosticItem]:
    """
    Return items whose l3_task is in the taxonomy mapping for the given l2.

    Looks up taxonomy.l3_tasks[l2] to get the set of l3 tasks,
    then filters pool.items by l3_task membership (order preserved).
    """
    l3_tasks_for_l2 = set(taxonomy.l3_tasks[l2])
    return [item for item in pool.items if item.l3_task in l3_tasks_for_l2]


def variant_families_in(pool: ItemPool, item_ids: list[str]) -> list[VariantFamily]:
    """
    Return families where both the base_item_id and all variant_item_ids are in item_ids.

    Order preserved from pool.variant_families.
    """
    item_id_set = set(item_ids)
    result = []
    for fam in pool.variant_families:
        if fam.base_item_id in item_id_set and all(vid in item_id_set for vid in fam.variant_item_ids):
            result.append(fam)
    return result
