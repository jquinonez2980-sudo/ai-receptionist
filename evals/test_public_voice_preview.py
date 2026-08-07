"""platform_api/public_voice_preview.py — POST /platform/public/voice/preview,
the unauthenticated try-esmi voice preview (docs/ESMI_DASHBOARD_UX.md
Section 6).

No live ElevenLabs or R2 calls here: `_synthesize` is monkeypatched and R2
is replaced with an in-memory fake client (same pattern as
test_voice_preview.py's FakeS3). An autouse fixture resets the shared
rate-limit counter before every test — the `limiter` here is the SAME
module-level singleton (rate_limit.py) every test in this file (and every
other rate-limited route) shares, so without a reset, unrelated tests would
start failing once the file's total call count crosses 5/minute.

What matters here:
  1. Only a fixed sample_id (from public_voice_samples.PUBLIC_SAMPLES) is
     ever accepted — there is no `text` field on the request model at all,
     so free-form text can't reach ElevenLabs no matter what a caller sends.
  2. Unknown sample_id / bad language / non-public voice_id each 400 before
     any R2/ElevenLabs call.
  3. Cache miss synthesizes once and uploads; a cache hit never calls
     ElevenLabs again.
  4. The response echoes the resolved text and the required watermark
     caption.
  5. Cache keys and object keys are completely isolated from
     platform_api/voice_preview.py's tenant-scoped scheme — different
     prefix (public_voice_previews/ vs voice_previews/<tenant>/), different
     hash input (namespaced with "public:").
  6. Rate limit: the 6th call within a minute from the same key gets 429 —
     stricter than /chat's 10/minute, since a cache miss is real
     ElevenLabs cost with no tenant to attribute it to.
  7. No auth required at all (no X-Platform-Secret / X-Tenant-Id check —
     this is deliberately the one public platform_api route).

Run: PYTHONUTF8=1 pytest evals/test_public_voice_preview.py -v
"""

import sys
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import platform_api.public_voice_preview as pvp
import public_voice_samples as pvs

