# streamlit_app.py - AWARD-WINNING DESIGN

import streamlit as st
from graph import graph
import uuid

st.set_page_config(
    page_title="Esmy — AI Receptionist",
    page_icon="✦",
    layout="centered",
    initial_sidebar_state="collapsed"
)

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,300;0,400;0,600;1,300;1,400&family=DM+Mono:wght@300;400&family=Syne:wght@400;600;700;800&display=swap');

*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

:root {
    --bg:        #080c10;
    --bg2:       #0d1117;
    --surface:   #111820;
    --surface2:  #161e28;
    --border:    rgba(255,255,255,0.06);
    --border2:   rgba(255,255,255,0.12);
    --gold:      #c9a96e;
    --gold2:     #e8c98a;
    --gold-dim:  rgba(201,169,110,0.15);
    --gold-glow: rgba(201,169,110,0.08);
    --text:      #e8e4dc;
    --text-dim:  #8a8480;
    --text-mute: #4a4844;
    --blue:      #4a90d9;
    --blue-dim:  rgba(74,144,217,0.12);
    --radius:    16px;
    --radius-lg: 24px;
}

/* ── GLOBAL ── */
.stApp {
    background: var(--bg) !important;
    font-family: 'DM Mono', monospace !important;
}

.stApp > header { display: none !important; }
#MainMenu, footer, .stDeployButton { display: none !important; }

/* hide streamlit chrome */
.stChatMessage { background: transparent !important; border: none !important; padding: 0 !important; }
.stChatMessage > div { background: transparent !important; }
[data-testid="stChatMessageContent"] { background: transparent !important; padding: 0 !important; }

/* ── BACKGROUND TEXTURE ── */
.stApp::before {
    content: '';
    position: fixed;
    inset: 0;
    background:
        radial-gradient(ellipse 80% 50% at 50% -10%, rgba(201,169,110,0.07) 0%, transparent 60%),
        radial-gradient(ellipse 40% 40% at 90% 90%, rgba(74,144,217,0.04) 0%, transparent 50%),
        repeating-linear-gradient(
            0deg,
            transparent,
            transparent 39px,
            rgba(255,255,255,0.012) 40px
        ),
        repeating-linear-gradient(
            90deg,
            transparent,
            transparent 39px,
            rgba(255,255,255,0.012) 40px
        );
    pointer-events: none;
    z-index: 0;
}

/* ── MAIN CONTAINER ── */
.block-container {
    max-width: 780px !important;
    padding: 0 24px 120px !important;
    position: relative;
    z-index: 1;
}

/* ── HERO HEADER ── */
.aria-hero {
    text-align: center;
    padding: 64px 0 40px;
    position: relative;
}

.aria-badge {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    background: var(--gold-dim);
    border: 1px solid rgba(201,169,110,0.25);
    border-radius: 999px;
    padding: 6px 16px;
    margin-bottom: 28px;
    animation: fadeSlideDown 0.6s ease both;
}

.aria-badge-dot {
    width: 6px; height: 6px;
    background: var(--gold);
    border-radius: 50%;
    animation: pulse 2s ease infinite;
}

.aria-badge-text {
    font-family: 'DM Mono', monospace;
    font-size: 11px;
    letter-spacing: 0.12em;
    color: var(--gold);
    text-transform: uppercase;
}

.aria-name {
    font-family: 'Cormorant Garamond', serif;
    font-size: clamp(52px, 8vw, 84px);
    font-weight: 300;
    color: var(--text);
    line-height: 1;
    letter-spacing: -0.02em;
    animation: fadeSlideDown 0.7s 0.1s ease both;
}

.aria-name em {
    font-style: italic;
    color: var(--gold);
}

.aria-tagline {
    font-family: 'DM Mono', monospace;
    font-size: 12px;
    color: var(--text-dim);
    letter-spacing: 0.15em;
    text-transform: uppercase;
    margin-top: 16px;
    animation: fadeSlideDown 0.7s 0.2s ease both;
}

.aria-divider {
    display: flex;
    align-items: center;
    gap: 16px;
    margin: 32px auto;
    width: 280px;
    animation: fadeSlideDown 0.7s 0.3s ease both;
}

.aria-divider-line {
    flex: 1;
    height: 1px;
    background: linear-gradient(90deg, transparent, var(--gold), transparent);
}

.aria-divider-mark {
    color: var(--gold);
    font-size: 14px;
    opacity: 0.7;
}

