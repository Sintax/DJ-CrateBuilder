import zipfile
from urllib.parse import parse_qs, urlsplit

from cratebuilder import support


def test_scrub_replaces_home_library_username_and_secrets():
    text = (r"C:\Users\djsin\Music\DJ-CrateBuilder\YouTube\House\x.mp3 "
            r"C:\Users\djsin\AppData\Roaming\x  user djsin here "
            "token=abc123 cookiefile C:/Users/djsin/cookies.txt "
            "from 192.168.1.20 mail dj@example.com")
    out = support.scrub_text(
        text, home=r"C:\Users\djsin",
        base_dir=r"C:\Users\djsin\Music\DJ-CrateBuilder",
        username="djsin", cookie_file=r"C:/Users/djsin/cookies.txt")
    assert "djsin" not in out
    assert "<LIBRARY>" in out and "<HOME>" in out and "<USER>" in out
    assert "token=<redacted>" in out
    assert "<COOKIE_FILE>" in out
    assert "<IP>" in out and "<EMAIL>" in out


def test_scrub_keeps_track_titles_and_channel_urls():
    text = "DOWNLOADED  Artist - Track  https://www.youtube.com/@channel"
    out = support.scrub_text(text, home="/home/u", base_dir="/home/u/Music",
                             username="u")
    assert "Artist - Track" in out
    assert "https://www.youtube.com/@channel" in out


def test_scrub_handles_forward_and_back_slashes_the_same(tmp_path):
    out = support.scrub_text("C:/Users/djsin/Music/DJ-CrateBuilder/a and C:\\Users\\djsin\\b",
                             home="C:\\Users\\djsin",
                             base_dir="C:\\Users\\djsin\\Music\\DJ-CrateBuilder",
                             username="djsin")
    assert out == "<LIBRARY>/a and <HOME>\\b"


def test_scrub_library_inside_home_becomes_library_not_home_prefix():
    out = support.scrub_text(r"C:\Users\djsin\Music\DJ-CrateBuilder\YouTube\a.mp3",
                             home=r"C:\Users\djsin",
                             base_dir=r"C:\Users\djsin\Music\DJ-CrateBuilder",
                             username="djsin")
    assert out == r"<LIBRARY>\YouTube\a.mp3"


def test_scrub_paths_under_home_keep_their_tail():
    out = support.scrub_text(r"C:\Users\djsin\AppData\Roaming\x.json",
                             home=r"C:\Users\djsin",
                             base_dir=r"D:\Crates", username="djsin")
    assert out == r"<HOME>\AppData\Roaming\x.json"


def test_scrub_matches_windows_paths_case_insensitively():
    out = support.scrub_text(r"c:\users\DJSIN\music\dj-cratebuilder\a and DjSin said hi",
                             home=r"C:\Users\djsin",
                             base_dir=r"C:\Users\djsin\Music\DJ-CrateBuilder",
                             username="djsin")
    assert out == "<LIBRARY>\\a and <USER> said hi"


def test_scrub_ignores_a_trailing_separator_on_the_configured_paths():
    out = support.scrub_text(r"C:\Users\djsin\Music\DJ-CrateBuilder\a",
                             home="C:\\Users\\djsin\\",
                             base_dir="C:\\Users\\djsin\\Music\\DJ-CrateBuilder\\",
                             username="djsin")
    assert out == r"<LIBRARY>\a"


def test_scrub_replaces_username_only_as_a_whole_word():
    out = support.scrub_text("djsinger and dj-djsin and (djsin) and djsin.",
                             home="/home/djsin", base_dir="/home/djsin/Music",
                             username="djsin")
    assert out == "djsinger and dj-djsin and (<USER>) and <USER>."


def test_scrub_escapes_regex_characters_in_the_username_and_paths():
    out = support.scrub_text("user dj.sin at C:\\Users\\dj.sin (x)\\y",
                             home="C:\\Users\\dj.sin (x)", base_dir="D:\\Crates",
                             username="dj.sin")
    assert out == "user <USER> at <HOME>\\y"
    assert support.scrub_text("djXsin", home="/h", base_dir="/h/m",
                              username="dj.sin") == "djXsin"


