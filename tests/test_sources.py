"""Tests for the network layer and the data-source connectors."""

from __future__ import annotations

import io
import subprocess
from pathlib import Path

import pytest

from qslcard.adif import qso_from_fields
from qslcard.config import StationConfig
from qslcard.net import HttpResponse, ResponseCache, TransportError, UrllibTransport
from qslcard.privacy import EgressDenied, EgressPolicy
from qslcard.sources import available_sources, get_source
from qslcard.sources.adif_file import AdifFileSource, collect_adif_paths
from qslcard.sources.base import CallbookRecord, SourceContext, apply_record
from qslcard.sources.clublog import ClubLogSource, read_oqrs_csv
from qslcard.sources.eqsl import EqslSource
from qslcard.sources.hamqth import HamQthSource, parse_hamqth_search
from qslcard.sources.lotw import LotwSource, find_tqsl, run_tqsl
from qslcard.sources.qrz import QrzSource, parse_logbook_response, parse_qrz_callsign


class FakeTransport:
    """Records requests and replays canned responses or exceptions."""

    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def request(self, method, url, *, data=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "data": data})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


QRZ_CALLSIGN_XML = """<?xml version="1.0" encoding="utf-8"?>
<QRZDatabase version="1.34">
  <Session><Key>abc123</Key></Session>
  <Callsign>
    <call>JA1ABC</call><fname>Taro</fname><name>Yamada</name>
    <addr1>1-2-3 Chiyoda</addr1><addr2>Tokyo</addr2><state>Tokyo</state>
    <zip>100-0001</zip><country>Japan</country><grid>PM95</grid>
    <cqzone>25</cqzone><ituzone>45</ituzone><email>ja1abc@example.org</email>
    <qslmgr>Bureau</qslmgr><class>Extra</class>
  </Callsign>
</QRZDatabase>"""

QRZ_SESSION_OK = (
    """<?xml version="1.0"?><QRZDatabase><Session><Key>abc123</Key></Session></QRZDatabase>"""
)
QRZ_SESSION_FAIL = (
    '<?xml version="1.0"?><QRZDatabase><Session>'
    "<Error>Invalid user name/password</Error></Session></QRZDatabase>"
)

HAMQTH_XML = """<?xml version="1.0"?>
<HamQTH version="1.0"><session><session_id>sess42</session_id></session>
<search><callsign>VK2XY</callsign><nick>Bruce</nick><qth>Sydney</qth>
<country>Australia</country><grid>QF56</grid><adr_street1>1 Harbour St</adr_street1>
<adr_city>Sydney</adr_city><adr_zip>2000</adr_zip><us_state></us_state><cq>30</cq><itu>59</itu>
</search></HamQTH>"""


def rec(**fields: str):
    return qso_from_fields(fields)


def ctx(**kwargs) -> SourceContext:
    base = {"credentials": {}, "options": {}}
    base.update(kwargs)
    return SourceContext(**base)  # type: ignore[arg-type]


# -- registry and ADIF source ------------------------------------------------


def test_registry_lists_every_connector() -> None:
    assert available_sources() == ["adif", "clublog", "eqsl", "hamqth", "lotw", "qrz"]
    assert get_source("qrz").name == "qrz"
    with pytest.raises(KeyError):
        get_source("nope")


def test_adif_source_collects_files_and_yields_qsos(tmp_path) -> None:
    (tmp_path / "a.adi").write_text(
        "<CALL:5>JA1AA<QSO_DATE:8>20260101<TIME_ON:4>1200<BAND:3>20m<MODE:3>SSB<EOR>",
        encoding="utf-8",
    )
    (tmp_path / "notes.txt").write_text("ignore me", encoding="utf-8")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "b.adx").write_text(
        '<?xml version="1.0"?><ADX><RECORDS><RECORD><CALL>VK2XY</CALL>'
        "<QSO_DATE>20260102</QSO_DATE><TIME_ON>1300</TIME_ON></RECORD></RECORDS></ADX>",
        encoding="utf-8",
    )
    paths = collect_adif_paths([str(tmp_path)])
    assert len(paths) == 2
    source = AdifFileSource()
    context = ctx(options={"paths": [str(tmp_path)]})
    assert source.is_configured(context)
    qsos = list(source.fetch(context))
    assert {q.call for q in qsos} == {"JA1AA", "VK2XY"}
    assert all(q.source == "adif" for q in qsos)
    assert "2 ADIF file(s)" in source.test(context)


# -- QRZ --------------------------------------------------------------------