/* ── CAPABILITY PILLS ── */
.aria-pills {
    display: flex;
    flex-wrap: wrap;
    justify-content: center;
    gap: 8px;
    margin-bottom: 48px;
    animation: fadeSlideDown 0.7s 0.4s ease both;
}

.aria-pill {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    background: var(--surface);
    border: 1px solid var(--border2);
    border-radius: 999px;
    padding: 7px 14px;
    font-family: 'DM Mono', monospace;
    font-size: 11px;
    color: var(--text-dim);
    letter-spacing: 0.05em;
    transition: all 0.2s ease;
}

.aria-pill-icon { color: var(--gold); font-size: 13px; }

/* ── CHAT WINDOW ── */
.chat-window {
    background: var(--surface);
    border: 1px solid var(--border2);
    border-radius: var(--radius-lg);
    overflow: hidden;
    margin-bottom: 0;
    box-shadow: 0 32px 80px rgba(0,0,0,0.5), 0 0 0 1px rgba(255,255,255,0.03);
    animation: fadeSlideUp 0.8s 0.5s ease both;
}

.chat-titlebar {
    background: var(--surface2);
    border-bottom: 1px solid var(--border);
    padding: 14px 20px;
    display: flex;
    align-items: center;
    justify-content: space-between;
}

.chat-titlebar-left {
    display: flex;
    align-items: center;
    gap: 10px;
}

.chat-avatar {
    width: 32px; height: 32px;
    background: linear-gradient(135deg, var(--gold), var(--gold2));
    border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    font-size: 14px;
    flex-shrink: 0;
}

.chat-titlebar-name {
    font-family: 'Syne', sans-serif;
    font-size: 13px;
    font-weight: 600;
    color: var(--text);
    letter-spacing: 0.03em;
}

.chat-titlebar-status {
    display: flex;
    align-items: center;
    gap: 5px;
    font-family: 'DM Mono', monospace;
    font-size: 10px;
    color: #4ade80;
    letter-spacing: 0.08em;
}

.chat-status-dot {
    width: 6px; height: 6px;
    background: #4ade80;
    border-radius: 50%;
    animation: pulse 2s ease infinite;
}

.chat-messages {
    padding: 24px 20px;
    min-height: 280px;
    max-height: 480px;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 16px;
    scrollbar-width: thin;
    scrollbar-color: var(--border2) transparent;
}

/* ── MESSAGE BUBBLES ── */
.msg-row {
    display: flex;
    gap: 10px;
    animation: msgIn 0.3s ease both;
}

.msg-row.user { flex-direction: row-reverse; }

.msg-avatar {
    width: 28px; height: 28px;
    border-radius: 50%;
    flex-shrink: 0;
    display: flex; align-items: center; justify-content: center;
    font-size: 12px;
    margin-top: 2px;
}

.msg-avatar.aria-av {
    background: linear-gradient(135deg, var(--gold), var(--gold2));
}

.msg-avatar.user-av {
    background: var(--blue-dim);
    border: 1px solid rgba(74,144,217,0.3);
    color: var(--blue);
}

.msg-bubble {
    max-width: 72%;
    padding: 12px 16px;
    border-radius: 18px;
    font-family: 'DM Mono', monospace;
    font-size: 13px;
    line-height: 1.65;
    position: relative;
}

.msg-bubble.aria-bubble {
    background: var(--bg2);
    border: 1px solid var(--border2);
    color: var(--text);
    border-bottom-left-radius: 4px;
}