def test_scrub_redacts_tokens_in_urls_and_leaves_already_redacted_alone():
    text = "GET /ws?token=abc-123&x=1 token=<redacted> token='q'"
    out = support.scrub_text(text, home="/home/u", base_dir="/home/u/Music",
                             username="u")
    assert out == "GET /ws?token=<redacted>&x=1 token=<redacted> token='q'"
    assert support.scrub_text(out, home="/home/u", base_dir="/home/u/Music",
                              username="u") == out


def test_scrub_hides_ip_addresses_inside_urls_and_ports():
    out = support.scrub_text("remote http://192.168.1.20:8765/ from 10.0.0.7",
                             home="/home/u", base_dir="/home/u/Music", username="u")
    assert out == "remote http://<IP>:8765/ from <IP>"


def test_scrub_hides_emails_but_not_youtube_handles():
    out = support.scrub_text("dj.sintax+x@gmail.com https://youtube.com/@dj.handle",
                             home="/home/u", base_dir="/home/u/Music", username="u")
    assert out == "<EMAIL> https://youtube.com/@dj.handle"


def test_scrub_hides_an_email_whose_local_part_is_the_username():
    out = support.scrub_text("login djsin@sintax-music.com",
                             home="/home/djsin", base_dir="/home/djsin/Music",
                             username="djsin")
    assert out == "login <EMAIL>"
    assert "sintax-music" not in out


def test_scrub_matches_repr_escaped_paths_with_doubled_backslashes():
    line = ("WL FOLDER-POPULATE | 'C:\\\\Users\\\\djsin\\\\Music\\\\DJ-CrateBuilder"
            "\\\\YouTube\\\\House' | {\"p\": \"C:\\\\Users\\\\djsin\\\\AppData\"}")
    out = support.scrub_text(line, home="C:\\Users\\djsin",
                             base_dir="C:\\Users\\djsin\\Music\\DJ-CrateBuilder",
                             username="DJ Sintax")
    assert out == ("WL FOLDER-POPULATE | '<LIBRARY>\\\\YouTube\\\\House' | "
                   "{\"p\": \"<HOME>\\\\AppData\"}")
    assert "djsin" not in out


def test_scrub_matches_url_encoded_paths():
    out = support.scrub_text("C%3A%5CUsers%5Cdjsin%5CMusic and C%3A%5CUsers%5Cdjsin%5C"
                             "Music%5CDJ-CrateBuilder%5Ca.mp3",
                             home="C:\\Users\\djsin",
                             base_dir="C:\\Users\\djsin\\Music\\DJ-CrateBuilder",
                             username="DJ Sintax")
    assert out == "<HOME>%5CMusic and <LIBRARY>%5Ca.mp3"
    out = support.scrub_text("%2Fhome%2Fu%2FMusic%2Fa and /home/u/Music/b",
                             home="/home/u", base_dir="/home/u/Music", username="u")
    assert out == "<LIBRARY>%2Fa and <LIBRARY>/b"


def test_scrub_hides_the_home_folder_name_when_it_differs_from_the_username():
    out = support.scrub_text("folder djsin owned by DJ Sintax; djsinger stays",
                             home="C:\\Users\\djsin\\", base_dir="D:\\Crates",
                             username="DJ Sintax")
    assert out == "folder <USER> owned by <USER>; djsinger stays"


def test_scrub_hides_the_username_between_url_encoded_separators():
    out = support.scrub_text("D%3A%5Cdjsin%5Cstuff and %2Fdjsin%2F",
                             home="C:\\Users\\other", base_dir="D:\\Crates",
                             username="djsin")
    assert out == "D%3A%5C<USER>%5Cstuff and %2F<USER>%2F"


