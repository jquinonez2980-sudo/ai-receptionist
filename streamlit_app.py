# streamlit_app.py — Orchelix AI · Esmi

import streamlit as st
from graph import graph
import uuid
import base64
import re
from pathlib import Path


def load_image_b64(filenames):
    for name in filenames:
        path = Path(__file__).parent / name
        if path.exists():
            ext = path.suffix.lower().replace(".", "")
            mime = "jpeg" if ext in ["jpg", "jpeg"] else "png"
            with open(path, "rb") as f:
                return base64.b64encode(f.read()).decode(), mime
    return None, None


logo_b64, logo_mime = load_image_b64(["logo.png", "logo.jpg", "logo.jpeg"])
esmi_b64, esmi_mime = load_image_b64(["Esmi.jpg", "esmi.png", "esmi.jpg", "esmi.jpeg"])

if esmi_b64:
    st.set_page_config(
        page_title="Esmi · Orchelix AI",
        page_icon=f"data:image/{esmi_mime};base64,{esmi_b64}",
        layout="wide",
        initial_sidebar_state="expanded",
    )
else:
    st.set_page_config(
        page_title="Esmi · Orchelix AI",
        page_icon="✦",
        layout="wide",
        initial_sidebar_state="expanded",
    )


def clean_response(text):
    text = re.sub(r"#{1,6}\s+", "", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    text = re.sub(r"\*(?!\s)(.+?)(?<!\s)\*", r"\1", text)
    text = re.sub(r"_(?!\s)(.+?)(?<!\s)_", r"\1", text)
    text = re.sub(r"`(.+?)`", r"\1", text)
    text = re.sub(r"\n[-*_]{3,}\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_time_slots(text):
    slot_pattern = re.compile(
        r"\b(\d{1,2}:\d{2}\s*(?:AM|PM)\s*[–\-]\s*\d{1,2}:\d{2}\s*(?:AM|PM))\b"
    )
    slots = slot_pattern.findall(text)
    date_pattern = re.compile(
        r"((?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+"
        r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
        r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
        r"\s+\d{1,2}(?:,?\s+\d{4})?)",
        re.IGNORECASE,
    )
    date_match = date_pattern.search(text)
    date_label = date_match.group(1) if date_match else None
    return date_label, [s.strip() for s in slots]


def strip_slots_from_text(text):
    text = re.sub(
        r"\n\s*[-•]\s*\d{1,2}:\d{2}\s*(?:AM|PM)\s*[–\-]\s*\d{1,2}:\d{2}\s*(?:AM|PM)",
        "",
        text,
    )
    text = re.sub(
        r"\n\s*\d{1,2}:\d{2}\s*(?:AM|PM)\s*[–\-]\s*\d{1,2}:\d{2}\s*(?:AM|PM)",
        "",
        text,
    )
    text = re.sub(r"Which of these works best for you\?\s*", "", text)
    return text.strip()


logo_img_tag = (
    f'<img src="data:image/{logo_mime};base64,{logo_b64}" class="hdr-logo" alt="Orchelix">'
    if logo_b64
    else '<div class="hdr-logo-fallback">✦</div>'
)
esmi_avatar = f"data:image/{esmi_mime};base64,{esmi_b64}" if esmi_b64 else "✦"
esmi_sidebar_tag = (
    f'<img src="data:image/{esmi_mime};base64,{esmi_b64}" class="sb-esmi-img" alt="Esmi">'
    if esmi_b64
    else '<div class="sb-esmi-fallback">✦</div>'
)

# ── CSS ──────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');

*, *::before, *::after { font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif !important; }

/* App shell */
.stApp { background: #F4F6F9 !important; }
.main .block-container {
    padding-top: 1.5rem;
    padding-bottom: 140px;
    max-width: 800px;
}

/* Hide Streamlit chrome */
#MainMenu, footer, .stDeployButton,
[data-testid="stToolbar"], [data-testid="manage-app-button"],
[class*="viewerBadge"], [class*="ViewerBadge"],
a[href="https://streamlit.io/cloud"], a[href*="share.streamlit.io"] { display: none !important; }
header[data-testid="stHeader"] { background: transparent !important; }

/* ─── Sidebar ─────────────────────────────────────────── */
section[data-testid="stSidebar"] {
    background: #FFFFFF !important;
    border-right: 1px solid #E8ECF0 !important;
}
section[data-testid="stSidebar"] > div { padding-top: 1.4rem; }
hr { border: none !important; border-top: 1px solid #E8ECF0 !important; margin: 1rem 0 !important; }

.sb-brand { display: flex; align-items: center; gap: 10px; padding: 2px 0 14px; }
.sb-logo { width: 36px; height: 36px; object-fit: cover; border-radius: 8px; border: 1px solid #E8ECF0; }
.sb-logo-fallback {
    width: 36px; height: 36px; border-radius: 8px;
    background: linear-gradient(135deg, #00B8D4, #0A2540);
    display: flex; align-items: center; justify-content: center;
    font-size: 16px;
}
.sb-name { font-size: 13.5px !important; font-weight: 700 !important; color: #0A2540 !important; line-height: 1.2; }
.sb-tag  { font-size: 10px !important; color: #00B8D4 !important; font-weight: 500 !important; margin-top: 2px; }

.sb-section-label {
    font-size: 10px !important; font-weight: 700 !important;
    color: #98A2B3 !important; text-transform: uppercase;
    letter-spacing: 0.1em; margin: 0 0 10px !important;
}

.sb-stat { margin-bottom: 10px; }
.sb-stat-label { font-size: 10.5px !important; color: #98A2B3 !important; text-transform: uppercase; letter-spacing: 0.08em; font-weight: 600 !important; }
.sb-stat-value { font-size: 20px !important; font-weight: 700 !important; color: #0A2540 !important; line-height: 1.2; margin-top: 1px; }
.sb-stat-value span { color: #00B8D4 !important; }
.sb-stat-mono { font-size: 10.5px !important; font-weight: 500 !important; color: #64748B !important; font-family: 'JetBrains Mono', monospace !important; margin-top: 2px; }

section[data-testid="stSidebar"] .stButton > button {
    background: #0A2540 !important;
    color: #ffffff !important;
    border: none !important;
    border-radius: 10px !important;
    font-size: 13px !important;
    font-weight: 600 !important;
    width: 100% !important;
    padding: 10px 16px !important;
    transition: background 0.15s ease !important;
    letter-spacing: 0.01em !important;
}
section[data-testid="stSidebar"] .stButton > button:hover { background: #00B8D4 !important; }

.sb-powered {
    display: flex; flex-direction: column; align-items: center;
    padding: 18px 0 8px; gap: 10px;
}
.sb-esmi-img {
    width: 72px; height: 72px;
    object-fit: cover; object-position: center 10%;
    border-radius: 14px;
    border: 2px solid #B2EBF2;
    box-shadow: 0 2px 12px rgba(0,184,212,0.18);
    background: #0A2540;
    display: block;
}
.sb-esmi-fallback {
    width: 58px; height: 58px;
    border-radius: 14px;
    background: linear-gradient(135deg, #00B8D4, #0A2540);
    display: flex; align-items: center; justify-content: center; font-size: 22px;
}
.sb-powered-text { font-size: 11px !important; color: #98A2B3 !important; text-align: center; line-height: 1.7; }
.sb-powered-text b { color: #0A2540 !important; font-weight: 600 !important; }
.sb-powered-text span { color: #00B8D4 !important; font-weight: 500 !important; }

/* ─── Main header ──────────────────────────────────────── */
.chat-header {
    display: flex; align-items: center; gap: 14px;
    padding: 16px 22px;
    background: linear-gradient(135deg, #0A2540 0%, #0e3460 100%);
    border-radius: 16px;
    margin-bottom: 18px;
    box-shadow: 0 4px 24px rgba(10,37,64,0.18);
}
.hdr-logo {
    width: 50px; height: 50px;
    object-fit: cover; object-position: center;
    border-radius: 10px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.3);
    flex-shrink: 0;
}
.hdr-logo-fallback {
    width: 50px; height: 50px;
    background: linear-gradient(135deg, #00B8D4, #00D4B8);
    border-radius: 10px;
    display: flex; align-items: center; justify-content: center;
    font-size: 20px; flex-shrink: 0;
}
.hdr-text { flex: 1; min-width: 0; }
.hdr-title {
    font-size: 17px !important; font-weight: 700 !important;
    color: #ffffff !important; line-height: 1.2; margin: 0;
    letter-spacing: -0.01em;
}
.hdr-sub { font-size: 11.5px; color: rgba(255,255,255,0.5); margin: 3px 0 0; }
.live-badge {
    display: flex; align-items: center; gap: 6px;
    background: rgba(0,184,212,0.15);
    border: 1px solid rgba(0,184,212,0.3);
    border-radius: 999px;
    padding: 5px 12px;
    font-size: 11px; color: #00D4EE; font-weight: 600;
    flex-shrink: 0; white-space: nowrap;
}
.live-dot {
    width: 6px; height: 6px; background: #00D4EE;
    border-radius: 50%; box-shadow: 0 0 6px rgba(0,212,238,0.7);
    animation: pulse 2.2s ease infinite;
}

/* ─── Quick chips ──────────────────────────────────────── */
div[data-testid="column"] .stButton > button {
    background: #FFFFFF !important;
    border: 1.5px solid #E2E8F0 !important;
    border-radius: 999px !important;
    color: #344054 !important;
    font-size: 13px !important;
    font-weight: 500 !important;
    padding: 8px 16px !important;
    width: 100% !important;
    white-space: nowrap !important;
    transition: all 0.15s ease !important;
    box-shadow: 0 1px 3px rgba(0,0,0,0.05) !important;
    line-height: 1.4 !important;
}
div[data-testid="column"] .stButton > button:hover {
    background: #0A2540 !important;
    border-color: #0A2540 !important;
    color: #ffffff !important;
    box-shadow: 0 4px 12px rgba(10,37,64,0.18) !important;
}
div[data-testid="column"] .stButton > button:active {
    background: #00B8D4 !important;
    border-color: #00B8D4 !important;
}

/* ─── Chat bubbles ─────────────────────────────────────── */
[data-testid="stChatMessage"]:has([data-testid="chatAvatarIcon-assistant"]) {
    background: #FFFFFF !important;
    border-radius: 16px !important;
    border: 1px solid #E8ECF0 !important;
    padding: 16px 20px !important;
    margin-bottom: 10px !important;
    box-shadow: 0 1px 4px rgba(0,0,0,0.05) !important;
}
[data-testid="stChatMessage"]:has([data-testid="chatAvatarIcon-user"]) {
    background: linear-gradient(135deg, #0A2540 0%, #0e3460 100%) !important;
    border-radius: 16px !important;
    border: none !important;
    padding: 16px 20px !important;
    margin-bottom: 10px !important;
    box-shadow: 0 2px 10px rgba(10,37,64,0.18) !important;
}
[data-testid="stChatMessage"]:has([data-testid="chatAvatarIcon-user"]) p,
[data-testid="stChatMessage"]:has([data-testid="chatAvatarIcon-user"]) li {
    color: rgba(255,255,255,0.9) !important;
}
[data-testid="stChatMessage"]:has([data-testid="chatAvatarIcon-assistant"]) p,
[data-testid="stChatMessage"]:has([data-testid="chatAvatarIcon-assistant"]) li {
    color: #1e293b !important;
}
[data-testid="stChatMessage"] p {
    font-size: 14.5px !important; line-height: 1.75 !important; margin-bottom: 6px !important;
}
[data-testid="stChatMessage"] ul, [data-testid="stChatMessage"] ol {
    font-size: 14.5px !important; line-height: 1.75 !important;
    padding-left: 20px !important; margin: 4px 0 8px 0 !important;
}
[data-testid="stChatMessage"] li { margin-bottom: 3px !important; }
[data-testid="stChatMessage"] mark, [data-testid="stChatMessage"] code,
[data-testid="stChatMessage"] pre, [data-testid="stChatMessage"] kbd {
    background: transparent !important; color: inherit !important;
    font-family: 'Inter', sans-serif !important; font-size: 14.5px !important;
    padding: 0 !important; border: none !important; border-radius: 0 !important; box-shadow: none !important;
}

/* ─── Chat input ───────────────────────────────────────── */
[data-testid="stChatInput"] {
    background: #FFFFFF !important;
    border: 1.5px solid #D0D5DD !important;
    border-radius: 14px !important;
    box-shadow: 0 1px 4px rgba(0,0,0,0.07), 0 4px 16px rgba(0,0,0,0.05) !important;
}
[data-testid="stChatInput"]:focus-within {
    border-color: #00B8D4 !important;
    box-shadow: 0 0 0 3px rgba(0,184,212,0.1), 0 4px 16px rgba(0,0,0,0.05) !important;
}
[data-testid="stChatInput"] textarea {
    background: #FFFFFF !important; color: #0A2540 !important;
    -webkit-text-fill-color: #0A2540 !important;
    font-size: 14px !important; caret-color: #00B8D4 !important; border: none !important;
}
[data-testid="stChatInput"] textarea::placeholder {
    color: #98A2B3 !important; -webkit-text-fill-color: #98A2B3 !important;
}
[data-testid="stChatInput"] button {
    background: #00B8D4 !important; border-radius: 8px !important;
    color: white !important; border: none !important;
}
[data-testid="stChatInput"] button:hover { background: #009AB5 !important; }
div[data-baseweb="base-input"], div[data-baseweb="textarea"], div[data-baseweb="input"] {
    background: #FFFFFF !important;
}

/* ─── Time slot section ────────────────────────────────── */
.slot-header {
    display: flex; align-items: center; gap: 8px;
    padding: 9px 14px;
    background: #0A2540;
    border-radius: 10px;
    margin: 14px 0 10px;
    font-size: 11.5px; font-weight: 600;
    color: #ffffff; letter-spacing: 0.06em; text-transform: uppercase;
}
.slot-header em { color: #00D4EE; font-style: normal; }

/* Slot buttons share the same column selector — override for card look */
.slot-grid div[data-testid="column"] .stButton > button {
    border-radius: 12px !important;
    border: 1.5px solid #B2EBF2 !important;
    background: #FFFFFF !important;
    color: #0A2540 !important;
    font-size: 13px !important;
    font-weight: 600 !important;
    padding: 12px 8px !important;
    min-height: 48px !important;
    white-space: normal !important;
    line-height: 1.35 !important;
    letter-spacing: 0 !important;
}
.slot-grid div[data-testid="column"] .stButton > button:hover {
    background: #E6F7FA !important;
    border-color: #00B8D4 !important;
    color: #0A2540 !important;
    box-shadow: 0 4px 14px rgba(0,184,212,0.18) !important;
}

/* ─── Spinner ──────────────────────────────────────────── */
[data-testid="stSpinner"] { color: #00B8D4 !important; }

/* ─── Footer ───────────────────────────────────────────── */
.app-footer {
    text-align: center; font-size: 11px; color: #98A2B3;
    padding: 22px 0 6px; letter-spacing: 0.02em;
}
.app-footer b { color: #475467; font-weight: 600; }
.app-footer span { color: #00B8D4; font-weight: 500; }

/* ─── Mobile ───────────────────────────────────────────── */
@media (max-width: 768px) {
    .main .block-container { padding: 1rem 0.75rem 140px; }
    .chat-header { padding: 12px 14px; gap: 10px; }
    .hdr-title { font-size: 15px !important; }
    .hdr-sub { font-size: 10px; }
    .live-badge { display: none; }
    .hdr-logo { width: 42px; height: 42px; }
    section[data-testid="stSidebar"] { display: none !important; }
    [data-testid="stChatInput"] {
        position: fixed !important; bottom: 0 !important;
        left: 0 !important; right: 0 !important;
        border-radius: 0 !important;
        border-left: none !important; border-right: none !important; border-bottom: none !important;
        z-index: 99999 !important;
        box-shadow: 0 -4px 20px rgba(0,0,0,0.10) !important;
    }
}

@keyframes pulse {
    0%, 100% { opacity: 1; transform: scale(1); }
    50%       { opacity: 0.45; transform: scale(0.8); }
}
</style>
""", unsafe_allow_html=True)

# ── Session state ─────────────────────────────────────────────────────────────
if "thread_id" not in st.session_state:
    st.session_state.thread_id = str(uuid.uuid4())

if "messages" not in st.session_state:
    st.session_state.messages = [
        {
            "role": "assistant",
            "content": (
                "Hi there! I'm Esmi, your AI receptionist at Orchelix. "
                "I can book appointments, check availability, and answer any questions about our services. "
                "How can I help you today?"
            ),
        }
    ]

if "quick_prompt" not in st.session_state:
    st.session_state.quick_prompt = None

config = {"configurable": {"thread_id": st.session_state.thread_id}}

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    # Brand
    if logo_b64:
        st.markdown(f"""
        <div class="sb-brand">
            <img src="data:image/{logo_mime};base64,{logo_b64}" class="sb-logo" alt="Orchelix">
            <div>
                <div class="sb-name">Orchelix AI</div>
                <div class="sb-tag">AI Consulting</div>
            </div>
        </div>
        """, unsafe_allow_html=True)
    else:
        st.markdown("""
        <div class="sb-brand">
            <div class="sb-logo-fallback">✦</div>
            <div>
                <div class="sb-name">Orchelix AI</div>
                <div class="sb-tag">AI Consulting</div>
            </div>
        </div>
        """, unsafe_allow_html=True)

    st.divider()

    # Session stats
    msg_count = len([m for m in st.session_state.messages if m["role"] == "user"])
    st.markdown(f"""
    <div class="sb-section-label">Session</div>
    <div class="sb-stat">
        <div class="sb-stat-label">Messages</div>
        <div class="sb-stat-value"><span>{msg_count}</span></div>
    </div>
    <div class="sb-stat">
        <div class="sb-stat-label">Thread ID</div>
        <div class="sb-stat-mono">{st.session_state.thread_id[:22]}…</div>
    </div>
    """, unsafe_allow_html=True)

    st.divider()

    if st.button("New Conversation"):
        st.session_state.messages = [
            {
                "role": "assistant",
                "content": "Fresh start! I'm Esmi — how can I help you today?",
            }
        ]
        st.session_state.thread_id = str(uuid.uuid4())
        st.session_state.quick_prompt = None
        st.rerun()

    # Powered by
    st.markdown(f"""
    <div class="sb-powered">
        {esmi_sidebar_tag}
        <div class="sb-powered-text">
            Powered by <b>Orchelix AI</b><br>
            <span>LangGraph · GPT-4o Mini</span>
        </div>
    </div>
    """, unsafe_allow_html=True)


# ── Header ────────────────────────────────────────────────────────────────────
st.markdown(f"""
<div class="chat-header">
    {logo_img_tag}
    <div class="hdr-text">
        <div class="hdr-title">Esmi — AI Receptionist</div>
        <div class="hdr-sub">Orchelix AI Consulting · Appointments · Services · FAQs</div>
    </div>
    <div class="live-badge"><div class="live-dot"></div>Live</div>
</div>
""", unsafe_allow_html=True)

# ── Quick chips ───────────────────────────────────────────────────────────────
chips = [
    ("📅 Book an appointment", "Book an appointment"),
    ("🕐 Check availability",  "Check this week's availability"),
    ("💰 Pricing",             "What are your pricing packages?"),
    ("📋 Services",            "What services do you offer?"),
]
c1, c2 = st.columns(2)
with c1:
    if st.button(chips[0][0], key="chip_0", use_container_width=True):
        st.session_state.quick_prompt = chips[0][1]
with c2:
    if st.button(chips[1][0], key="chip_1", use_container_width=True):
        st.session_state.quick_prompt = chips[1][1]
c3, c4 = st.columns(2)
with c3:
    if st.button(chips[2][0], key="chip_2", use_container_width=True):
        st.session_state.quick_prompt = chips[2][1]
with c4:
    if st.button(chips[3][0], key="chip_3", use_container_width=True):
        st.session_state.quick_prompt = chips[3][1]

st.write("")


# ── Message renderer ──────────────────────────────────────────────────────────
def render_message(content, role):
    if role != "assistant":
        st.markdown(content)
        return

    date_label, slots = parse_time_slots(content)

    if slots:
        cleaned = strip_slots_from_text(content)
        if cleaned:
            st.markdown(cleaned)

        dl = date_label if date_label else "Available"
        st.markdown(f"""
        <div class="slot-header">
            📅 &nbsp;<em>{dl}</em> &nbsp;— choose a time
        </div>
        """, unsafe_allow_html=True)

        max_cols = 3
        rows = [slots[i : i + max_cols] for i in range(0, len(slots), max_cols)]
        st.markdown('<div class="slot-grid">', unsafe_allow_html=True)
        for row in rows:
            cols = st.columns(len(row))
            for j, slot in enumerate(row):
                parts = re.split(r"\s*[–\-]\s*", slot)
                start = parts[0].strip()
                end   = parts[1].strip() if len(parts) > 1 else ""
                with cols[j]:
                    if st.button(
                        f"{start}\n– {end}",
                        key=f"slot_{hash(slot)}_{j}_{id(content)}",
                        use_container_width=True,
                    ):
                        st.session_state.quick_prompt = slot
        st.markdown("</div>", unsafe_allow_html=True)
    else:
        st.markdown(content)


# ── Chat history ──────────────────────────────────────────────────────────────
for message in st.session_state.messages:
    if message["role"] == "assistant":
        with st.chat_message("assistant", avatar=esmi_avatar):
            render_message(message["content"], "assistant")
    else:
        with st.chat_message("user", avatar="👤"):
            st.markdown(message["content"])

# ── Input ─────────────────────────────────────────────────────────────────────
prompt = st.chat_input("Ask Esmi anything — availability, services, pricing…")

active_prompt = st.session_state.quick_prompt or prompt
if st.session_state.quick_prompt:
    st.session_state.quick_prompt = None

if active_prompt:
    st.session_state.messages.append({"role": "user", "content": active_prompt})

    with st.chat_message("user", avatar="👤"):
        st.markdown(active_prompt)

    with st.chat_message("assistant", avatar=esmi_avatar):
        with st.spinner("Esmi is thinking…"):
            try:
                result = graph.invoke(
                    {"messages": [{"role": "user", "content": active_prompt}]},
                    config,
                )
                raw_response = result["messages"][-1].content
                response = clean_response(raw_response)
            except Exception as e:
                response = f"Something went wrong — please try again. ({e})"
        render_message(response, "assistant")

    st.session_state.messages.append({"role": "assistant", "content": response})
    st.rerun()

# ── Footer ────────────────────────────────────────────────────────────────────
st.markdown("""
<div class="app-footer">
    © <b>Orchelix AI Consulting</b> · All rights reserved &nbsp;·&nbsp;
    <span>Orchestrating the Future of AI</span>
</div>
""", unsafe_allow_html=True)
