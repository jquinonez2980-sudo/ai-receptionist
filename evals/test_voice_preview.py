"""POST /platform/voice/preview — Voice Studio preview endpoint
(docs/ESMI_DASHBOARD_UX.md Section 3.5 / 4 / 12.2).

No live ElevenLabs or R2 calls here: `_synthesize` (the one function that
makes the outbound HTTP call) is monkeypatched, and R2 is replaced with an
in-memory fake client the same way test_knowledge_db.py fakes
platform_api.recordings — via a fake module object injected into
sys.modules, since platform_api.voice_preview imports `_bucket`, `_get_client`,
`r2_configured` lazily inside the route (late import, so the substitution is
visible at call time).

What matters here:
  1. Validation (speed range, language, empty text) 400s before anything else
     runs — no R2/ElevenLabs call on a bad request.
  2. An unmapped voice_id (voice_library.VOICE_LIBRARY has no entry) is a 503
     that names the id, never a guess.
  3. A cache miss synthesizes once and uploads to R2; a cache hit (object
     already in R2) never calls ElevenLabs again.
  4. The cache key changes whenever any of tenant/voice/speed/language/text
     changes, and stays identical when none of them do.

Run: PYTHONUTF8=1 pytest evals/test_voice_preview.py -v
"""

import sys
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import platform_api.voice_preview as vp
import voice_library
from evals.test_signup import FakeConn, FakeEngine

SECRET = "test-platform-secret"
TID = "otro-nivel"
HEADERS = {"X-Platform-Secret": SECRET, "X-Tenant-Id": TID}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("PLATFORM_API_SECRET", SECRET)
    app = FastAPI()
    app.include_router(vp.router)
    return TestClient(app)


