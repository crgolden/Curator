import pytest

from curator.psn.identifiers import (
    InvalidPsnIdentifierError,
    validate_account_id,
    validate_group_id,
    validate_np_communication_id,
    validate_online_id,
    validate_trophy_group,
)
from curator.psn.session import PsnSession

_TRAVERSAL_PAYLOADS = (
    "..",
    "../..",
    "%2e%2e",
    "%2E%2E%2F",
    "a/../b",
    "a%2f..%2fb",
    "a\\..\\b",
)


@pytest.mark.parametrize("value", ["0", "1234567890123456789", "8214019822170343784"[:20]])
def test_account_id_accepts_digits_up_to_twenty(value):
    assert validate_account_id(value) == value


@pytest.mark.parametrize("value", ["", "12a", "-1", "1.2", *_TRAVERSAL_PAYLOADS])
def test_account_id_rejects_anything_that_is_not_digits(value):
    with pytest.raises(InvalidPsnIdentifierError):
        validate_account_id(value)


@pytest.mark.parametrize("value", ["deeprog", "Abc", "a_b-c", "SixteenCharsLong"])
def test_online_id_accepts_psns_letter_digit_underscore_hyphen_alphabet(value):
    assert validate_online_id(value) == value


@pytest.mark.parametrize("value", ["ab", "SeventeenCharsXYZ", "has space", "has.dot", *_TRAVERSAL_PAYLOADS])
def test_online_id_rejects_out_of_range_lengths_and_path_characters(value):
    with pytest.raises(InvalidPsnIdentifierError):
        validate_online_id(value)


@pytest.mark.parametrize("value", ["NPWR15509_00", "NPWR00845_00", "NPWR1234567_99"])
def test_np_communication_id_accepts_the_npwr_shape(value):
    assert validate_np_communication_id(value) == value


@pytest.mark.parametrize("value", ["", "NPWR_00", "npwr15509_00", "NPWR15509", *_TRAVERSAL_PAYLOADS])
def test_np_communication_id_rejects_everything_else(value):
    with pytest.raises(InvalidPsnIdentifierError):
        validate_np_communication_id(value)


@pytest.mark.parametrize("value", ["all", "default", "001", "1"])
def test_trophy_group_accepts_alphanumeric_selectors(value):
    assert validate_trophy_group(value) == value


@pytest.mark.parametrize("value", ["", "a-b", "a_b", *_TRAVERSAL_PAYLOADS])
def test_trophy_group_rejects_everything_else(value):
    with pytest.raises(InvalidPsnIdentifierError):
        validate_trophy_group(value)


def test_group_id_accepts_a_derived_one_to_one_dm_id():
    value = "~300A4EB95AD46BAD.7DEFC8FC0BA52FCB"
    assert validate_group_id(value) == value


def test_group_id_accepts_a_server_allocated_multi_member_id():
    value = "ba08b67ca0b044b7688a29abdc884f37b5dd47cd-215"
    assert validate_group_id(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        "new-group",
        "~300A4EB95AD46BAD",
        "~300A4EB95AD46BAD.7DEFC8FC0BA52FC",
        "ba08b67ca0b044b7688a29abdc884f37b5dd47cd",
        "ba08b67ca0b044b7688a29abdc884f37b5dd47cd-",
        *_TRAVERSAL_PAYLOADS,
    ],
)
def test_group_id_rejects_anything_matching_neither_shape(value):
    with pytest.raises(InvalidPsnIdentifierError):
        validate_group_id(value)


def test_verified_url_rejects_a_percent_encoded_traversal_segment():
    with pytest.raises(ValueError, match="traversal"):
        PsnSession._verified_url("https://m.np.playstation.com/api/%2e%2e/%2e%2e/etc/passwd")


def test_verified_url_rejects_a_percent_encoded_separator_hiding_a_traversal():
    with pytest.raises(ValueError, match="traversal"):
        PsnSession._verified_url("https://m.np.playstation.com/api/a%2F..%2Fb")


def test_verified_url_rejects_a_backslash_traversal_segment():
    with pytest.raises(ValueError, match="traversal"):
        PsnSession._verified_url("https://m.np.playstation.com/api/a\\..\\b")


def test_verified_url_allows_an_ordinary_psn_path():
    url = "https://m.np.playstation.com/api/gamingLoungeGroups/v1/groups/abc-1/members/me"
    assert PsnSession._verified_url(url) == url