def test_parse_qrz_callsign() -> None:
    record = parse_qrz_callsign(QRZ_CALLSIGN_XML, "ja1abc")
    assert record.call == "JA1ABC"
    assert record.name == "Taro, Yamada"
    assert record.country == "Japan"
    assert record.gridsquare == "PM95"
    assert record.cq_zone == "25"
    assert "1-2-3 Chiyoda" in record.address
    assert record.qsl_via == "Bureau"
    assert record.source == "qrz"


def test_qrz_lookup_uses_login_then_cache() -> None:
    transport = FakeTransport(
        [
            HttpResponse(200, QRZ_SESSION_OK),
            HttpResponse(200, QRZ_CALLSIGN_XML),
        ]
    )
    cache = ResponseCache(str(Path.cwd() / ".test-cache-qrz"), ttl_seconds=60)
    context = ctx(
        transport=transport,
        cache=cache,
        credentials={"qrz_username": "u", "qrz_password": "p"},
    )
    source = QrzSource()
    assert source.is_configured(context)
    first = source.lookup(["JA1ABC"], context)
    assert first[0].gridsquare == "PM95"
    assert len(transport.calls) == 2  # login plus one lookup

    second = source.lookup(["JA1ABC"], context)
    assert second[0].call == "JA1ABC"
    assert len(transport.calls) == 2  # served from cache
    cache.clear()
    Path(cache.directory).rmdir()


def test_qrz_login_error_is_reported() -> None:
    transport = FakeTransport([HttpResponse(200, QRZ_SESSION_FAIL)])
    context = ctx(transport=transport, credentials={"qrz_username": "u", "qrz_password": "bad"})
    with pytest.raises(TransportError, match="login failed"):
        QrzSource().lookup(["JA1ABC"], context)


def test_qrz_logbook_fetch_and_error() -> None:
    adif = "<CALL:5>JA1AA<QSO_DATE:8>20260101<TIME_ON:4>1200<BAND:3>20m<MODE:3>SSB<EOR>"
    import urllib.parse

    ok_body = urllib.parse.urlencode({"RESULT": "OK", "ADIF": adif})
    transport = FakeTransport([HttpResponse(200, ok_body)])
    context = ctx(transport=transport, credentials={"qrz_logbook_key": "key"})
    qsos = list(QrzSource().fetch(context))
    assert len(qsos) == 1
    assert qsos[0].call == "JA1AA"
    assert qsos[0].source == "qrz"
    assert transport.calls[0]["data"]["ACTION"] == "FETCH"

    bad = FakeTransport([HttpResponse(200, "RESULT=FAIL&REASON=Invalid+key")])
    with pytest.raises(TransportError, match="logbook fetch failed"):
        list(QrzSource().fetch(ctx(transport=bad, credentials={"qrz_logbook_key": "key"})))


def test_parse_logbook_response_splits_fields_and_adif() -> None:
    fields, adif = parse_logbook_response("RESULT=OK&ADIF=%3CEOR%3E&COUNT=1")
    assert fields["RESULT"] == "OK"
    assert fields["COUNT"] == "1"
    assert adif == "<EOR>"


# -- HamQTH -----------------------------------------------------------------


def test_hamqth_parse_and_lookup() -> None:
    record = parse_hamqth_search(HAMQTH_XML)
    assert record.call == "VK2XY"
    assert record.name == "Bruce"
    assert record.country == "Australia"
    assert record.gridsquare == "QF56"
    assert record.source == "hamqth"

    login = (
        '<?xml version="1.0"?><HamQTH><session><session_id>sess42</session_id></session></HamQTH>'
    )
    transport = FakeTransport([HttpResponse(200, login), HttpResponse(200, HAMQTH_XML)])
    context = ctx(transport=transport, credentials={"hamqth_username": "u", "hamqth_password": "p"})
    source = HamQthSource()
    assert source.is_configured(context)
    records = source.lookup(["VK2XY"], context)
    assert records[0].call == "VK2XY"
    assert "HamQTH session" in source.test(context)


def test_hamqth_login_error() -> None:
    login = (
        '<?xml version="1.0"?><HamQTH><session>'
        "<error>Wrong user name or password</error></session></HamQTH>"
    )
    transport = FakeTransport([HttpResponse(200, login)])
    context = ctx(
        transport=transport, credentials={"hamqth_username": "u", "hamqth_password": "bad"}
    )
    with pytest.raises(TransportError, match="login failed"):
        HamQthSource().lookup(["VK2XY"], context)


# -- LoTW / TQSL ------------------------------------------------------------


