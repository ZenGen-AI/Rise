from diagnostic.isolation import (
    token_similarity, structural_overlap, is_contaminated, filter_contaminated,
)


def test_token_similarity_identical_is_one():
    assert token_similarity("the cat sat", "the cat sat") == 1.0


def test_token_similarity_disjoint_is_zero():
    assert token_similarity("alpha beta", "gamma delta") == 0.0


def test_structural_overlap_matches_shared_keys():
    a = {"conflict": "resource", "norms": ["honesty"]}
    b = {"conflict": "resource", "norms": ["loyalty"]}
    assert structural_overlap(a, b) > 0     # conflict 键值相同


def test_is_contaminated_above_threshold():
    cand = {"text": "Anne moves the marble while Sally is away", "structure": {"conflict": "false_belief"}}
    pool_item = {"text": "Sally leaves and Anne moves the marble", "structure": {"conflict": "false_belief"}}
    assert is_contaminated(cand, [pool_item], text_threshold=0.3, struct_threshold=0.5) is True


def test_is_contaminated_below_threshold_is_clean():
    cand = {"text": "two traders negotiate a fair price", "structure": {"conflict": "resource"}}
    pool_item = {"text": "Sally leaves and Anne moves the marble", "structure": {"conflict": "false_belief"}}
    assert is_contaminated(cand, [pool_item], text_threshold=0.3, struct_threshold=0.5) is False


def test_filter_contaminated_removes_and_records():
    pool = [{"text": "Sally moves the marble", "structure": {"conflict": "false_belief"}}]
    candidates = [
        {"id": "c1", "text": "Sally moves the marble", "structure": {"conflict": "false_belief"}},
        {"id": "c2", "text": "traders negotiate price", "structure": {"conflict": "resource"}},
    ]
    kept, rejected = filter_contaminated(candidates, pool,
                                         text_threshold=0.3, struct_threshold=0.5)
    assert [c["id"] for c in kept] == ["c2"]
    assert len(rejected) == 1
    assert rejected[0]["id"] == "c1"
    assert "reason" in rejected[0]      # 记录剔除原因(硬约束:命中要记录)
