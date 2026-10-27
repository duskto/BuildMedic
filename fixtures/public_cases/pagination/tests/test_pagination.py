import pytest
from demo.pagination import paginate

@pytest.mark.parametrize("page,expected", [(1, [0, 1, 2]), (2, [3, 4, 5]), (3, [6]), (4, [])])
def test_pages(page, expected):
    assert paginate(list(range(7)), page, 3) == expected

@pytest.mark.parametrize("page,size", [(0, 3), (1, 0), (-1, 3)])
def test_invalid_arguments(page, size):
    with pytest.raises(ValueError):
        paginate([1], page, size)

def test_input_unchanged():
    items = [1, 2, 3]
    paginate(items, 1, 2)
    assert items == [1, 2, 3]
