# app.py — Orhelix AI • Esmi  (Chanin-style, mobile-ready)
import streamlit as st
from graph import graph
import uuid
import base64
from pathlib import Path

st.set_page_config(
    page_title="Orhelix AI • Esmi",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ── Load logo as base64 ────────────────────────────────────────────────────────
def get_logo_b64():
    logo_path = Path(__file__).parent / "logo.jpg"
    if logo_path.exists():
        with open(logo_path, "rb") as f:
            return base64.b64encode(f.read()).decode()
    return None

logo_b64 = get_logo_b64()
logo_img_tag = f'<img src="data:image/jpeg;base64,{logo_b64}" class="orhelix-logo-img" alt="Orhelix Logo">' if logo_b64 else '<div class="orhelix-logo">🤖</div>'

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');

* { font-family: 'Inter', sans-serif !important; }

.stApp { background-color: #FFFFFF; }
.main .block-container { padding-top: 1.5rem; padding-bottom: 100px; max-width: 860px; }

#MainMenu, footer, .stDeployButton { display: none !important; }
header[data-testid="stHeader"] { background: transparent !important; }

h1, h2, h3 { color: #0A2540 !important; }

section[data-testid="stSidebar"] {
    background-color: #F8F9FA !important;
    border-right: 1px solid #E8ECEF;
}

hr { border-color: #E8ECEF !important; margin: 0.75rem 0 !important; }

/* Esmi (assistant) chat bubble */
[data-testid="stChatMessage"]:has([data-testid="chatAvatarIcon-assistant"]) {
    background-color: #E6F7FA !important;
    border-radius: 16px !important;
    border: 1px solid #B2EBF2 !important;
    padding: 10px 14px !important;
    margin-bottom: 8px !important;
}

/* User chat bubble */
[data-testid="stChatMessage"]:has([data-testid="chatAvatarIcon-user"]) {
    background-color: #F1F3F5 !important;
    border-radius: 16px !important;
    border: 1px solid #E2E8F0 !important;
    padding: 10px 14px !important;
    margin-bottom: 8px !important;
}

[data-testid="stChatMessage"] p {
    color: #1e293b !important;
    font-size: 14.5px !important;
    line-height: 1.65 !important;
}

/* ── Chat input — FIXED for white bg with dark text ── */
[data-testid="stChatInput"],
.stChatInputContainer {
    background: #FFFFFF !important;
    border: 1.5px solid #00B8D4 !important;
    border-radius: 12px !important;
    box-shadow: 0 2px 12px rgba(0,184,212,0.12) !important;
}

[data-testid="stChatInput"] textarea,
.stChatInputContainer textarea {
    background: #FFFFFF !important;
    background-color: #FFFFFF !important;
    color: #0A2540 !important;
    -webkit-text-fill-color: #0A2540 !important;
    font-size: 14px !important;
    caret-color: #00B8D4 !important;
    border: none !important;
}

[data-testid="stChatInput"] textarea::placeholder,
.stChatInputContainer textarea::placeholder {
    color: #94a3b8 !important;
    -webkit-text-fill-color: #94a3b8 !important;
}

div[data-baseweb="base-input"],
div[data-baseweb="textarea"],
div[data-baseweb="input"] {
    background: #FFFFFF !important;
    background-color: #FFFFFF !important;
}

[data-testid="stChatInput"] button {
    background: #00B8D4 !important;
    border-radius: 8px !important;
    color: white !important;
    border: none !important;
}

/* Sidebar buttons */
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

/* Quick chip buttons */
div[data-testid="column"] .stButton button {
    background: #F0FAFB !important;
    border: 1px solid #B2EBF2 !important;
    border-radius: 999px !important;
    color: #0A2540 !important;
    font-size: 12.5px !important;
    font-weight: 500 !important;
    padding: 6px 10px !important;
    transition: all 0.15s ease !important;
}
div[data-testid="column"] .stButton button:hover {
    background: #00B8D4 !important;
    color: white !important;
    border-color: #00B8D4 !important;
}

/* Sidebar labels */
section[data-testid="stSidebar"] label {
    color: #0A2540 !important;
    font-size: 13px !important;
    font-weight: 500 !important;
}

[data-testid="stSpinner"] { color: #00B8D4 !important; }

/* ── Header card ── */
.orhelix-header {
    display: flex;
    align-items: center;
    gap: 16px;
    padding: 18px 22px;
    background: linear-gradient(135deg, #0A2540 0%, #0e3460 100%);
    border-radius: 14px;
    margin-bottom: 20px;
    box-shadow: 0 4px 20px rgba(10,37,64,0.15);
}

/* Real logo image */
.orhelix-logo-img {
    width: 56px;
    height: 56px;
    object-fit: contain;
    border-radius: 10px;
    background: #ffffff;
    padding: 4px;
    flex-shrink: 0;
    box-shadow: 0 2px 10px rgba(0,0,0,0.25);
}

/* Fallback emoji logo */
.orhelix-logo {
    width: 56px; height: 56px;
    background: linear-gradient(135deg, #00B8D4, #00D4B8);
    border-radius: 12px;
    display: flex; align-items: center; justify-content: center;
    font-size: 26px;
    flex-shrink: 0;
    box-shadow: 0 2px 8px rgba(0,184,212,0.4);
}

/* Text block next to logo */
.orhelix-header-text {
    display: flex;
    flex-direction: column;
    gap: 2px;
    flex: 1;
    min-width: 0;
}

.orhelix-title {
    font-size: 20px !important;
    font-weight: 700 !important;
    color: #ffffff !important;
    line-height: 1.2;
    margin: 0;
    letter-spacing: -0.01em;
}

.orhelix-slogan {
    font-size: 11.5px;
    color: #00D4EE;
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    margin: 0;
    opacity: 0.9;
}

.orhelix-tagline {
    font-size: 12px;
    color: rgba(255,255,255,0.55);
    margin: 0;
    font-style: italic;
    font-weight: 300;
}

.orhelix-badge {
    margin-left: auto;
    background: rgba(0,184,212,0.2);
    border: 1px solid rgba(0,184,212,0.4);
    border-radius: 999px;
    padding: 5px 14px;
    font-size: 11.5px;
    color: #00D4EE;
    font-weight: 600;
    display: flex;
    align-items: center;
    gap: 6px;
    white-space: nowrap;
    letter-spacing: 0.04em;
    flex-shrink: 0;
}

.badge-dot {
    width: 7px; height: 7px;
    background: #00D4EE;
    border-radius: 50%;
    box-shadow: 0 0 6px #00D4EE;
    animation: blink 2s ease infinite;
}

.stat-card {
    background: white;
    border: 1px solid #E2E8F0;
    border-radius: 10px;
    padding: 10px 14px;
    margin-bottom: 8px;
}
.stat-label { font-size: 11px; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.08em; font-weight: 600; }
.stat-value { font-size: 15px; font-weight: 700; color: #0A2540; margin-top: 2px; }
.stat-value span { color: #00B8D4; }

.orhelix-footer {
    text-align: center;
    font-size: 11.5px;
    color: #94a3b8;
    padding: 16px 0 4px;
    letter-spacing: 0.03em;
}
.orhelix-footer b { color: #0A2540; font-weight: 600; }

/* Sidebar logo */
.sidebar-logo-wrap {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px 0 4px;
    margin-bottom: 4px;
}
.sidebar-logo-img {
    width: 36px; height: 36px;
    object-fit: contain;
    border-radius: 8px;
    background: #fff;
    padding: 3px;
    border: 1px solid #E2E8F0;
}
.sidebar-brand-name {
    font-size: 14px !important;
    font-weight: 700 !important;
    color: #0A2540 !important;
    line-height: 1.2;
}
.sidebar-brand-tag {
    font-size: 10.5px !important;
    color: #00B8D4 !important;
    font-weight: 500 !important;
}

/* Mobile */
@media (max-width: 768px) {
    .main .block-container { padding: 1rem 0.75rem 100px; }
    .orhelix-header { padding: 14px 16px; gap: 12px; }
    .orhelix-title { font-size: 16px !important; }
    .orhelix-slogan { font-size: 10px; }
    .orhelix-tagline { display: none; }
    .orhelix-badge { display: none; }
    .orhelix-logo-img { width: 44px; height: 44px; }
    [data-testid="stChatInput"],
    .stChatInputContainer {
        position: fixed !important;
        bottom: 0 !important;
        left: 0 !important;
        right: 0 !important;
        border-radius: 0 !important;
        border-left: none !important;
        border-right: none !important;
        border-bottom: none !important;
        padding: 8px 12px !important;
        z-index: 999 !important;
        box-shadow: 0 -4px 20px rgba(0,0,0,0.08) !important;
        background: white !important;
    }
    section[data-testid="stSidebar"] { display: none !important; }
}

@keyframes blink {
    0%, 100% { opacity: 1; }
    50%       { opacity: 0.3; }
}
</style>
""", unsafe_allow_html=True)

# ── SESSION STATE ──────────────────────────────────────────────────────────────
if "thread_id" not in st.session_state:
    st.session_state.thread_id = str(uuid.uuid4())

if "messages" not in st.session_state:
    st.session_state.messages = [
        {
            "role": "assistant",
            "content": "Hello! I'm **Esmi**, your AI consultant at Orhelix. 👋\n\nI can help you **book appointments**, answer questions about our **services & pricing**, and **check availability** — all in real time. How can I help you today?"
        }
    ]

if "quick_prompt" not in st.session_state:
    st.session_state.quick_prompt = None

config = {"configurable": {"thread_id": st.session_state.thread_id}}

# ── SIDEBAR ───────────────────────────────────────────────────────────────────
with st.sidebar:
    # Sidebar logo + brand
    if logo_b64:
        st.markdown(f"""
        <div class="sidebar-logo-wrap">
            <img src="data:image/jpeg;base64,{logo_b64}" class="sidebar-logo-img" alt="Orhelix Logo">
            <div>
                <div class="sidebar-brand-name">Orhelix AI</div>
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
        st.session_state.messages = [
            {
                "role": "assistant",
                "content": "Conversation reset. I'm Esmi — how can I help you today? 😊"
            }
        ]
        st.session_state.thread_id = str(uuid.uuid4())
        st.rerun()

    st.markdown("""
    <div style="padding-top:1.5rem;text-align:center;">
        <div style="font-size:11px;color:#94a3b8;line-height:1.7;">
            🤖 Powered by <b style="color:#0A2540;">Orhelix AI</b><br>
            LangGraph · GPT-4o · Google Calendar
        </div>
    </div>
    """, unsafe_allow_html=True)

# ── MAIN ──────────────────────────────────────────────────────────────────────

# Header with real logo, slogan, and tagline
st.markdown(f"""
<div class="orhelix-header">
    {logo_img_tag}
    <div class="orhelix-header-text">
        <div class="orhelix-title">Orhelix AI Consulting</div>
        <div class="orhelix-slogan">Orchestrating the Future of AI</div>
        <div class="orhelix-tagline">AI Consulting that Evolves with You</div>
    </div>
    <div class="orhelix-badge">
        <div class="badge-dot"></div>
        Live
    </div>
</div>
""", unsafe_allow_html=True)

# Quick chips — only when no user messages yet
msg_count = len([m for m in st.session_state.messages if m["role"] == "user"])
if msg_count == 0:
    chips = [
        ("📅", "Book an appointment"),
        ("🕐", "Check this week's availability"),
        ("💰", "Pricing & services"),
        ("📋", "What do you offer?"),
    ]
    cols = st.columns(len(chips))
    for i, (col, (icon, label)) in enumerate(zip(cols, chips)):
        with col:
            if st.button(f"{icon} {label}", key=f"chip_{i}", use_container_width=True):
                st.session_state.quick_prompt = label

    st.write("")  # spacer

# Chat history
for message in st.session_state.messages:
    avatar = "🤖" if message["role"] == "assistant" else "👤"
    with st.chat_message(message["role"], avatar=avatar):
        st.markdown(message["content"])

# Chat input
prompt = st.chat_input("Ask Esmi anything — availability, services, booking…")

active_prompt = st.session_state.quick_prompt or prompt
if st.session_state.quick_prompt:
    st.session_state.quick_prompt = None

if active_prompt:
    st.session_state.messages.append({"role": "user", "content": active_prompt})

    with st.chat_message("user", avatar="👤"):
        st.markdown(active_prompt)

    with st.chat_message("assistant", avatar="🤖"):
        with st.spinner("Esmi is thinking…"):
            try:
                result = graph.invoke(
                    {"messages": [{"role": "user", "content": active_prompt}]},
                    config
                )
                response = result["messages"][-1].content
            except Exception as e:
                response = f"I'm sorry, I ran into an issue. Please try again. *(Error: {str(e)})*"
        st.markdown(response)

    st.session_state.messages.append({"role": "assistant", "content": response})
    st.rerun()

# Footer
st.markdown("""
<div class="orhelix-footer">
    © <b>Orhelix AI Consulting</b> · All rights reserved
    &nbsp;·&nbsp;
    <span style="color:#00B8D4;font-weight:600;">Orchestrating the Future of AI</span>
</div>
""", unsafe_allow_html=True)