def test_scrub_tolerates_empty_inputs():
    assert support.scrub_text("", home="/home/u", base_dir="/home/u/Music",
                              username="u") == ""
    assert support.scrub_text(None, home="/home/u", base_dir="/home/u/Music",
                              username="u") == ""
    assert support.scrub_text("hello u", home="", base_dir=" ", username="",
                              cookie_file="") == "hello u"
    assert support.scrub_text("hello", home=None, base_dir=None, username=None) == "hello"


def test_tail_bytes_starts_on_a_line_boundary(tmp_path):
    p = tmp_path / "a.log"
    p.write_text("line1\nline2\nline3\n", encoding="utf-8", newline="\n")
    assert support.tail_bytes(str(p), limit=9) == "line3\n"
    assert support.tail_bytes(str(tmp_path / "missing.log")) == ""


def test_tail_bytes_returns_the_whole_file_when_it_fits(tmp_path):
    p = tmp_path / "a.log"
    p.write_text("line1\nline2\n", encoding="utf-8", newline="\n")
    assert support.tail_bytes(str(p)) == "line1\nline2\n"
    assert support.tail_bytes(str(p), limit=12) == "line1\nline2\n"


def test_tail_bytes_keeps_a_partial_tail_when_it_has_no_newline(tmp_path):
    p = tmp_path / "a.log"
    p.write_text("abcdefghij", encoding="utf-8", newline="\n")
    assert support.tail_bytes(str(p), limit=4) == "ghij"


def test_tail_bytes_replaces_undecodable_bytes(tmp_path):
    p = tmp_path / "a.log"
    p.write_bytes(b"ok\n\xff\xfe bad\n")
    out = support.tail_bytes(str(p))
    assert out.startswith("ok\n") and out.endswith(" bad\n")
    assert "\ufffd" in out


def test_tail_bytes_default_limit_is_half_a_megabyte():
    assert support.TAIL_BYTES == 512 * 1024


def test_system_block_lists_app_os_python_and_window():
    out = support.system_block(app_version="2.0", app_build=88,
                               platform="Windows-11", python="3.12.1",
                               transport="local")
    assert out == ("App: DJ-CrateBuilder 2.0 build 88\n"
                   "OS: Windows-11\nPython: 3.12.1\nWindow: local\n")


def test_issue_url_encodes_and_truncates():
    url = support.issue_url("https://github.com/x/y/issues/new", "Crash on scan",
                            "a" * 10000)
    assert url.startswith("https://github.com/x/y/issues/new?title=Crash+on+scan&body=")
    assert len(url) < 7000
    assert "log+bundle+attached" in url


def test_issue_url_keeps_a_short_body_whole_and_appends_the_attachment_note():
    url = support.issue_url("https://github.com/x/y/issues/new", "T", "line1\nline2")
    q = parse_qs(urlsplit(url).query)
    assert q["title"] == ["T"]
    assert q["body"] == ["line1\nline2\n\n[log bundle attached]"]


def test_issue_url_tolerates_missing_title_and_body():
    q = parse_qs(urlsplit(support.issue_url("https://g/i/new", None, None)).query,
                 keep_blank_values=True)
    assert q["title"] == [""]
    assert q["body"] == ["\n\n[log bundle attached]"]


def test_build_bundle_writes_named_members(tmp_path):
    z = tmp_path / "r.zip"
    support.build_bundle(str(z), {"activity.log": "a\n", "debug.log": "b\n",
                                  "report.txt": "c"})
    with zipfile.ZipFile(z) as zf:
        assert sorted(zf.namelist()) == ["activity.log", "debug.log", "report.txt"]
        assert zf.read("debug.log") == b"b\n"


def test_build_bundle_deflates_and_writes_empty_members_for_missing_text(tmp_path):
    z = tmp_path / "r.zip"
    support.build_bundle(str(z), {"empty.log": None, "big.log": "x" * 10000})
    with zipfile.ZipFile(z) as zf:
        assert zf.read("empty.log") == b""
        info = zf.getinfo("big.log")
        assert info.compress_type == zipfile.ZIP_DEFLATED
        assert info.compress_size < info.file_size