def test_run_tqsl_passes_password_via_environment_only() -> None:
    seen: dict = {}

    def fake_runner(argv, env=None, **kwargs):
        seen["argv"] = argv
        seen["env"] = env
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    result = run_tqsl(["tqsl", "-d"], password="s3cr3t", runner=fake_runner)
    assert result.returncode == 0
    assert "s3cr3t" not in " ".join(seen["argv"])
    assert seen["env"]["TQSL_PASSWORD"] == "s3cr3t"


def test_run_tqsl_refuses_password_on_the_command_line() -> None:
    with pytest.raises(ValueError, match="command line"):
        run_tqsl(["tqsl", "-p", "s3cr3t"], password="s3cr3t", runner=lambda *a, **k: None)


def test_lotw_download_builds_command_and_reports_failure(tmp_path) -> None:
    calls: list = []

    def ok_runner(argv, env=None, **kwargs):
        calls.append((argv, (env or {}).get("TQSL_PASSWORD")))
        return subprocess.CompletedProcess(argv, 0)

    source = LotwSource(tqsl_path=str(tmp_path / "tqsl.exe"))
    (tmp_path / "tqsl.exe").write_text("stub", encoding="utf-8")
    context = ctx(
        credentials={"lotw_password": "pw"},
        options={"tqsl_path": str(tmp_path / "tqsl.exe")},
        runner=ok_runner,
    )
    target = tmp_path / "lotw.adi"
    result = source.download(context, str(target))
    assert result.returncode == 0
    assert calls[0][1] == "pw"
    assert str(target) in " ".join(calls[0][0])

    def fail_runner(argv, env=None, **kwargs):
        return subprocess.CompletedProcess(argv, 2, stdout="", stderr="bad certificate password")

    failing = ctx(
        credentials={"lotw_password": "pw"},
        options={"tqsl_path": str(tmp_path / "tqsl.exe")},
        runner=fail_runner,
    )
    with pytest.raises(TransportError, match="TQSL download failed"):
        source.download(failing, str(target))


def test_lotw_fetch_imports_report_and_marks_confirmations(tmp_path) -> None:
    report = tmp_path / "lotw.adi"
    report.write_text(
        "<CALL:5>JA1AA<QSO_DATE:8>20260101<TIME_ON:4>1200<BAND:3>20m<MODE:3>SSB<EOR>",
        encoding="utf-8",
    )
    source = LotwSource()
    qsos = list(source.fetch(ctx(options={"report_path": str(report)})))
    assert len(qsos) == 1
    assert qsos[0].source == "lotw"
    assert qsos[0].lotw_qsl_rcvd == "Y"


def test_lotw_missing_tqsl_gives_actionable_error() -> None:
    source = LotwSource(tqsl_path="Z:/definitely/missing/tqsl.exe")
    context = ctx(options={"tqsl_path": "Z:/definitely/missing/tqsl.exe"})
    assert source.is_configured(context) is False
    with pytest.raises(TransportError, match="TQSL was not found"):
        source.test(context)
    assert find_tqsl("Z:/definitely/missing/tqsl.exe") is None


# -- Club Log ---------------------------------------------------------------


def test_clublog_upload_and_403_stop() -> None:
    transport = FakeTransport([HttpResponse(200, "OK"), HttpResponse(403, "denied")])
    context = ctx(
        transport=transport,
        station=StationConfig(callsign="BG1XYZ"),
        credentials={"clublog_email": "a@b.c", "clublog_password": "pw", "clublog_api_key": "key"},
    )
    source = ClubLogSource()
    assert source.is_configured(context)
    qsos = [rec(CALL="JA1AA", QSO_DATE="20260101", TIME_ON="1200", BAND="20m", MODE="SSB")]
    assert source.upload(context, qsos) == 1
    assert transport.calls[0]["data"]["callsign"] == "BG1XYZ"
    assert "<EOR>" in transport.calls[0]["data"]["adif"]

    with pytest.raises(TransportError, match="403"):
        source.upload(context, qsos)


def test_clublog_403_exception_is_translated() -> None:
    transport = FakeTransport([TransportError("HTTP 403", status=403)])
    context = ctx(
        transport=transport,
        station=StationConfig(callsign="BG1XYZ"),
        credentials={"clublog_email": "a@b.c", "clublog_password": "pw", "clublog_api_key": "key"},
    )
    with pytest.raises(TransportError, match="Stop sending immediately"):
        ClubLogSource().upload(context, [rec(CALL="JA1AA", QSO_DATE="20260101", TIME_ON="1200")])