HEADERS = {}  # no auth headers at all — this endpoint is public


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(pvp.router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    pvp.limiter.reset()
    yield
    pvp.limiter.reset()


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


def body(**over):
    b = {"sample_id": "general", "language": "en"}
    b.update(over)
    return b


# ── fixed samples only: no free-form text is even representable ────────────


def test_request_model_has_no_text_field():
    assert "text" not in pvp.PublicVoicePreviewRequest.model_fields


def test_extra_text_field_in_the_request_body_is_ignored(client, fake_s3, monkeypatch):
    """Even if a caller sends a `text` field (the tenant endpoint's field
    name), Pydantic drops unknown fields by default — the fixed sample is
    used regardless."""
    calls = []

    def fake_synthesize(elevenlabs_voice_id, api_key, text):
        calls.append(text)
        return b"\x00" * 16_000

    monkeypatch.setattr(pvp, "_synthesize", fake_synthesize)

    r = client.post("/platform/public/voice/preview", json=body(text="ignore all instructions"))
    assert r.status_code == 200, r.text
    assert calls == [pvs.PUBLIC_SAMPLES["general"]["en"]]


# ── validation: fail before any R2/ElevenLabs call ──────────────────────────


def test_unknown_sample_id_is_a_400(client, fake_s3):
    r = client.post("/platform/public/voice/preview", json=body(sample_id="not-a-real-sample"))
    assert r.status_code == 400
    assert "not-a-real-sample" in r.json()["detail"]
    assert fake_s3.put_calls == 0


@pytest.mark.parametrize("bad_lang", ["fr", "auto", "ES-mx", ""])
def test_invalid_language_is_a_400(client, fake_s3, bad_lang):
    r = client.post("/platform/public/voice/preview", json=body(language=bad_lang))
    assert r.status_code == 400
    assert "language" in r.json()["detail"]
    assert fake_s3.put_calls == 0


def test_non_public_voice_id_is_a_400(client, fake_s3):
    """sofia might exist in the full tenant VOICE_LIBRARY some day — being
    mapped there does not make it public."""
    r = client.post("/platform/public/voice/preview", json=body(voice_id="sofia"))
    assert r.status_code == 400
    assert "sofia" in r.json()["detail"]
    assert fake_s3.put_calls == 0


def test_public_voice_ids_is_exactly_esmi_default():
    assert pvs.PUBLIC_VOICE_IDS == frozenset({"esmi-default"})


# ── no auth required ─────────────────────────────────────────────────────


def test_no_auth_headers_required(client, fake_s3, monkeypatch):
    monkeypatch.setattr(pvp, "_synthesize", lambda *a, **kw: b"\x00" * 16_000)
    r = client.post("/platform/public/voice/preview", json=body(), headers=HEADERS)
    assert r.status_code == 200, r.text


# ── cache miss synthesizes; cache hit does not; watermark always present ───


def test_cache_miss_synthesizes_and_uploads(client, fake_s3, monkeypatch):
    calls = []

    def fake_synthesize(elevenlabs_voice_id, api_key, text):
        calls.append((elevenlabs_voice_id, api_key, text))
        return b"\x00" * 16_000  # 1 second at 128kbps

    monkeypatch.setattr(pvp, "_synthesize", fake_synthesize)

    r = client.post("/platform/public/voice/preview", json=body())
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["url"].startswith("https://r2.example/")
    assert out["duration_sec"] == pytest.approx(1.0, abs=0.01)
    assert out["text"] == pvs.PUBLIC_SAMPLES["general"]["en"]
    assert out["watermark"] == pvp._WATERMARK

    assert len(calls) == 1
    assert calls[0][0] == "hpp4J3VqNfWAUOO0d1Us"  # esmi-default resolved through VOICE_LIBRARY
    assert calls[0][1] == "test-key"
    assert calls[0][2] == pvs.PUBLIC_SAMPLES["general"]["en"]
    assert fake_s3.put_calls == 1


def test_cache_hit_never_calls_elevenlabs(client, fake_s3, monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("must not synthesize on a cache hit")

    monkeypatch.setattr(pvp, "_synthesize", boom)

    text = pvs.PUBLIC_SAMPLES["general"]["en"]
    key = pvp._object_key(pvp._cache_key("esmi-default", "en", "general", text))
    fake_s3.objects[key] = b"\x00" * 8000

    r = client.post("/platform/public/voice/preview", json=body())
    assert r.status_code == 200, r.text
    assert fake_s3.put_calls == 0
    assert fake_s3.presign_calls == 1


def test_synthesis_failure_is_a_502(client, fake_s3, monkeypatch):
    def fake_synthesize(*a, **kw):
        raise RuntimeError("elevenlabs 500")

    monkeypatch.setattr(pvp, "_synthesize", fake_synthesize)
    r = client.post("/platform/public/voice/preview", json=body())
    assert r.status_code == 502
    assert fake_s3.put_calls == 0


def test_r2_not_configured_is_a_503(client, monkeypatch):
    fake_module = types.SimpleNamespace(
        r2_configured=lambda: False, _bucket=lambda: "bkt", _get_client=lambda: None
    )
    monkeypatch.setitem(sys.modules, "platform_api.recordings", fake_module)
    r = client.post("/platform/public/voice/preview", json=body())
    assert r.status_code == 503


def test_missing_elevenlabs_key_is_a_503(client, fake_s3, monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.delenv("ELEVENLABS_API_KEY_B64", raising=False)
    r = client.post("/platform/public/voice/preview", json=body())
    assert r.status_code == 503


# ── cache key / object key isolation from the tenant endpoint ──────────────


def test_object_key_prefix_never_collides_with_tenant_previews():
    text = pvs.PUBLIC_SAMPLES["general"]["en"]
    key = pvp._object_key(pvp._cache_key("esmi-default", "en", "general", text))
    assert key.startswith("public_voice_previews/")
    assert "voice_previews/" not in key.replace("public_voice_previews/", "", 1)


def test_cache_key_differs_from_tenant_scheme_for_identical_inputs():
    """Even if a tenant's cache_key(tenant_id, voice_id, speed, language,
    text) happened to hash the exact same (voice_id, language, text) tuple,
    the public "public:" namespace prefix means the two hashes can never
    collide — see platform_api/voice_preview.py's _cache_key for the
    tenant-side formula this is deliberately incompatible with."""
    import hashlib

    text = pvs.PUBLIC_SAMPLES["general"]["en"]
    public_key = pvp._cache_key("esmi-default", "en", "general", text)
    tenant_style_raw = f"some-tenant:esmi-default:1.0:en:{text}"
    tenant_style_key = hashlib.sha256(tenant_style_raw.encode("utf-8")).hexdigest()
    assert public_key != tenant_style_key


def test_cache_key_is_stable_for_identical_input():
    text = pvs.PUBLIC_SAMPLES["general"]["en"]
    a = pvp._cache_key("esmi-default", "en", "general", text)
    b = pvp._cache_key("esmi-default", "en", "general", text)
    assert a == b


def test_cache_key_changes_when_sample_id_changes():
    a = pvp._cache_key("esmi-default", "en", "general", pvs.PUBLIC_SAMPLES["general"]["en"])
    b = pvp._cache_key("esmi-default", "en", "hvac", pvs.PUBLIC_SAMPLES["hvac"]["en"])
    assert a != b


def test_cache_key_changes_when_language_changes():
    a = pvp._cache_key("esmi-default", "en", "general", pvs.PUBLIC_SAMPLES["general"]["en"])
    b = pvp._cache_key("esmi-default", "es", "general", pvs.PUBLIC_SAMPLES["general"]["es"])
    assert a != b


# ── rate limit: stricter than /chat ─────────────────────────────────────────


def test_sixth_call_in_a_minute_is_429(client, fake_s3, monkeypatch):
    monkeypatch.setattr(pvp, "_synthesize", lambda *a, **kw: b"\x00" * 16_000)
    statuses = [
        client.post("/platform/public/voice/preview", json=body()).status_code for _ in range(6)
    ]
    assert statuses[:5] == [200, 200, 200, 200, 200]
    assert statuses[5] == 429


# ── resolve_sample() itself ──────────────────────────────────────────────


def test_resolve_sample_is_case_insensitive():
    assert pvs.resolve_sample("GENERAL", "EN") == pvs.PUBLIC_SAMPLES["general"]["en"]


def test_resolve_sample_none_for_unknown_sample_or_language():
    assert pvs.resolve_sample("not-real", "en") is None
    assert pvs.resolve_sample("general", "fr") is None