class FakeS3:
    """In-memory stand-in for the boto3 R2 client — head/put/presign only."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.put_calls = 0
        self.presign_calls = 0

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise Exception("NoSuchKey")
        return {"ContentLength": len(self.objects[Key])}

    def put_object(self, Bucket, Key, Body, ContentType):
        self.objects[Key] = Body
        self.put_calls += 1

    def generate_presigned_url(self, op, Params, ExpiresIn):
        self.presign_calls += 1
        return f"https://r2.example/{Params['Key']}?ttl={ExpiresIn}"


@pytest.fixture
def fake_s3(monkeypatch):
    fake_client = FakeS3()
    fake_module = types.SimpleNamespace(
        r2_configured=lambda: True,
        _bucket=lambda: "bkt",
        _get_client=lambda: fake_client,
    )
    monkeypatch.setitem(sys.modules, "platform_api.recordings", fake_module)
    return fake_client


@pytest.fixture(autouse=True)
def _elevenlabs_key(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")


@pytest.fixture(autouse=True)
def _voice_catalog(monkeypatch):
    """A known mapping so happy-path tests don't depend on the real
    VOICE_LIBRARY's actual contents (which now also carries the live
    "esmi-default" mapping — see test_resolve_voice_id_esmi_default_is_mapped
    below for coverage of that real entry)."""
    monkeypatch.setitem(voice_library.VOICE_LIBRARY, "sofia", "el_real_voice_id")
    yield
    voice_library.VOICE_LIBRARY.pop("sofia", None)


def body(**over):
    b = {"voice_id": "sofia", "speed": 1.0, "language": "en", "text": "Hello there."}
    b.update(over)
    return b


# ── validation happens before any R2/ElevenLabs call ─────────────────────────


@pytest.mark.parametrize("bad_speed", [0.5, 0.84, 1.16, 2.0, -1.0])
def test_out_of_range_speed_is_a_400(client, fake_s3, bad_speed):
    r = client.post("/platform/voice/preview", json=body(speed=bad_speed), headers=HEADERS)
    assert r.status_code == 400
    assert "speed" in r.json()["detail"]
    assert fake_s3.put_calls == 0


@pytest.mark.parametrize("bad_lang", ["english", "fr", "ES-mx"])
def test_invalid_language_is_a_400(client, fake_s3, bad_lang):
    r = client.post("/platform/voice/preview", json=body(language=bad_lang), headers=HEADERS)
    assert r.status_code == 400
    assert "language" in r.json()["detail"]
    assert fake_s3.put_calls == 0


def test_empty_text_is_rejected(client, fake_s3):
    r = client.post("/platform/voice/preview", json=body(text="   "), headers=HEADERS)
    assert r.status_code == 400
    assert fake_s3.put_calls == 0


def test_overlong_text_is_rejected(client, fake_s3):
    r = client.post(
        "/platform/voice/preview",
        json=body(text="x" * (vp._MAX_PREVIEW_TEXT_LEN + 1)),
        headers=HEADERS,
    )
    assert r.status_code == 422  # pydantic Field(max_length=...)
    assert fake_s3.put_calls == 0


def test_routes_require_the_platform_secret(client, fake_s3):
    r = client.post("/platform/voice/preview", json=body(), headers={"X-Tenant-Id": TID})
    assert r.status_code == 401
    assert fake_s3.put_calls == 0


def test_routes_require_a_tenant(client, fake_s3):
    r = client.post("/platform/voice/preview", json=body(), headers={"X-Platform-Secret": SECRET})
    assert r.status_code == 400
    assert fake_s3.put_calls == 0


# ── unmapped voice refuses to guess ──────────────────────────────────────────


def test_unmapped_voice_id_is_a_503_not_a_guess(client, fake_s3):
    r = client.post("/platform/voice/preview", json=body(voice_id="not-a-real-voice"), headers=HEADERS)
    assert r.status_code == 503
    assert "not-a-real-voice" in r.json()["detail"]
    assert fake_s3.put_calls == 0


def test_empty_voice_library_refuses_every_voice(client, fake_s3, monkeypatch):
    monkeypatch.setattr(voice_library, "VOICE_LIBRARY", {})
    r = client.post("/platform/voice/preview", json=body(), headers=HEADERS)
    assert r.status_code == 503


def test_r2_not_configured_is_a_503(client, monkeypatch):
    fake_module = types.SimpleNamespace(
        r2_configured=lambda: False, _bucket=lambda: "bkt", _get_client=lambda: None
    )
    monkeypatch.setitem(sys.modules, "platform_api.recordings", fake_module)
    r = client.post("/platform/voice/preview", json=body(), headers=HEADERS)
    assert r.status_code == 503


def test_missing_elevenlabs_key_is_a_503(client, fake_s3, monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.delenv("ELEVENLABS_API_KEY_B64", raising=False)
    r = client.post("/platform/voice/preview", json=body(), headers=HEADERS)
    assert r.status_code == 503


# ── cache miss synthesizes; cache hit does not ───────────────────────────────


def test_cache_miss_synthesizes_and_uploads(client, fake_s3, monkeypatch):
    calls = []

    def fake_synthesize(elevenlabs_voice_id, api_key, speed, text):
        calls.append((elevenlabs_voice_id, api_key, speed, text))
        return b"\x00" * 16_000  # 1 second at 128kbps

    monkeypatch.setattr(vp, "_synthesize", fake_synthesize)

    r = client.post("/platform/voice/preview", json=body(), headers=HEADERS)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["url"].startswith("https://r2.example/")
    assert out["duration_sec"] == pytest.approx(1.0, abs=0.01)
    assert "cache_key" in out and len(out["cache_key"]) == 64  # sha256 hexdigest

    assert len(calls) == 1
    assert calls[0][0] == "el_real_voice_id"  # resolved through VOICE_LIBRARY
    assert calls[0][1] == "test-key"
    assert fake_s3.put_calls == 1


def test_cache_hit_never_calls_elevenlabs(client, fake_s3, monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("must not synthesize on a cache hit")

    monkeypatch.setattr(vp, "_synthesize", boom)

    key = vp._object_key(TID, vp._cache_key(TID, "sofia", 1.0, "en", "Hello there."))
    fake_s3.objects[key] = b"\x00" * 8000

    r = client.post("/platform/voice/preview", json=body(), headers=HEADERS)
    assert r.status_code == 200, r.text
    assert fake_s3.put_calls == 0
    assert fake_s3.presign_calls == 1


def test_synthesis_failure_is_a_502(client, fake_s3, monkeypatch):
    def fake_synthesize(*a, **kw):
        raise RuntimeError("elevenlabs 500")

    monkeypatch.setattr(vp, "_synthesize", fake_synthesize)
    r = client.post("/platform/voice/preview", json=body(), headers=HEADERS)
    assert r.status_code == 502
    assert fake_s3.put_calls == 0


# ── cache key is content-addressed ───────────────────────────────────────────


@pytest.mark.parametrize(
    "changed",
    [
        {"voice_id": "other"},
        {"speed": 1.05},
        {"language": "es"},
        {"text": "Different text."},
    ],
)
def test_cache_key_changes_when_an_input_changes(changed):
    base = vp._cache_key(TID, "sofia", 1.0, "en", "Hello there.")
    over = {"tenant_id": TID, "voice_id": "sofia", "speed": 1.0, "language": "en", "text": "Hello there."}
    over.update(changed)
    other = vp._cache_key(
        over["tenant_id"], over["voice_id"], over["speed"], over["language"], over["text"]
    )
    assert base != other


def test_cache_key_is_stable_for_identical_input():
    a = vp._cache_key(TID, "sofia", 1.0, "en", "Hello there.")
    b = vp._cache_key(TID, "sofia", 1.0, "en", "Hello there.")
    assert a == b


def test_cache_key_is_tenant_scoped():
    """Two tenants previewing the same voice/text must never share a cached
    object — see docs/ESMI_DASHBOARD_UX.md Section 12.3 (never global)."""
    a = vp._cache_key("otro-nivel", "sofia", 1.0, "en", "Hello there.")
    b = vp._cache_key("coastline-condos", "sofia", 1.0, "en", "Hello there.")
    assert a != b


# ── duration estimate ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "num_bytes,expected",
    [(16_000, 1.0), (8_000, 0.5), (32_000, 2.0)],
)
def test_estimate_duration_matches_fixed_bitrate(num_bytes, expected):
    assert vp._estimate_duration_sec(num_bytes) == pytest.approx(expected, abs=0.01)


# ── voice_library.resolve_voice_id ────────────────────────────────────────────


def test_resolve_voice_id_is_case_insensitive(monkeypatch):
    monkeypatch.setitem(voice_library.VOICE_LIBRARY, "sofia", "abc123")
    assert voice_library.resolve_voice_id("SOFIA") == "abc123"
    assert voice_library.resolve_voice_id(" sofia ") == "abc123"


def test_resolve_voice_id_none_when_unmapped(monkeypatch):
    monkeypatch.setattr(voice_library, "VOICE_LIBRARY", {})
    assert voice_library.resolve_voice_id("sofia") is None


def test_resolve_voice_id_esmi_default_is_mapped():
    """The one real, live-confirmed mapping (railway run
    scripts/sync_vapi_voice.py --show-current) — checked against the actual
    module-level VOICE_LIBRARY, not a monkeypatched stand-in, so this fails
    loudly if the real catalog entry is ever edited or removed by mistake."""
    assert voice_library.resolve_voice_id("esmi-default") == "hpp4J3VqNfWAUOO0d1Us"
    assert voice_library.resolve_voice_id("ESMI-DEFAULT") == "hpp4J3VqNfWAUOO0d1Us"


@pytest.mark.parametrize(
    "short_id,elevenlabs_id",
    [
        ("ava", "EXAVITQu4vr4xnSDxMaL"),  # ElevenLabs "Sarah"
        ("noah", "pNInz6obpgDQGcFmaJgB"),  # ElevenLabs "Adam"
    ],
)
def test_resolve_voice_id_popular_catalog_entries_are_mapped(short_id, elevenlabs_id):
    """Two of the three popular-voice additions, checked against the real
    (unpatched) VOICE_LIBRARY — same treatment as esmi-default above, so an
    accidental edit/removal of one of these fails loudly here too. "sofia" is
    covered separately below — the autouse _voice_catalog fixture overrides
    that one key for every test in this file."""
    assert voice_library.resolve_voice_id(short_id) == elevenlabs_id
    assert voice_library.resolve_voice_id(short_id.upper()) == elevenlabs_id


def test_resolve_voice_id_sofia_is_mapped(monkeypatch):
    """"sofia" needs its own test: the autouse _voice_catalog fixture above
    overrides VOICE_LIBRARY["sofia"] with a fake id for every test in this
    file (so happy-path preview tests don't depend on the real catalog's
    contents) — undo that override here to check the real mapping."""
    monkeypatch.setitem(voice_library.VOICE_LIBRARY, "sofia", "21m00Tcm4TlvDq8ikWAM")
    assert voice_library.resolve_voice_id("sofia") == "21m00Tcm4TlvDq8ikWAM"
    assert voice_library.resolve_voice_id("SOFIA") == "21m00Tcm4TlvDq8ikWAM"


def test_resolve_voice_id_still_refuses_other_real_catalog_lookups():
    """Adding "esmi-default" must not make every other short id resolve —
    the real (unpatched) catalog still refuses to guess for anything else.
    (Doesn't check "sofia" here — the autouse _voice_catalog fixture above
    temporarily injects that key into the real dict for every test.)"""
    assert voice_library.resolve_voice_id("totally-unknown-voice") is None


# ── onboarding voice gate: onboarding_voice_previewed_at ─────────────────────
# docs/ESMI_DASHBOARD_UX.md Section 7 Step 3 — "must preview once" gate.


def use_db(monkeypatch, conn=None):
    conn = conn or FakeConn()
    monkeypatch.setattr("platform_db.get_engine", lambda: FakeEngine(conn))
    return conn


def test_successful_preview_marks_onboarding_voice_previewed(client, fake_s3, monkeypatch):
    monkeypatch.setattr(vp, "_synthesize", lambda *a, **kw: b"\x00" * 16_000)
    conn = use_db(monkeypatch)

    r = client.post("/platform/voice/preview", json=body(), headers=HEADERS)
    assert r.status_code == 200, r.text

    writes = conn.sql_containing("onboarding_voice_previewed_at")
    assert len(writes) == 1
    sql, params = writes[0]
    assert "INSERT INTO tenants" in sql
    assert "ON CONFLICT" in sql
    assert params == {"id": TID}


def test_cache_hit_also_marks_onboarding_voice_previewed(client, fake_s3, monkeypatch):
    """A cache hit is still a successful preview response — the gate cares
    about "did they hear it", not "did we call ElevenLabs this time"."""
    key = vp._object_key(TID, vp._cache_key(TID, "sofia", 1.0, "en", "Hello there."))
    fake_s3.objects[key] = b"\x00" * 8000
    conn = use_db(monkeypatch)

    r = client.post("/platform/voice/preview", json=body(), headers=HEADERS)
    assert r.status_code == 200, r.text
    assert len(conn.sql_containing("onboarding_voice_previewed_at")) == 1


@pytest.mark.parametrize(
    "override,expected_status",
    [
        ({"speed": 5.0}, 400),
        ({"language": "fr"}, 400),
        ({"text": "   "}, 400),
        ({"voice_id": "not-a-real-voice"}, 503),
    ],
)
def test_failed_preview_never_marks_onboarding_voice_previewed(
    client, fake_s3, monkeypatch, override, expected_status
):
    conn = use_db(monkeypatch)
    r = client.post("/platform/voice/preview", json=body(**override), headers=HEADERS)
    assert r.status_code == expected_status
    assert conn.sql_containing("onboarding_voice_previewed_at") == []


def test_synthesis_failure_never_marks_onboarding_voice_previewed(client, fake_s3, monkeypatch):
    monkeypatch.setattr(vp, "_synthesize", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    conn = use_db(monkeypatch)
    r = client.post("/platform/voice/preview", json=body(), headers=HEADERS)
    assert r.status_code == 502
    assert conn.sql_containing("onboarding_voice_previewed_at") == []


def test_default_tenant_is_never_marked(client, fake_s3, monkeypatch):
    """tenant_id 'default' (Orchelix) has no self-serve onboarding — the flag
    write must be skipped outright, not attempted and swallowed."""
    monkeypatch.setattr(vp, "_synthesize", lambda *a, **kw: b"\x00" * 16_000)
    conn = use_db(monkeypatch)
    headers = {"X-Platform-Secret": SECRET, "X-Tenant-Id": "default"}

    r = client.post("/platform/voice/preview", json=body(), headers=headers)
    assert r.status_code == 200, r.text
    assert conn.sql_containing("onboarding_voice_previewed_at") == []


def test_flag_write_failure_does_not_break_a_successful_preview(client, fake_s3, monkeypatch):
    """The preview itself already fully succeeded (audio synthesized/cached,
    URL presigned) by the time this best-effort write runs — a DB hiccup here
    must not turn that into an error response."""
    monkeypatch.setattr(vp, "_synthesize", lambda *a, **kw: b"\x00" * 16_000)

    class BoomEngine:
        def begin(self):
            raise RuntimeError("db unreachable")

    monkeypatch.setattr("platform_db.get_engine", lambda: BoomEngine())

    r = client.post("/platform/voice/preview", json=body(), headers=HEADERS)
    assert r.status_code == 200, r.text
    assert r.json()["url"]