.msg-bubble.user-bubble {
    background: linear-gradient(135deg, #1d4ed8, #2563eb);
    color: #fff;
    border-bottom-right-radius: 4px;
    box-shadow: 0 4px 16px rgba(37,99,235,0.3);
}

.msg-time {
    font-size: 9px;
    color: var(--text-mute);
    margin-top: 4px;
    letter-spacing: 0.08em;
    padding: 0 4px;
}
.msg-row.user .msg-time { text-align: right; }

/* ── WELCOME MESSAGE ── */
.welcome-msg {
    background: linear-gradient(135deg, rgba(201,169,110,0.08), rgba(201,169,110,0.04));
    border: 1px solid rgba(201,169,110,0.2);
    border-radius: var(--radius);
    padding: 20px;
    text-align: center;
    margin-bottom: 8px;
}

.welcome-msg-title {
    font-family: 'Cormorant Garamond', serif;
    font-size: 18px;
    font-weight: 400;
    color: var(--gold);
    margin-bottom: 6px;
}

.welcome-msg-sub {
    font-family: 'DM Mono', monospace;
    font-size: 11px;
    color: var(--text-dim);
    letter-spacing: 0.05em;
}

/* ── QUICK ACTIONS ── */
.quick-actions {
    padding: 0 20px 16px;
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
}

.quick-btn {
    background: var(--bg2);
    border: 1px solid var(--border2);
    border-radius: 999px;
    padding: 7px 14px;
    font-family: 'DM Mono', monospace;
    font-size: 11px;
    color: var(--text-dim);
    cursor: pointer;
    transition: all 0.2s ease;
    white-space: nowrap;
}

/* ── CHAT INPUT OVERRIDE ── */
.stChatInputContainer, [data-testid="stChatInput"] {
    background: var(--surface) !important;
    border: 1px solid var(--border2) !important;
    border-radius: 999px !important;
    padding: 4px 8px !important;
    margin-top: 12px !important;
    box-shadow: 0 8px 32px rgba(0,0,0,0.3) !important;
}

[data-testid="stChatInput"] textarea {
    background: transparent !important;
    color: var(--text) !important;
    font-family: 'DM Mono', monospace !important;
    font-size: 13px !important;
    border: none !important;
    outline: none !important;
}

[data-testid="stChatInput"] textarea::placeholder {
    color: var(--text-mute) !important;
    letter-spacing: 0.05em;
}

/* ── FOOTER ── */
.aria-footer {
    text-align: center;
    padding: 32px 0 0;
    font-family: 'DM Mono', monospace;
    font-size: 10px;
    color: var(--text-mute);
    letter-spacing: 0.12em;
    text-transform: uppercase;
    animation: fadeSlideUp 0.8s 0.8s ease both;
}

.aria-footer span { color: var(--gold); opacity: 0.6; }

/* ── SIDEBAR ── */
[data-testid="stSidebar"] {
    background: var(--bg2) !important;
    border-right: 1px solid var(--border) !important;
}

/* ── ANIMATIONS ── */
@keyframes fadeSlideDown {
    from { opacity: 0; transform: translateY(-16px); }
    to   { opacity: 1; transform: translateY(0); }
}

@keyframes fadeSlideUp {
    from { opacity: 0; transform: translateY(16px); }
    to   { opacity: 1; transform: translateY(0); }
}

@keyframes msgIn {
    from { opacity: 0; transform: translateY(8px); }
    to   { opacity: 1; transform: translateY(0); }
}

@keyframes pulse {
    0%, 100% { opacity: 1; transform: scale(1); }
    50%       { opacity: 0.5; transform: scale(0.85); }
}

@keyframes typing {
    0%, 60%, 100% { transform: translateY(0); opacity: 0.4; }
    30%           { transform: translateY(-4px); opacity: 1; }
}

/* ── TYPING INDICATOR ── */
.typing-indicator {
    display: flex;
    align-items: center;
    gap: 4px;
    padding: 14px 16px;
    background: var(--bg2);
    border: 1px solid var(--border2);
    border-radius: 18px;
    border-bottom-left-radius: 4px;
    width: fit-content;
}

.typing-dot {
    width: 5px; height: 5px;
    background: var(--gold);
    border-radius: 50%;
    animation: typing 1.2s ease infinite;
}
.typing-dot:nth-child(2) { animation-delay: 0.2s; }
.typing-dot:nth-child(3) { animation-delay: 0.4s; }

/* ── SCROLLBAR ── */
::-webkit-scrollbar { width: 4px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--border2); border-radius: 2px; }

/* spinner override */
[data-testid="stSpinner"] { color: var(--gold) !important; }

