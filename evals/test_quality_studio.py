"""platform_api/quality_studio.py — POST /platform/quality-studio/run,
Quality Studio's practice-scenario runner (docs/ESMI_DASHBOARD_UX.md
Section 3.6).

No real LLM calls: platform_api.quality_studio._graph_module.graph is
replaced with a FakeGraph that plays back scripted LangChain messages per
turn (simulating checkpointer accumulation across calls the same way the
real graph would), so these tests are fast and exercise the endpoint's own
logic (transcript assembly, disposition/success scoring, tenant scoping,
error handling) without depending on OpenAI or a real agent.

The scenario_mode short-circuit ITSELF (the safety mechanism this endpoint
relies on) has its own dedicated tests in evals/test_tools_scenario_mode.py
— this file trusts that contract and tests the endpoint built on top of it.

What matters here:
  1. Auth: missing X-Platform-Secret -> 401; missing X-Tenant-Id -> 400 —
     same platform_api.security helpers every other route uses.
  2. Unknown scenario_id -> 400 before the graph is ever touched.
  3. Graph not ready (None) -> 503.
  4. Tenant scoping: the tenant_id from X-Tenant-Id is what reaches
     config.configurable (and therefore namespaced_thread / the prompt
     middleware / tools) — never a hardcoded or cross-tenant value.
  5. Success path: a scripted "new_lead_books" run where the fake graph
     emits a book_appointment tool call whose result carries the practice
     marker -> disposition="booked", success=True, full transcript with
     caller/esmi turns in order.
  6. Soft-fail: a scenario where the expected signal never fires (e.g.
     faq_only with no KB tool called) -> success=False, NOT an error —
     "the agent didn't do the right thing" is a valid, informative result,
     not a 500.
  7. after_hours: book_appointment called but its result has NO practice
     marker (real closed-hours rejection) -> success=True (nothing was
     actually booked).
  8. A graph exception mid-run is caught and reported as a 502, not an
     unhandled 500.
  9. angry_urgent (phase 2): success iff escalate_to_human was called;
     soft-fail appends a scenario-specific hint to `note` (never on success).
  10. existing_client_reschedule (phase 2): success iff find_booking,
      request_cancellation_code, AND reschedule_appointment all fired —
      skipping the confirmation-code step is a soft-fail even if the booking
      still got "moved".

Run: PYTHONUTF8=1 pytest evals/test_quality_studio.py -v
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import platform_api.quality_studio as qs

SECRET = "test-platform-secret"
TID = "otro-nivel"
HEADERS = {"X-Platform-Secret": SECRET, "X-Tenant-Id": TID}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("PLATFORM_API_SECRET", SECRET)
    app = FastAPI()
    app.include_router(qs.router)
    return TestClient(app)


class FakeGraph:
    """Plays back one list of response-messages per scripted caller turn,
    accumulating into `messages` the same way a real checkpointer-backed
    graph would across repeated ainvoke() calls on the same thread."""

    def __init__(self, turn_responses):
        self.turn_responses = list(turn_responses)
        self.call_count = 0
        self.messages: list = []
        self.configs: list = []  # every config passed, for tenant-scoping assertions

    async def ainvoke(self, input, config=None, context=None):
        self.configs.append(config)
        user_text = input["messages"][0]["content"]
        self.messages.append(HumanMessage(content=user_text))
        self.messages.extend(self.turn_responses[self.call_count])
        self.call_count += 1
        return {"messages": list(self.messages)}


class BoomGraph:
    async def ainvoke(self, *a, **kw):
        raise RuntimeError("graph explosion")


def ai_with_tool_call(tool_name: str, call_id: str = "call_1") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": tool_name, "args": {}, "id": call_id}],
    )


def ai_reply(text: str) -> AIMessage:
    return AIMessage(content=text)


def tool_result(call_id: str, content: str) -> ToolMessage:
    return ToolMessage(content=content, tool_call_id=call_id)


# ── auth ──────────────────────────────────────────────────────────────────


def test_requires_platform_secret(client):
    r = client.post(
        "/platform/quality-studio/run",
        json={"scenario_id": "faq_only"},
        headers={"X-Tenant-Id": TID},
    )
    assert r.status_code == 401


def test_requires_tenant_header(client):
    r = client.post(
        "/platform/quality-studio/run",
        json={"scenario_id": "faq_only"},
        headers={"X-Platform-Secret": SECRET},
    )
    assert r.status_code == 400


# ── validation ────────────────────────────────────────────────────────────


def test_unknown_scenario_id_is_400_before_touching_graph(client, monkeypatch):
    monkeypatch.setattr(qs, "_graph_module", type("M", (), {"graph": BoomGraph()})())
    r = client.post(
        "/platform/quality-studio/run", json={"scenario_id": "not-a-real-scenario"}, headers=HEADERS
    )
    assert r.status_code == 400


def test_graph_not_ready_is_503(client, monkeypatch):
    monkeypatch.setattr(qs, "_graph_module", type("M", (), {"graph": None})())
    r = client.post("/platform/quality-studio/run", json={"scenario_id": "faq_only"}, headers=HEADERS)
    assert r.status_code == 503


# ── tenant scoping ────────────────────────────────────────────────────────


def test_tenant_id_from_header_reaches_graph_config(client, monkeypatch):
    fake = FakeGraph([[ai_reply("Our hours are 9 to 5.")]])
    monkeypatch.setattr(qs, "_graph_module", type("M", (), {"graph": fake})())

    r = client.post(
        "/platform/quality-studio/run",
        json={"scenario_id": "faq_only"},
        headers={"X-Platform-Secret": SECRET, "X-Tenant-Id": "coastline-condos"},
    )
    assert r.status_code == 200, r.text
    assert len(fake.configs) == 1
    assert fake.configs[0]["configurable"]["tenant_id"] == "coastline-condos"
    assert fake.configs[0]["configurable"]["scenario_mode"] is True
    assert "coastline-condos" in fake.configs[0]["configurable"]["thread_id"]


def test_response_tenant_id_matches_request(client, monkeypatch):
    fake = FakeGraph([[ai_reply("Our hours are 9 to 5.")]])
    monkeypatch.setattr(qs, "_graph_module", type("M", (), {"graph": fake})())
    r = client.post("/platform/quality-studio/run", json={"scenario_id": "faq_only"}, headers=HEADERS)
    assert r.json()["tenant_id"] == TID


# ── success path: new_lead_books (4 scripted turns) ─────────────────────────


def test_new_lead_books_success_path(client, monkeypatch):
    fake = FakeGraph(
        [
            [ai_reply("Sure — what day works for you?")],
            [ai_with_tool_call("list_available_slots", "c1"),
             tool_result("c1", "Thursday 9am, 9:30, 10am"),
             ai_reply("I have Thursday at 9, 9:30, or 10 — which works?")],
            [ai_reply("Great, and can I get your name?")],
            [ai_with_tool_call("book_appointment", "c2"),
             tool_result("c2", "[Practice run — not a real booking] Booked — confirmed for Thursday at 9am."),
             ai_reply("You're all set for Thursday at 9am, Jamie!")],
        ]
    )
    monkeypatch.setattr(qs, "_graph_module", type("M", (), {"graph": fake})())

    r = client.post(
        "/platform/quality-studio/run", json={"scenario_id": "new_lead_books"}, headers=HEADERS
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scenario_id"] == "new_lead_books"
    assert body["disposition"] == "booked"
    assert body["success"] is True
    assert "book_appointment" in body["tools_called"]
    assert "list_available_slots" in body["tools_called"]
    # 4 caller turns + 4 esmi replies, in order, caller first each time
    assert len(body["transcript"]) == 8
    assert [t["speaker"] for t in body["transcript"]] == [
        "caller", "esmi", "caller", "esmi", "caller", "esmi", "caller", "esmi",
    ]
    assert body["transcript"][-1]["text"] == "You're all set for Thursday at 9am, Jamie!"
    assert "book_appointment" in body["transcript"][-1]["tools_called"]
    assert "Not a real customer call" in body["note"]


# ── soft-fail: faq_only where the agent never touches the KB ───────────────


def test_faq_only_soft_fail_when_no_kb_tool_called(client, monkeypatch):
    fake = FakeGraph([[ai_reply("I'm not sure, let me guess...")]])
    monkeypatch.setattr(qs, "_graph_module", type("M", (), {"graph": fake})())

    r = client.post("/platform/quality-studio/run", json={"scenario_id": "faq_only"}, headers=HEADERS)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is False
    assert body["disposition"] == "no_signal"
    assert body["tools_called"] == []


def test_faq_only_success_when_kb_used_and_nothing_booked(client, monkeypatch):
    fake = FakeGraph(
        [
            [ai_with_tool_call("search_knowledge_base", "c1"),
             tool_result("c1", "We're open 9-5, prices start at $99."),
             ai_reply("We're open 9 to 5, and prices start at $99.")]
        ]
    )
    monkeypatch.setattr(qs, "_graph_module", type("M", (), {"graph": fake})())

    r = client.post("/platform/quality-studio/run", json={"scenario_id": "faq_only"}, headers=HEADERS)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    assert body["disposition"] == "info"


# ── after_hours: real rejection, no practice marker ─────────────────────────


def test_after_hours_success_when_booking_correctly_rejected(client, monkeypatch):
    fake = FakeGraph(
        [
            [ai_with_tool_call("book_appointment", "c1"),
             tool_result("c1", "We're closed at that hour — could you pick a daytime slot?"),
             ai_reply("We're closed at that hour — could you pick a daytime slot?")]
        ]
    )
    monkeypatch.setattr(qs, "_graph_module", type("M", (), {"graph": fake})())

    r = client.post("/platform/quality-studio/run", json={"scenario_id": "after_hours"}, headers=HEADERS)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True  # nothing was actually booked
    assert body["disposition"] == "booked"  # tool WAS called — derive_outcome only sees the call


def test_after_hours_fails_when_it_actually_books(client, monkeypatch):
    """If the agent incorrectly books an after-hours slot, that's a real
    failed scenario — a false pass here would be exactly the kind of gap
    Quality Studio exists to catch."""
    fake = FakeGraph(
        [
            [ai_with_tool_call("book_appointment", "c1"),
             tool_result("c1", "[Practice run — not a real booking] Booked — confirmed for 3am."),
             ai_reply("You're all set for 3am!")]
        ]
    )
    monkeypatch.setattr(qs, "_graph_module", type("M", (), {"graph": fake})())

    r = client.post("/platform/quality-studio/run", json={"scenario_id": "after_hours"}, headers=HEADERS)

    assert r.status_code == 200, r.text
    assert r.json()["success"] is False


# ── spanish_caller ────────────────────────────────────────────────────────


def test_spanish_caller_success_when_replies_are_spanish(client, monkeypatch):
    fake = FakeGraph(
        [
            [ai_reply("¡Hola! Sí, tenemos citas disponibles esta semana.")],
            [ai_reply("Nuestro horario de atención es de 9 a 5, de lunes a viernes.")],
        ]
    )
    monkeypatch.setattr(qs, "_graph_module", type("M", (), {"graph": fake})())

    r = client.post("/platform/quality-studio/run", json={"scenario_id": "spanish_caller"}, headers=HEADERS)

    assert r.status_code == 200, r.text
    assert r.json()["success"] is True


def test_spanish_caller_fails_when_reply_is_english(client, monkeypatch):
    fake = FakeGraph(
        [
            [ai_reply("Hi! Yes, we have appointments available this week.")],
            [ai_reply("Our hours are 9 to 5, Monday through Friday.")],
        ]
    )
    monkeypatch.setattr(qs, "_graph_module", type("M", (), {"graph": fake})())

    r = client.post("/platform/quality-studio/run", json={"scenario_id": "spanish_caller"}, headers=HEADERS)

    assert r.status_code == 200, r.text
    assert r.json()["success"] is False


# ── angry_urgent ──────────────────────────────────────────────────────────────


def test_angry_urgent_success_when_escalated(client, monkeypatch):
    fake = FakeGraph(
        [
            [ai_reply("I'm really sorry to hear that — let me get you some help.")],
            [ai_with_tool_call("escalate_to_human", "c1"),
             tool_result("c1", "I've flagged this for our team and someone will follow up "
                                "with you shortly. [Practice run — no real email was sent]"),
             ai_reply("I've flagged this for our team — someone will reach out shortly.")],
        ]
    )
    monkeypatch.setattr(qs, "_graph_module", type("M", (), {"graph": fake})())

    r = client.post("/platform/quality-studio/run", json={"scenario_id": "angry_urgent"}, headers=HEADERS)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    assert body["disposition"] == "escalated"
    assert "escalate_to_human" in body["tools_called"]
    assert "hint" not in body["note"].lower()  # no soft-fail hint appended on success


def test_angry_urgent_soft_fail_with_hint_when_not_escalated(client, monkeypatch):
    fake = FakeGraph(
        [
            [ai_reply("I understand you're upset — let's see what I can do.")],
            [ai_reply("Can you tell me more about the issue?")],
        ]
    )
    monkeypatch.setattr(qs, "_graph_module", type("M", (), {"graph": fake})())

    r = client.post("/platform/quality-studio/run", json={"scenario_id": "angry_urgent"}, headers=HEADERS)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is False
    assert "escalate_to_human" not in body["tools_called"]
    assert "should have called escalate_to_human" in body["note"]


# ── existing_client_reschedule ───────────────────────────────────────────────


def test_existing_client_reschedule_success_when_full_security_flow_runs(client, monkeypatch):
    fake = FakeGraph(
        [
            [ai_reply("Sorry to hear that — let's get it moved. What's your email?")],
            [ai_with_tool_call("find_booking", "c1"),
             tool_result("c1", "Found these upcoming bookings:\n- Appointment on Thursday at "
                                "2:00 PM (id: prac0001) [Practice run — no real calendar was searched]"),
             ai_with_tool_call("request_cancellation_code", "c2"),
             tool_result("c2", "I've sent a confirmation code to the contact on file. "
                                "[Practice run — no real code was sent]"),
             ai_reply("I've sent a code to your email — what does it say?")],
            [ai_with_tool_call("reschedule_appointment", "c3"),
             tool_result("c3", "Done — I've moved your appointment to Friday at 2pm. "
                                "[Practice run — no real booking was touched]"),
             ai_reply("You're all set for Friday at 2pm!")],
        ]
    )
    monkeypatch.setattr(qs, "_graph_module", type("M", (), {"graph": fake})())

    r = client.post(
        "/platform/quality-studio/run", json={"scenario_id": "existing_client_reschedule"}, headers=HEADERS
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    assert {"find_booking", "request_cancellation_code", "reschedule_appointment"} <= set(
        body["tools_called"]
    )
    assert "hint" not in body["note"].lower()


def test_existing_client_reschedule_soft_fail_when_code_step_is_skipped(client, monkeypatch):
    """The agent finds the booking but reschedules without ever requesting a
    confirmation code — exactly the security-flow shortcut this scenario
    exists to catch."""
    fake = FakeGraph(
        [
            [ai_reply("Sorry to hear that — let's get it moved. What's your email?")],
            [ai_with_tool_call("find_booking", "c1"),
             tool_result("c1", "Found these upcoming bookings:\n- Appointment on Thursday at "
                                "2:00 PM (id: prac0001) [Practice run — no real calendar was searched]"),
             ai_reply("Found it — what time would you like instead?")],
            [ai_with_tool_call("reschedule_appointment", "c2"),
             tool_result("c2", "Done — I've moved your appointment to Friday at 2pm. "
                                "[Practice run — no real booking was touched]"),
             ai_reply("You're all set for Friday at 2pm!")],
        ]
    )
    monkeypatch.setattr(qs, "_graph_module", type("M", (), {"graph": fake})())

    r = client.post(
        "/platform/quality-studio/run", json={"scenario_id": "existing_client_reschedule"}, headers=HEADERS
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is False
    assert "request_cancellation_code" not in body["tools_called"]
    assert "which step it skipped" in body["note"]


# ── errors ────────────────────────────────────────────────────────────────


def test_graph_exception_is_a_502_not_a_500(client, monkeypatch):
    monkeypatch.setattr(qs, "_graph_module", type("M", (), {"graph": BoomGraph()})())
    r = client.post("/platform/quality-studio/run", json={"scenario_id": "faq_only"}, headers=HEADERS)
    assert r.status_code == 502
