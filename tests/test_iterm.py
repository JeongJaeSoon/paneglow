import pytest

from paneglow.iterm import flatten, is_claude_job


class FakeGrid:
    def __init__(self, width, height):
        self.width, self.height = width, height


class FakeSession:
    def __init__(self, name, width=80, height=24):
        self.name = name
        self.grid_size = FakeGrid(width, height)


class SizelessSession:
    """A session the API gave us no grid_size for."""
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
    """A measured 2x2 tree. iTerm2 groups by column; a person reads by row.

    Splitter(vertical=True)
      +- Splitter(vertical=False) -> top-left, bottom-left
      +- Splitter(vertical=False) -> top-right, bottom-right
    """
    tl, bl = FakeSession("top-left"), FakeSession("bottom-left")
    tr, br = FakeSession("top-right"), FakeSession("bottom-right")
    tree = FakeSplitter(FakeSplitter(tl, bl, vertical=False),
                        FakeSplitter(tr, br, vertical=False),
                        vertical=True)
    # Depth-first would give tl, bl, tr, br -- key A2 would point at the
    # bottom-left pane instead of the top-right one.
    assert flatten(tree, leaf=FakeSession) == [tl, tr, bl, br]


def test_flatten_stacked_rows_stay_top_to_bottom():
    top, mid, bot = FakeSession("t"), FakeSession("m"), FakeSession("b")
    assert flatten(FakeSplitter(top, mid, bot, vertical=False),
                   leaf=FakeSession) == [top, mid, bot]


def test_flatten_uneven_columns():
    """Left column split three ways, right column two. Read from the top."""
    l1, l2, l3 = FakeSession("l1"), FakeSession("l2"), FakeSession("l3")
    r1, r2 = FakeSession("r1"), FakeSession("r2")
    tree = FakeSplitter(FakeSplitter(l1, l2, l3, vertical=False),
                        FakeSplitter(r1, r2, vertical=False),
                        vertical=True)
    assert flatten(tree, leaf=FakeSession) == [l1, r1, l2, r2, l3]


def test_flatten_follows_a_dragged_divider():
    """Measured on real iTerm2 after dragging the left column's divider down.

        ttys010 (69 rows) | ttys011 (46 rows)
                          | ttys013 (46 rows)
        ttys012 (23 rows) |

    The right column's bottom pane really does sit above the left column's.
    Assuming even splits puts that pair backwards.
    """
    tl = FakeSession("ttys010", 67, 69)
    bl = FakeSession("ttys012", 67, 23)
    tr = FakeSession("ttys011", 67, 46)
    br = FakeSession("ttys013", 67, 46)
    tree = FakeSplitter(FakeSplitter(tl, bl, vertical=False),
                        FakeSplitter(tr, br, vertical=False),
                        vertical=True)
    assert flatten(tree, leaf=FakeSession) == [tl, tr, br, bl]


def test_flatten_falls_back_to_even_shares_without_grid_size():
    """No sizes available -- still return something sane rather than crashing."""
    tl, bl = SizelessSession("tl"), SizelessSession("bl")
    tr, br = SizelessSession("tr"), SizelessSession("br")
    tree = FakeSplitter(FakeSplitter(tl, bl, vertical=False),
                        FakeSplitter(tr, br, vertical=False),
                        vertical=True)
    assert flatten(tree, leaf=SizelessSession) == [tl, tr, bl, br]


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
