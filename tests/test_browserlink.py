"""Tests for djcrate:// URI parsing (wire contract v1)."""
from cratebuilder.browserlink import (
    parse_djcrate_uri, BrowserSend, ParseError, MSG_NEWER, MSG_BAD_URL)


def test_valid_channel_uri():
    r = parse_djcrate_uri(
        "djcrate://add?v=1&kind=channel&url=https%3A%2F%2Fsoundcloud.com%2Fsomeartist")
    assert r == BrowserSend(kind="channel", url="https://soundcloud.com/someartist")


def test_valid_track_uri_with_encoded_query():
    r = parse_djcrate_uri(
        "djcrate://add?v=1&kind=track&url="
        "https%3A%2F%2Fwww.youtube.com%2Fwatch%3Fv%3DdQw4w9WgXcQ")
    assert r == BrowserSend(
        kind="track", url="https://www.youtube.com/watch?v=dQw4w9WgXcQ")


def test_parameter_order_is_not_significant():
    r = parse_djcrate_uri(
        "djcrate://add?url=https%3A%2F%2Fsoundcloud.com%2Fa&kind=channel&v=1")
    assert isinstance(r, BrowserSend)


def test_unknown_extra_parameters_are_ignored():
    r = parse_djcrate_uri(
        "djcrate://add?v=1&kind=track&extra=x&url=https%3A%2F%2Fsoundcloud.com%2Fa%2Fb")
    assert isinstance(r, BrowserSend)


def test_newer_version_and_unknown_kind_get_the_update_message():
    for uri in (
        "djcrate://add?v=2&kind=channel&url=https%3A%2F%2Fsoundcloud.com%2Fa",
        "djcrate://add?kind=channel&url=https%3A%2F%2Fsoundcloud.com%2Fa",   # v absent
        "djcrate://add?v=1&kind=playlist&url=https%3A%2F%2Fsoundcloud.com%2Fa",
        "djcrate://add?v=1&url=https%3A%2F%2Fsoundcloud.com%2Fa",            # kind absent
    ):
        r = parse_djcrate_uri(uri)
        assert r == ParseError(reason="newer", message=MSG_NEWER), uri


def test_bad_urls_get_the_unsupported_message():
    for uri in (
        "djcrate://add?v=1&kind=track",                                       # url absent
        "djcrate://add?v=1&kind=track&url=",                                  # url empty
        "djcrate://add?v=1&kind=track&url=http%3A%2F%2Fsoundcloud.com%2Fa",   # not https
        "djcrate://add?v=1&kind=track&url=https%3A%2F%2Fevil.example%2Fa",    # bad host
    ):
        r = parse_djcrate_uri(uri)
        assert r == ParseError(reason="bad_url", message=MSG_BAD_URL), uri


def test_non_add_verbs_and_junk_are_silently_ignored():
    for uri in (
        "djcrate://frobnicate?v=1",
        "https://soundcloud.com/a",       # not djcrate at all
        "show",
        "",
        None,
        12345,
    ):
        r = parse_djcrate_uri(uri)
        assert r == ParseError(reason="ignore", message=""), repr(uri)


def test_never_raises_on_garbage():
    parse_djcrate_uri("djcrate://add?%%%bad=encoding%")
    parse_djcrate_uri("djcrate://" + "x" * 100_000)