def test_clublog_oqrs_csv_becomes_cards(tmp_path) -> None:
    path = tmp_path / "oqrs.csv"
    path.write_text(
        "Callsign,Date,Band,Mode,Route,Name,City,Country\n"
        "JA1ABC,2026-09-11,20m,SSB,BURO,Taro,Tokyo,Japan\n",
        encoding="utf-8",
    )
    rows = read_oqrs_csv(str(path))
    assert rows[0]["callsign"] == "JA1ABC"
    qsos = list(ClubLogSource().fetch(ctx(options={"oqrs_path": str(path)})))
    assert len(qsos) == 1
    assert qsos[0].call == "JA1ABC"
    assert qsos[0].qso_date == "20260911"
    assert qsos[0].extra["OQRS"] == "Y"
    assert "Tokyo" in qsos[0].address


# -- eQSL -------------------------------------------------------------------


def test_eqsl_imports_report_and_refuses_remote_download(tmp_path) -> None:
    report = tmp_path / "eqsl.adi"
    report.write_text(
        "<CALL:5>JA1AA<QSO_DATE:8>20260101<TIME_ON:4>1200<BAND:3>20m<MODE:3>SSB"
        "<LOTW_QSL_RCVD:1>Y<EOR>",
        encoding="utf-8",
    )
    source = EqslSource()
    context = ctx(options={"report_path": str(report)})
    assert source.is_configured(context)
    qsos = list(source.fetch(context))
    assert qsos[0].source == "eqsl"
    assert qsos[0].eqsl_qsl_rcvd == "Y"
    with pytest.raises(TransportError, match="no general API"):
        source.download(context, str(tmp_path / "out.adi"))


# -- callbook application ---------------------------------------------------


def test_apply_record_fills_blanks_only_unless_overwriting() -> None:
    qso = rec(CALL="JA1ABC", QSO_DATE="20260101", TIME_ON="1200", NAME="Manual Name")
    record = CallbookRecord(call="JA1ABC", name="Taro", qth="Tokyo", address="1-2-3", source="qrz")
    assert apply_record(qso, record) is True
    assert qso.name == "Manual Name"  # manual correction preserved
    assert qso.qth == "Tokyo"
    assert qso.extra["CALLBOOK_SOURCE"] == "qrz"

    assert apply_record(qso, record, overwrite=True) is True
    assert qso.name == "Taro"


# -- transport and cache ----------------------------------------------------


def test_egress_policy_blocks_before_any_socket() -> None:
    def explode(*_args, **_kwargs):
        raise AssertionError("a socket was opened for a disallowed host")

    transport = UrllibTransport(EgressPolicy(), opener=explode)
    with pytest.raises(EgressDenied):
        transport.get("https://evil.example.com/steal")
    with pytest.raises(EgressDenied):
        transport.get("http://xmldata.qrz.com/insecure")


def test_response_cache_round_trip_and_ttl(tmp_path) -> None:
    cache = ResponseCache(str(tmp_path), ttl_seconds=60)
    assert cache.get("missing") is None
    cache.put("qrz:call:JA1ABC", "<xml/>")
    assert cache.get("qrz:call:JA1ABC") == "<xml/>"
    # ttl_seconds=0 expires immediately; a negative value means "never expire".
    expired = ResponseCache(str(tmp_path), ttl_seconds=0)
    assert expired.get("qrz:call:JA1ABC") is None
    forever = ResponseCache(str(tmp_path), ttl_seconds=-1)
    assert forever.get("qrz:call:JA1ABC") == "<xml/>"
    assert cache.clear() == 1


def test_response_body_decoding_is_reported_as_text() -> None:
    response = HttpResponse(200, io.StringIO("body").read())
    assert response.ok
    assert not HttpResponse(500, "boom").ok


# -- QSL status write-back and LoTW pull ------------------------------------


def test_qrz_push_qsl_status_posts_insert_with_qsl_fields() -> None:
    transport = FakeTransport(
        [HttpResponse(200, "RESULT=OK&LOGID=1"), HttpResponse(200, "RESULT=OK")]
    )
    context = ctx(transport=transport, credentials={"qrz_logbook_key": "key"})
    qsos = [
        rec(CALL="JA1AA", QSO_DATE="20260911", TIME_ON="1200", BAND="20m", MODE="SSB"),
        rec(CALL="VK2XY", QSO_DATE="20260911", TIME_ON="1230", BAND="40m", MODE="CW"),
    ]
    pushed = QrzSource().push_qsl_status(
        context, qsos, sent_date="2026-09-12", via="BURO", chunk_size=1
    )
    assert pushed == 2
    assert len(transport.calls) == 2  # chunk_size=1 splits the batch
    payload = transport.calls[0]["data"]
    assert payload["ACTION"] == "INSERT"
    assert payload["KEY"] == "key"
    assert "<QSL_SENT:1>Y" in payload["ADIF"]
    assert "<QSLSDATE:8>20260912" in payload["ADIF"]
    assert "<QSL_SENT_VIA:4>BURO" in payload["ADIF"]