</style>
""", unsafe_allow_html=True)

# ── SESSION STATE ──────────────────────────────────────────────────────────────
if "thread_id" not in st.session_state:
    st.session_state.thread_id = str(uuid.uuid4())
if "messages" not in st.session_state:
    st.session_state.messages = []
if "quick_prompt" not in st.session_state:
    st.session_state.quick_prompt = None

config = {"configurable": {"thread_id": st.session_state.thread_id}}

# ── HERO ──────────────────────────────────────────────────────────────────────
st.markdown("""
<div class="aria-hero">
    <div class="aria-badge">
        <div class="aria-badge-dot"></div>
        <span class="aria-badge-text">Online &amp; Available 24 / 7</span>
    </div>
    <div class="aria-name"><em>Esmy</em></div>
    <div class="aria-tagline">Your Intelligent Virtual Receptionist</div>
    <div class="aria-divider">
        <div class="aria-divider-line"></div>
        <div class="aria-divider-mark">✦</div>
        <div class="aria-divider-line"></div>
    </div>
    <div class="aria-pills">
        <div class="aria-pill"><span class="aria-pill-icon">◈</span> Lead Qualification</div>
        <div class="aria-pill"><span class="aria-pill-icon">◈</span> Real-Time Availability</div>
        <div class="aria-pill"><span class="aria-pill-icon">◈</span> Instant Booking</div>
        <div class="aria-pill"><span class="aria-pill-icon">◈</span> Pricing &amp; FAQs</div>
    </div>
</div>
""", unsafe_allow_html=True)

# ── CHAT WINDOW ───────────────────────────────────────────────────────────────
st.markdown("""
<div class="chat-window">
    <div class="chat-titlebar">
        <div class="chat-titlebar-left">
            <div class="chat-avatar">✦</div>
            <div>
                <div class="chat-titlebar-name">Esmy</div>
            </div>
        </div>
        <div class="chat-titlebar-status">
            <div class="chat-status-dot"></div>
            Active
        </div>
    </div>
    <div class="chat-messages" id="chat-scroll">
""", unsafe_allow_html=True)

# Welcome message if no chat yet
if not st.session_state.messages:
    st.markdown("""
    <div class="welcome-msg">
        <div class="welcome-msg-title">Good to meet you.</div>
        <div class="welcome-msg-sub">Ask me about availability, services, pricing, or book an appointment.</div>
    </div>
    """, unsafe_allow_html=True)

# Render messages
from datetime import datetime
now = datetime.now().strftime("%I:%M %p")

for msg in st.session_state.messages:
    if msg["role"] == "user":
        st.markdown(f"""
        <div class="msg-row user">
            <div class="msg-avatar user-av">you</div>
            <div>
                <div class="msg-bubble user-bubble">{msg["content"]}</div>
                <div class="msg-time">{now}</div>
            </div>
        </div>
        """, unsafe_allow_html=True)
    else:
        st.markdown(f"""
        <div class="msg-row">
            <div class="msg-avatar aria-av">✦</div>
            <div>
                <div class="msg-bubble aria-bubble">{msg["content"]}</div>
                <div class="msg-time">{now}</div>
            </div>
        </div>
        """, unsafe_allow_html=True)

st.markdown("</div>", unsafe_allow_html=True)  # close chat-messages

# Quick action buttons
quick_options = [
    "📅 Book an appointment",
    "💰 What are your prices?",
    "🕐 Available this week?",
    "📋 What services do you offer?",
]

st.markdown('<div class="quick-actions">', unsafe_allow_html=True)
cols = st.columns(len(quick_options))
for i, (col, label) in enumerate(zip(cols, quick_options)):
    with col:
        if st.button(label, key=f"quick_{i}", use_container_width=True):
            st.session_state.quick_prompt = label
st.markdown("</div>", unsafe_allow_html=True)

st.markdown("</div>", unsafe_allow_html=True)  # close chat-window

# ── CHAT INPUT ────────────────────────────────────────────────────────────────
prompt = st.chat_input("Message Esmy...")

# Handle quick button or typed input
active_prompt = st.session_state.quick_prompt or prompt
if st.session_state.quick_prompt:
    st.session_state.quick_prompt = None

if active_prompt:
    st.session_state.messages.append({"role": "user", "content": active_prompt})

    with st.spinner(""):
        result = graph.invoke(
            {"messages": [{"role": "user", "content": active_prompt}]},
            config
        )
        response = result["messages"][-1].content

    st.session_state.messages.append({"role": "assistant", "content": response})
    st.rerun()

# ── FOOTER ────────────────────────────────────────────────────────────────────
st.markdown("""
<div class="aria-footer">
    Powered by <span>LangGraph</span> &nbsp;✦&nbsp; <span>GPT-4o</span> &nbsp;✦&nbsp; <span>Google Calendar</span>
</div>
""", unsafe_allow_html=True)

# Auto-scroll to bottom
st.markdown("""
<script>
    const chatScroll = document.getElementById('chat-scroll');
    if (chatScroll) chatScroll.scrollTop = chatScroll.scrollHeight;
</script>
""", unsafe_allow_html=True)