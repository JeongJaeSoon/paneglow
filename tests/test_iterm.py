import pytest

from paneglow.iterm import flatten, is_claude_job


class FakeSession:
    def __init__(self, name): self.name = name


class FakeSplitter:
    def __init__(self, *children, vertical=True):
        self.children = list(children)
        self.vertical = vertical


def test_flatten_returns_leaves_in_order():
    a, b, c = FakeSession("a"), FakeSession("b"), FakeSession("c")
    assert flatten(FakeSplitter(a, b, c), leaf=FakeSession) == [a, b, c]


def test_flatten_handles_nesting():
    a, b, c = FakeSession("a"), FakeSession("b"), FakeSession("c")
    tree = FakeSplitter(a, FakeSplitter(b, c))
    assert flatten(tree, leaf=FakeSession) == [a, b, c]


def test_flatten_single_leaf():
    a = FakeSession("a")
    assert flatten(a, leaf=FakeSession) == [a]


def test_flatten_reads_a_grid_row_by_row():
    """실측한 2x2 트리. iTerm2 는 열로 묶지만 사람은 행으로 읽는다.

    Splitter(vertical=True)
      ├─ Splitter(vertical=False) → 좌상, 좌하
      └─ Splitter(vertical=False) → 우상, 우하
    """
    tl, bl = FakeSession("top-left"), FakeSession("bottom-left")
    tr, br = FakeSession("top-right"), FakeSession("bottom-right")
    tree = FakeSplitter(FakeSplitter(tl, bl, vertical=False),
                        FakeSplitter(tr, br, vertical=False),
                        vertical=True)
    # 깊이 우선이면 tl, bl, tr, br 이 나와 A2 가 우상이 아니라 좌하를 가리킨다
    assert flatten(tree, leaf=FakeSession) == [tl, tr, bl, br]


def test_flatten_stacked_rows_stay_top_to_bottom():
    top, mid, bot = FakeSession("t"), FakeSession("m"), FakeSession("b")
    assert flatten(FakeSplitter(top, mid, bot, vertical=False),
                   leaf=FakeSession) == [top, mid, bot]


def test_flatten_uneven_columns():
    """왼쪽은 3분할, 오른쪽은 2분할 — 위에서부터 읽는다."""
    l1, l2, l3 = FakeSession("l1"), FakeSession("l2"), FakeSession("l3")
    r1, r2 = FakeSession("r1"), FakeSession("r2")
    tree = FakeSplitter(FakeSplitter(l1, l2, l3, vertical=False),
                        FakeSplitter(r1, r2, vertical=False),
                        vertical=True)
    assert flatten(tree, leaf=FakeSession) == [l1, r1, l2, r2, l3]


def test_claude_job_is_recognised_by_version_string():
    assert is_claude_job("2.1.220") is True
    assert is_claude_job("0.9.1-beta") is True


def test_non_claude_jobs():
    for job in ("zsh", "bash", "vim", "", None):
        assert is_claude_job(job) is False


@pytest.mark.integration
def test_reads_real_panes():
    import iterm2

    async def main(connection):
        from paneglow.iterm import current_tab_panes
        app = await iterm2.async_get_app(connection)
        panes = await current_tab_panes(app)
        assert all(p.tty.startswith("/dev/tty") for p in panes)

    iterm2.run_until_complete(main)