def test_qrz_push_qsl_status_reports_rejection() -> None:
    transport = FakeTransport([HttpResponse(200, "RESULT=FAIL&REASON=Invalid+key")])
    context = ctx(transport=transport, credentials={"qrz_logbook_key": "key"})
    qsos = [rec(CALL="JA1AA", QSO_DATE="20260911", TIME_ON="1200", BAND="20m", MODE="SSB")]
    with pytest.raises(TransportError, match="rejected"):
        QrzSource().push_qsl_status(context, qsos, sent_date="20260912")


def test_qrz_push_qsl_status_marks_received() -> None:
    transport = FakeTransport([HttpResponse(200, "RESULT=OK")])
    context = ctx(transport=transport, credentials={"qrz_logbook_key": "key"})
    qsos = [rec(CALL="JA1AA", QSO_DATE="20260911", TIME_ON="1200", BAND="20m", MODE="SSB")]
    assert QrzSource().push_qsl_status(context, qsos, sent_date="20261005", received=True) == 1
    assert "<QSL_RCVD:1>Y" in transport.calls[0]["data"]["ADIF"]
    assert "<QSLRDATE:8>20261005" in transport.calls[0]["data"]["ADIF"]


def test_qrz_fetch_since_uses_modsince() -> None:
    import urllib.parse

    adif = "<CALL:5>JA1AA<QSO_DATE:8>20260911<TIME_ON:4>1200<BAND:3>20m<MODE:3>SSB<EOR>"
    body = urllib.parse.urlencode({"RESULT": "OK", "ADIF": adif})
    transport = FakeTransport([HttpResponse(200, body)])
    context = ctx(transport=transport, credentials={"qrz_logbook_key": "key"})
    qsos = list(QrzSource().fetch(context, since="2026-09-01"))
    assert len(qsos) == 1
    assert transport.calls[0]["data"]["OPTION"] == "MODSINCE:20260901"

    transport.calls.clear()
    transport.responses.append(HttpResponse(200, body))
    list(QrzSource().fetch(context))
    assert transport.calls[0]["data"]["OPTION"] == "ALL"


def test_qrz_push_needs_the_logbook_key() -> None:
    context = ctx(transport=FakeTransport([]), credentials={})
    qsos = [rec(CALL="JA1AA", QSO_DATE="20260911", TIME_ON="1200", BAND="20m", MODE="SSB")]
    with pytest.raises(TransportError, match="Logbook API key"):
        QrzSource().push_qsl_status(context, qsos, sent_date="20260912")


def test_lotw_sync_downloads_with_tqsl_and_parses(tmp_path) -> None:
    report = (
        "<CALL:5>JA1AA<QSO_DATE:8>20260911<TIME_ON:4>1200<BAND:3>20m<MODE:3>SSB"
        "<LOTW_QSL_RCVD:1>Y<EOR>"
    )
    calls: list = []

    def runner(argv, env=None, **kwargs):
        calls.append((argv, (env or {}).get("TQSL_PASSWORD")))
        target = Path(argv[argv.index("-o") + 1])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(report, encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0)

    stub = tmp_path / "tqsl.exe"
    stub.write_text("stub", encoding="utf-8")
    source = LotwSource(tqsl_path=str(stub))
    context = ctx(
        credentials={"lotw_password": "pw"},
        options={"tqsl_path": str(stub)},
        runner=runner,
    )
    qsos = source.sync(context, str(tmp_path / "work"))
    assert len(qsos) == 1
    assert qsos[0].source == "lotw"
    assert qsos[0].lotw_qsl_rcvd == "Y"
    assert calls[0][1] == "pw"  # password travelled by environment only


def test_lotw_sync_reports_a_missing_output_file(tmp_path) -> None:
    def runner(argv, env=None, **kwargs):
        return subprocess.CompletedProcess(argv, 0)  # claims success, writes nothing

    stub = tmp_path / "tqsl.exe"
    stub.write_text("stub", encoding="utf-8")
    source = LotwSource(tqsl_path=str(stub))
    context = ctx(
        credentials={"lotw_password": "pw"},
        options={"tqsl_path": str(stub)},
        runner=runner,
    )
    with pytest.raises(TransportError, match="wrote no ADIF"):
        source.sync(context, str(tmp_path / "work"))
