# streamlit_app.py — Orchelix AI • Esmi (mobile-ready, clean formatting)
import streamlit as st
from graph import graph
import uuid
import base64
import re
from pathlib import Path

st.set_page_config(
    page_title="Orchelix AI • Esmi",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ── Strip markdown from AI responses ─────────────────────────────────────────
def clean_response(text):
    text = re.sub(r'#{1,6}\s+', '', text)
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'__(.+?)__', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    text = re.sub(r'_(.+?)_', r'\1', text)
    text = re.sub(r'`(.+?)`', r'\1', text)
    text = re.sub(r'\n[-*_]{3,}\n', '\n', text)
    text = re.sub(r'^\s*[-*]\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

# ── Load image as base64 helper ───────────────────────────────────────────────
def load_image_b64(filenames):
    """Try a list of filenames, return (b64, mime) for first found."""
    for name in filenames:
        path = Path(__file__).parent / name
        if path.exists():
            ext = path.suffix.lower().replace(".", "")
            mime = "jpeg" if ext in ["jpg", "jpeg"] else "png"
            with open(path, "rb") as f:
                return base64.b64encode(f.read()).decode(), mime
    return None, None

# Main Orchelix logo
logo_b64, logo_mime = load_image_b64(["logo.png", "logo.jpg", "logo.jpeg"])

# Esmi avatar — looks for esmi.png / esmi.jpg in project root
esmi_b64, esmi_mime = load_image_b64(["esmi.png", "esmi.jpg", "esmi.jpeg"])

# Build header logo tag — no padding so it fills the square nicely
if logo_b64:
    logo_img_tag = f'<img src="data:image/{logo_mime};base64,{logo_b64}" class="orchelix-logo-img" alt="Orchelix Logo">'
else:
    logo_img_tag = '<div class="orchelix-logo">🧬</div>'

# Build Esmi avatar for chat (base64 data URI used in st.chat_message avatar)
if esmi_b64:
    esmi_avatar = f"data:image/{esmi_mime};base64,{esmi_b64}"
else:
    esmi_avatar = "🤖"

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');

* { font-family: 'Inter', sans-serif !important; }

.stApp { background-color: #FFFFFF; }
.main .block-container {
    padding-top: 1.5rem;
    padding-bottom: 160px;
    max-width: 860px;
}

/* ── Hide Streamlit branding only ── */
#MainMenu                            { display: none !important; }
footer                               { display: none !important; }
.stDeployButton                      { display: none !important; }
[data-testid="stToolbar"]            { display: none !important; }
[data-testid="manage-app-button"]    { display: none !important; }
[class*="viewerBadge"]               { display: none !important; }
[class*="ViewerBadge"]               { display: none !important; }
a[href="https://streamlit.io/cloud"] { display: none !important; }
a[href*="share.streamlit.io"]        { display: none !important; }
header[data-testid="stHeader"]       { background: transparent !important; }

/* ── Sidebar ── */
section[data-testid="stSidebar"] {
    background-color: #F8F9FA !important;
    border-right: 1px solid #E8ECEF;
}
hr  { border-color: #E8ECEF !important; margin: 0.75rem 0 !important; }
h1, h2, h3 { color: #0A2540 !important; }

/* ── Assistant chat bubble ── */
[data-testid="stChatMessage"]:has([data-testid="chatAvatarIcon-assistant"]) {
    background-color: #E6F7FA !important;
    border-radius: 16px !important;
    border: 1px solid #B2EBF2 !important;
    padding: 12px 16px !important;
    margin-bottom: 10px !important;
}

/* ── User chat bubble ── */
[data-testid="stChatMessage"]:has([data-testid="chatAvatarIcon-user"]) {
    background-color: #F1F3F5 !important;
    border-radius: 16px !important;
    border: 1px solid #E2E8F0 !important;
    padding: 12px 16px !important;
    margin-bottom: 10px !important;
}

/* ── Chat text ── */
[data-testid="stChatMessage"] p,
[data-testid="stChatMessage"] li,
[data-testid="stChatMessage"] span {
    color: #1e293b !important;
    font-size: 15px !important;
    line-height: 1.75 !important;
    background: transparent !important;
    background-color: transparent !important;
}
[data-testid="stChatMessage"] mark,
[data-testid="stChatMessage"] code,
[data-testid="stChatMessage"] pre,
[data-testid="stChatMessage"] kbd {
    background: transparent !important;
    background-color: transparent !important;
    color: #1e293b !important;
    font-family: 'Inter', sans-serif !important;
    font-size: 15px !important;
    padding: 0 !important;
    border: none !important;
    border-radius: 0 !important;
    box-shadow: none !important;
}

/* ── Chat input ── */
[data-testid="stChatInput"] {
    background: #FFFFFF !important;
    border: 1.5px solid #00B8D4 !important;
    border-radius: 12px !important;
    box-shadow: 0 2px 12px rgba(0,184,212,0.12) !important;
}
[data-testid="stChatInput"] textarea {
    background: #FFFFFF !important;
    background-color: #FFFFFF !important;
    color: #0A2540 !important;
    -webkit-text-fill-color: #0A2540 !important;
    font-size: 14px !important;
    caret-color: #00B8D4 !important;
    border: none !important;
}
[data-testid="stChatInput"] textarea::placeholder {
    color: #94a3b8 !important;
    -webkit-text-fill-color: #94a3b8 !important;
}
[data-testid="stChatInput"] button {
    background: #00B8D4 !important;
    border-radius: 8px !important;
    color: white !important;
    border: none !important;
    z-index: 99999 !important;
    position: relative !important;
}
div[data-baseweb="base-input"],
div[data-baseweb="textarea"],
div[data-baseweb="input"] {
    background: #FFFFFF !important;
    background-color: #FFFFFF !important;
}

/* ── Sidebar buttons ── */
section[data-testid="stSidebar"] .stButton button {
    background-color: #00B8D4 !important;
    color: white !important;
    border: none !important;
    border-radius: 8px !important;
    font-weight: 500 !important;
    font-size: 13px !important;
    width: 100% !important;
    transition: background 0.2s ease !important;
}
section[data-testid="stSidebar"] .stButton button:hover {
    background-color: #008C9E !important;
}

/* ── Quick chip buttons — light teal style, 2 per row ── */
div[data-testid="column"] .stButton button {
    background: #F0FAFB !important;
    border: 1.5px solid #00B8D4 !important;
    border-radius: 12px !important;
    color: #0A2540 !important;
    font-size: 13px !important;
    font-weight: 500 !important;
    padding: 10px 8px !important;
    width: 100% !important;
    transition: all 0.15s ease !important;
    white-space: nowrap !important;
    overflow: hidden !important;
    text-overflow: ellipsis !important;
}
div[data-testid="column"] .stButton button:hover {
    background: #00B8D4 !important;
    color: white !important;
    border-color: #00B8D4 !important;
}

section[data-testid="stSidebar"] label {
    color: #0A2540 !important;
    font-size: 13px !important;
    font-weight: 500 !important;
}
[data-testid="stSpinner"] { color: #00B8D4 !important; }

/* ── Header card ── */
.orchelix-header {
    display: flex;
    align-items: center;
    gap: 14px;
    padding: 14px 18px;
    background: linear-gradient(135deg, #0A2540 0%, #0e3460 100%);
    border-radius: 14px;
    margin-bottom: 16px;
    box-shadow: 0 4px 20px rgba(10,37,64,0.15);
}

/* Logo fills its container with no extra whitespace */
.orchelix-logo-img {
    width: 60px;
    height: 60px;
    object-fit: cover;        /* fills the box edge-to-edge */
    border-radius: 10px;
    flex-shrink: 0;
    box-shadow: 0 2px 10px rgba(0,0,0,0.3);
    display: block;
}

.orchelix-logo {
    width: 60px; height: 60px;
    background: linear-gradient(135deg, #00B8D4, #00D4B8);
    border-radius: 10px;
    display: flex; align-items: center; justify-content: center;
    font-size: 28px; flex-shrink: 0;
    box-shadow: 0 2px 8px rgba(0,184,212,0.4);
}

.orchelix-header-text {
    display: flex; flex-direction: column;
    gap: 2px; flex: 1; min-width: 0;
}
.orchelix-title {
    font-size: 19px !important; font-weight: 700 !important;
    color: #ffffff !important; line-height: 1.2; margin: 0;
}
.orchelix-slogan {
    font-size: 11px; color: #00D4EE; font-weight: 600;
    letter-spacing: 0.06em; text-transform: uppercase; margin: 0; opacity: 0.9;
}
.orchelix-tagline {
    font-size: 11.5px; color: rgba(255,255,255,0.5);
    margin: 0; font-style: italic; font-weight: 300;
}
.orchelix-badge {
    margin-left: auto;
    background: rgba(0,184,212,0.2);
    border: 1px solid rgba(0,184,212,0.4);
    border-radius: 999px; padding: 5px 12px;
    font-size: 11px; color: #00D4EE; font-weight: 600;
    display: flex; align-items: center; gap: 6px;
    white-space: nowrap; flex-shrink: 0;
}
.badge-dot {
    width: 7px; height: 7px; background: #00D4EE;
    border-radius: 50%; box-shadow: 0 0 6px #00D4EE;
    animation: blink 2s ease infinite;
}

/* ── Stat cards ── */
.stat-card {
    background: white; border: 1px solid #E2E8F0;
    border-radius: 10px; padding: 10px 14px; margin-bottom: 8px;
}
.stat-label { font-size: 11px; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.08em; font-weight: 600; }
.stat-value { font-size: 15px; font-weight: 700; color: #0A2540; margin-top: 2px; }
.stat-value span { color: #00B8D4; }

/* ── Footer ── */
.orchelix-footer {
    text-align: center; font-size: 11.5px; color: #94a3b8;
    padding: 16px 0 4px; letter-spacing: 0.03em;
}
.orchelix-footer b { color: #0A2540; font-weight: 600; }

/* ── Sidebar logo ── */
.sidebar-logo-wrap {
    display: flex; align-items: center; gap: 10px;
    padding: 10px 0 4px; margin-bottom: 4px;
}
.sidebar-logo-img {
    width: 40px; height: 40px; object-fit: cover;
    border-radius: 8px; border: 1px solid #E2E8F0;
}
.sidebar-brand-name {
    font-size: 14px !important; font-weight: 700 !important;
    color: #0A2540 !important; line-height: 1.2;
}
.sidebar-brand-tag {
    font-size: 10.5px !important; color: #00B8D4 !important; font-weight: 500 !important;
}

/* ── Mobile ── */
@media (max-width: 768px) {
    .main .block-container { padding: 1rem 0.75rem 160px; }
    .orchelix-header { padding: 12px 14px; gap: 10px; }
    .orchelix-title { font-size: 16px !important; }
    .orchelix-slogan { font-size: 9.5px; }
    .orchelix-tagline { display: none; }
    .orchelix-badge { display: none; }
    .orchelix-logo-img { width: 50px; height: 50px; }
    section[data-testid="stSidebar"] { display: none !important; }

    [data-testid="stChatInput"] {
        position: fixed !important;
        bottom: 60px !important;
        left: 0 !important; right: 0 !important;
        border-radius: 0 !important;
        border-left: none !important; border-right: none !important; border-bottom: none !important;
        padding: 10px 14px !important;
        z-index: 99999 !important;
        box-shadow: 0 -4px 20px rgba(0,0,0,0.10) !important;
        background: white !important;
    }
}

@keyframes blink {
    0%, 100% { opacity: 1; }
    50%       { opacity: 0.3; }
}
</style>
""", unsafe_allow_html=True)

# ── SESSION STATE ─────────────────────────────────────────────────────────────
if "thread_id" not in st.session_state:
    st.session_state.thread_id = str(uuid.uuid4())

if "messages" not in st.session_state:
    st.session_state.messages = [
        {
            "role": "assistant",
            "content": "Hello! I'm Esmi, your AI consultant at Orchelix. 👋\n\nI can help you book appointments, answer questions about our services and pricing, and check availability — all in real time. How can I help you today?"
        }
    ]

if "quick_prompt" not in st.session_state:
    st.session_state.quick_prompt = None

config = {"configurable": {"thread_id": st.session_state.thread_id}}

# ── SIDEBAR ───────────────────────────────────────────────────────────────────
with st.sidebar:
    if logo_b64:
        st.markdown(f"""
        <div class="sidebar-logo-wrap">
            <img src="data:image/{logo_mime};base64,{logo_b64}" class="sidebar-logo-img" alt="Orchelix Logo">
            <div>
                <div class="sidebar-brand-name">Orchelix AI</div>
                <div class="sidebar-brand-tag">AI Consulting that Evolves with You</div>
            </div>
        </div>
        """, unsafe_allow_html=True)
    else:
        st.markdown("""
        <div class="sidebar-logo-wrap">
            <div style="font-size:28px;">🧬</div>
            <div>
                <div class="sidebar-brand-name">Orchelix AI</div>
                <div class="sidebar-brand-tag">AI Consulting that Evolves with You</div>
            </div>
        </div>
        """, unsafe_allow_html=True)

    st.markdown("## ⚙️ Settings")
    st.divider()
    st.selectbox("AI Model", ["GPT-4o Mini", "GPT-4o"], index=0)
    st.markdown("**Active Tools**")
    c1, c2 = st.columns(2)
    with c1:
        st.checkbox("📅 Calendar", value=True, disabled=True)
    with c2:
        st.checkbox("🔍 Knowledge", value=True, disabled=True)
    st.divider()

    msg_count = len([m for m in st.session_state.messages if m["role"] == "user"])
    st.markdown(f"""
    <div class="stat-card">
        <div class="stat-label">Messages sent</div>
        <div class="stat-value"><span>{msg_count}</span></div>
    </div>
    <div class="stat-card">
        <div class="stat-label">Session ID</div>
        <div class="stat-value" style="font-size:11px;color:#94a3b8;">{st.session_state.thread_id[:18]}…</div>
    </div>
    """, unsafe_allow_html=True)
    st.divider()

    if st.button("🔄 Reset Conversation"):
        st.session_state.messages = [{"role": "assistant", "content": "Conversation reset. I'm Esmi — how can I help you today? 😊"}]
        st.session_state.thread_id = str(uuid.uuid4())
        st.rerun()

    st.markdown("""
    <div style="padding-top:1.5rem;text-align:center;">
        <div style="font-size:11px;color:#94a3b8;line-height:1.7;">
            🤖 Powered by <b style="color:#0A2540;">Orchelix AI</b><br>
            LangGraph · GPT-4o · Google Calendar
        </div>
    </div>
    """, unsafe_allow_html=True)

# ── MAIN ──────────────────────────────────────────────────────────────────────
st.markdown(f"""
<div class="orchelix-header">
    {logo_img_tag}
    <div class="orchelix-header-text">
        <div class="orchelix-title">Orchelix AI Consulting</div>
        <div class="orchelix-slogan">Orchestrating the Future of AI</div>
        <div class="orchelix-tagline">AI Consulting that Evolves with You</div>
    </div>
    <div class="orchelix-badge">
        <div class="badge-dot"></div>
        Live
    </div>
</div>
""", unsafe_allow_html=True)

# Quick chips — 2x2 grid, only shown before first user message
msg_count = len([m for m in st.session_state.messages if m["role"] == "user"])
if msg_count == 0:
    chips = [
        ("📅", "Book an appointment"),
        ("🕐", "Check this week's availability"),
        ("💰", "Pricing & services"),
        ("📋", "What do you offer?"),
    ]
    # Row 1
    col1, col2 = st.columns(2)
    with col1:
        if st.button(f"{chips[0][0]} {chips[0][1]}", key="chip_0", use_container_width=True):
            st.session_state.quick_prompt = chips[0][1]
    with col2:
        if st.button(f"{chips[1][0]} {chips[1][1]}", key="chip_1", use_container_width=True):
            st.session_state.quick_prompt = chips[1][1]
    # Row 2
    col3, col4 = st.columns(2)
    with col3:
        if st.button(f"{chips[2][0]} {chips[2][1]}", key="chip_2", use_container_width=True):
            st.session_state.quick_prompt = chips[2][1]
    with col4:
        if st.button(f"{chips[3][0]} {chips[3][1]}", key="chip_3", use_container_width=True):
            st.session_state.quick_prompt = chips[3][1]
    st.write("")

# Chat history
for message in st.session_state.messages:
    if message["role"] == "assistant":
        with st.chat_message("assistant", avatar=esmi_avatar):
            st.write(message["content"])
    else:
        with st.chat_message("user", avatar="👤"):
            st.write(message["content"])

# ── Chat input ────────────────────────────────────────────────────────────────
prompt = st.chat_input("Ask Esmi anything — availability, services, booking…")

active_prompt = st.session_state.quick_prompt or prompt
if st.session_state.quick_prompt:
    st.session_state.quick_prompt = None

if active_prompt:
    st.session_state.messages.append({"role": "user", "content": active_prompt})

    with st.chat_message("user", avatar="👤"):
        st.write(active_prompt)

    with st.chat_message("assistant", avatar=esmi_avatar):
        with st.spinner("Esmi is thinking…"):
            try:
                result = graph.invoke(
                    {"messages": [{"role": "user", "content": active_prompt}]},
                    config
                )
                raw_response = result["messages"][-1].content
                response = clean_response(raw_response)
            except Exception as e:
                response = f"I'm sorry, I ran into an issue. Please try again. (Error: {str(e)})"
        st.write(response)

    st.session_state.messages.append({"role": "assistant", "content": response})
    st.rerun()

# Footer
st.markdown("""
<div class="orchelix-footer">
    © <b>Orchelix AI Consulting</b> · All rights reserved
    &nbsp;·&nbsp;
    <span style="color:#00B8D4;font-weight:600;">Orchestrating the Future of AI</span>
</div>
""", unsafe_allow_html=True)