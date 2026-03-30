from typing import get_args, get_type_hints

from rock.actions.sandbox._generated_types import SandboxInfoField
from rock.actions.sandbox.sandbox_info import SandboxInfo


def test_sandbox_info_field_literal_matches_typed_dict_keys() -> None:
    literal_fields = set(get_args(SandboxInfoField))
    typed_dict_fields = set(get_type_hints(SandboxInfo).keys())

    assert literal_fields == typed_dict_fields
