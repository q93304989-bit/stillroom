"""内容寻址的产物仓库。

这个文件锁住两条硬性质（见 `runtime/artifacts.py` 的模块说明）：

1. **内容寻址**：同样的字节永远同一个地址；重复写入是空操作，不是"再写一份"。
2. **读时校验**：`get_bytes()` 重算 hash 并比对 —— 磁盘上那份被改过就必须炸，
   否则"地址是内容的函数"只是句口号，损坏会被静默当成正确内容喂给下游。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from runtime import ArtifactStore, RepositoryError
from runtime.artifacts import REF_PREFIX
from runtime.hashing import sha256_of_text
from validator.errors import ErrorCode


def _store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(tmp_path / "artifacts")


# ---------------------------------------------------------------------------
# 一、内容寻址
# ---------------------------------------------------------------------------

def test_same_bytes_land_on_the_same_address(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = store.put_bytes(b"hello")
    second = store.put_bytes(b"hello")

    assert first.sha256 == second.sha256 == sha256_of_text("hello")
    assert first.path == second.path
    assert (first.created, second.created) == (True, False)   # 第二次是空操作


def test_addresses_differ_when_bytes_differ(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.put_bytes(b"hello").sha256 != store.put_bytes(b"hello ").sha256


def test_json_is_normalized_before_hashing(tmp_path: Path) -> None:
    """键序不影响地址：同一个逻辑对象 → 同一份字节 → 同一个 hash。"""
    store = _store(tmp_path)
    first = store.put_json({"b": 2, "a": 1})
    second = store.put_json({"a": 1, "b": 2})

    assert first.sha256 == second.sha256
    assert second.created is False


def test_layout_is_two_levels_deep(tmp_path: Path) -> None:
    """`<root>/sha256/<前两位>/<完整 hex>` —— 分两层是为了单目录别堆几十万文件。"""
    store = _store(tmp_path)
    stored = store.put_bytes(b"payload")

    assert stored.path.parent.name == stored.sha256[:2]
    assert stored.path.parent.parent.name == "sha256"
    assert stored.path.name == stored.sha256


def test_bytes_are_not_mangled_by_text_decoding(tmp_path: Path) -> None:
    """二进制内容（非法 UTF-8）也要能存能取 —— 按字节算 hash，不走文本路径。"""
    store = _store(tmp_path)
    payload = bytes(range(256))
    stored = store.put_bytes(payload)
    assert store.get_bytes(stored.sha256) == payload
    assert stored.bytes == 256


# ---------------------------------------------------------------------------
# 二、ref 方案
# ---------------------------------------------------------------------------

def test_ref_round_trips(tmp_path: Path) -> None:
    store = _store(tmp_path)
    stored = store.put_bytes(b"x")

    assert stored.ref == f"{REF_PREFIX}{stored.sha256}"
    assert ArtifactStore.parse_ref(stored.ref) == stored.sha256
    assert store.get_by_ref(stored.ref) == b"x"


@pytest.mark.parametrize(
    "bad_ref",
    [
        "sha256:abc",                       # 前缀不对
        "artifact:sha256:",                 # 空 digest
        "artifact:sha256:nothex",           # digest 长度不对
        "",
        123,
    ],
)
def test_malformed_refs_raise_valueerror(tmp_path: Path, bad_ref: object) -> None:
    """ref 格式错是**编程错误**，不是数据问题 —— 直接 `ValueError`。"""
    with pytest.raises(ValueError):
        ArtifactStore.parse_ref(bad_ref)  # type: ignore[arg-type]


def test_a_ref_with_a_non_hex_digest_is_rejected_at_the_filesystem_boundary(
    tmp_path: Path,
) -> None:
    """长度对但不是十六进制：`parse_ref` 只管形状，`path_for` 那道才管合法性。

    两处分工是刻意的——`parse_ref` 不该假装自己懂 digest 的字母表。
    """
    store = _store(tmp_path)
    assert ArtifactStore.parse_ref(f"{REF_PREFIX}{'z' * 64}") == "z" * 64
    with pytest.raises(ValueError):
        store.get_by_ref(f"{REF_PREFIX}{'z' * 64}")


@pytest.mark.parametrize("bad", ["short", "g" * 64, "", None, 64])
def test_malformed_digests_raise_valueerror(tmp_path: Path, bad: object) -> None:
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        store.path_for(bad)  # type: ignore[arg-type]


def test_put_bytes_rejects_non_bytes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(TypeError):
        store.put_bytes("a string")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 三、读时校验
# ---------------------------------------------------------------------------

def test_tampered_content_is_detected(tmp_path: Path) -> None:
    """把磁盘上的内容改掉 → 地址与内容不再对应 → 必须炸，不许静默返回错内容。"""
    store = _store(tmp_path)
    stored = store.put_bytes(b'{"a": 1}')

    stored.path.write_bytes(b'{"a": 2}')
    with pytest.raises(RepositoryError) as exc:
        store.get_bytes(stored.sha256)
    assert exc.value.code == ErrorCode.SCHEMA_INVALID.value
    assert exc.value.details["sha256"] == stored.sha256


def test_truncated_content_is_detected(tmp_path: Path) -> None:
    """截断也是一种篡改：不能因为"前缀还对"就放行。"""
    store = _store(tmp_path)
    stored = store.put_bytes(b"x" * 100)

    stored.path.write_bytes(b"x" * 50)
    with pytest.raises(RepositoryError) as exc:
        store.get_bytes(stored.sha256)
    assert exc.value.code == ErrorCode.SCHEMA_INVALID.value


def test_missing_content_is_reported_as_not_found(tmp_path: Path) -> None:
    store = _store(tmp_path)
    digest = sha256_of_text("never stored")
    assert store.has(digest) is False
    with pytest.raises(RepositoryError) as exc:
        store.get_bytes(digest)
    assert exc.value.code == ErrorCode.EXECUTION_NOT_FOUND.value


def test_json_get_round_trips_and_revalidates(tmp_path: Path) -> None:
    store = _store(tmp_path)
    value = {"prompt": "画一只猫", "nested": [1, 2, {"k": "v"}]}
    stored = store.put_json(value)

    assert store.get_json(stored.sha256) == value

    stored.path.write_bytes(b'{"prompt": "tampered"}')
    with pytest.raises(RepositoryError):
        store.get_json(stored.sha256)          # 校验发生在解码之前


# ---------------------------------------------------------------------------
# 四、跨实例 / 落盘形态
# ---------------------------------------------------------------------------

def test_a_second_instance_on_the_same_root_reads_the_same_content(tmp_path: Path) -> None:
    """store 是纯文件系统，没有进程内状态 —— 换一个实例必须读得到同一份内容。"""
    root = tmp_path / "artifacts"
    stored = ArtifactStore(root).put_json({"shared": True})

    other = ArtifactStore(root)
    assert other.has(stored.sha256)
    assert other.get_json(stored.sha256) == {"shared": True}


def test_root_is_created_on_demand(tmp_path: Path) -> None:
    root = tmp_path / "deep" / "nested" / "artifacts"
    store = ArtifactStore(root)
    assert root.is_dir()
    assert store.root == root


def test_write_leaves_no_staging_files_behind(tmp_path: Path) -> None:
    """临时文件必须被 `os.replace` 吃掉 —— 落完盘的目录里只应有正式文件。

    残留的 `.tmp-*` 会让"这个目录里的东西都是内容"这句话不成立，
    将来按目录清点（比如算占用、做打包）就会把垃圾算进去。
    """
    store = _store(tmp_path)
    for i in range(5):
        store.put_bytes(f"payload-{i}".encode())

    leftovers = [p for p in store.root.rglob("*") if ".tmp-" in p.name]
    assert leftovers == []


def test_stored_json_matches_the_canonical_form(tmp_path: Path) -> None:
    """落盘的是规范化 JSON，不是原样的 `json.dumps` —— 否则地址会随键序漂。"""
    store = _store(tmp_path)
    stored = store.put_json({"b": 1, "a": 2})
    text = stored.path.read_text(encoding="utf-8")

    assert text == '{"a":2,"b":1}'          # 键排序 + 紧凑分隔符 + 不转义非 ASCII
    assert stored.sha256 == sha256_of_text(text)
