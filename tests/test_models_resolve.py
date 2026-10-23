import hashlib

import pytest

from controlplane.envelope import BigRef, Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.models import MissingCheckpointRefError, latest_promoted_checkpoint
from controlplane.paths import model_file


def _ref(tag: str) -> dict:
    # sha256 必须是合规的十六进制摘要(I8):从 tag 派生,不同 tag 仍得到不同引用
    return BigRef(uri=f"file:///ckpt/{tag}", sha256=hashlib.sha256(tag.encode()).hexdigest(), size_bytes=1).to_dict()


def _write_model(store, n, *, status="effective", ref="__default__"):
    payload = {"promoted_from": f"cand-run-{n}"}
    if ref == "__default__":
        payload["checkpoint_ref"] = _ref(f"v{n}")
    elif ref is not None:
        payload["checkpoint_ref"] = ref
    env = Envelope(
        id=f"zengen-v{n}", type="zengen_model", version=1,
        created_at="2026-09-26T00:00:00Z", created_by=OwnerRef(),
        status=status, schema_version=1, links=[], payload=payload,
    )
    store.write_product(model_file(n), env, f"seed zengen-v{n}")


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path))
    s.init()
    return s


def test_no_promoted_models_returns_none(store):
    assert latest_promoted_checkpoint(store) is None


def test_single_effective_model_resolves_its_checkpoint(store):
    _write_model(store, 1)
    base = latest_promoted_checkpoint(store)
    assert base.model_id == "zengen-v1"
    assert base.version == 1
    assert base.checkpoint_ref == BigRef.from_dict(_ref("v1"))


def test_versions_compare_numerically_not_lexicographically(store):
    # 字符串序下 "zengen-v10.yaml" < "zengen-v9.yaml"，会错选 v9
    _write_model(store, 9)
    _write_model(store, 10)
    assert latest_promoted_checkpoint(store).model_id == "zengen-v10"


def test_non_effective_latest_is_skipped(store):
    _write_model(store, 2)
    _write_model(store, 3, status="deprecated")
    assert latest_promoted_checkpoint(store).model_id == "zengen-v2"


def test_all_models_non_effective_returns_none(store):
    _write_model(store, 1, status="superseded")
    assert latest_promoted_checkpoint(store) is None


def test_latest_effective_without_checkpoint_ref_raises_instead_of_falling_back(store):
    _write_model(store, 1)
    _write_model(store, 2, ref=None)  # 最新的 effective 缺引用
    with pytest.raises(MissingCheckpointRefError, match="zengen-v2"):
        latest_promoted_checkpoint(store)


def test_missing_ref_on_a_non_effective_model_does_not_block_resolution(store):
    _write_model(store, 1)
    _write_model(store, 2, status="deprecated", ref=None)
    assert latest_promoted_checkpoint(store).model_id == "zengen-v1"


@pytest.mark.parametrize("bad", [
    {"uri": "file:///ckpt/v2", "sha256": "../../etc", "size_bytes": 1},
    {"uri": "file:///ckpt/v2", "sha256": "a" * 64, "size_bytes": -1},
    {"uri": "", "sha256": "a" * 64, "size_bytes": 1},
], ids=["sha256", "size_bytes", "uri"])
def test_an_unusable_checkpoint_ref_is_reported_like_a_missing_one(store, bad):
    """手改进树的坏引用(I8):转成 MissingCheckpointRefError,调用方已把它当成确定性失败
    (规划动作解析转 ResolveError、决策 failed),不让 InvalidBigRefError 漏出去打崩处理器。
    同样不回退到更早的版本。"""
    _write_model(store, 1)
    _write_model(store, 2, ref=bad)
    with pytest.raises(MissingCheckpointRefError, match="zengen-v2"):
        latest_promoted_checkpoint(store)
