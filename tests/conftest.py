import pytest

from fbdstudio.blocks import Library
from fbdstudio.boards import Boards


@pytest.fixture(scope="session")
def library():
    lib = Library()
    assert not lib.errors, lib.errors
    return lib


@pytest.fixture(scope="session")
def board():
    return Boards().get("esp32dev")
