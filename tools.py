# tools.py - ORCHELIX AI RECEPTIONIST (Streamlit Cloud ready)

from langchain_core.tools import tool
from langchain_community.document_loaders import DirectoryLoader, TextLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.vectorstores import FAISS
from langchain_openai import OpenAIEmbeddings
import os
import json
import tempfile
from dotenv import load_dotenv

load_dotenv()


# ── GOOGLE CALENDAR HELPER (lazy, never runs at import time) ──────────────
def _get_calendar_service():
    """
    Build and return a Google Calendar service client.
    Tries two methods in order:
      1. Streamlit secret  GOOGLE_TOKEN_JSON  (recommended for Streamlit Cloud)
      2. Local token.json file               (works locally)
    Raises a clear RuntimeError if neither is available.
    """
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    # ── Method 1: Streamlit Cloud secret ──────────────────────────────────
    try:
        import streamlit as st
        token_data = json.loads(st.secrets["GOOGLE_TOKEN_JSON"])
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump(token_data, f)
            tmp_path = f.name
        creds = Credentials.from_authorized_user_file(tmp_path)
        os.unlink(tmp_path)          # clean up temp file immediately
        return build("calendar", "v3", credentials=creds)
    except Exception:
        pass                         # secret not set — fall through to local file

    # ── Method 2: Local token.json ─────────────────────────────────────────
    token_path = os.getenv("GOOGLE_TOKEN_PATH", "token.json")
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path)
        return build("calendar", "v3", credentials=creds)

    # ── Neither worked ─────────────────────────────────────────────────────
    raise RuntimeError(
        "Google Calendar credentials not found. "
        "Add GOOGLE_TOKEN_JSON to Streamlit secrets, or provide a local token.json file."
    )


# ── ORCHELIX KNOWLEDGE BASE ───────────────────────────────────────────────
@tool
def search_knowledge_base(query: str) -> str:
    """Search the Orchelix AI knowledge base (all .md files)."""
    try:
        loader = DirectoryLoader(
            "orchelix_knowledge_base/",
            glob="**/*.md",
            loader_cls=TextLoader,
            loader_kwargs={"encoding": "utf-8"}
        )
        docs = loader.load()

        if not docs:
            return "No documents found in orchelix_knowledge_base folder."

        text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=1000, chunk_overlap=200
        )
        splits = text_splitter.split_documents(docs)

        vectorstore = FAISS.from_documents(splits, OpenAIEmbeddings())
        retriever = vectorstore.as_retriever(search_kwargs={"k": 6})
        results = retriever.invoke(query)

        return "\n\n".join([doc.page_content for doc in results])
    except Exception as e:
        return f"Knowledge base error: {str(e)}"


# ── GOOGLE CALENDAR TOOLS ─────────────────────────────────────────────────
@tool
def list_available_slots(start_date: str, end_date: str) -> str:
    """List available 30-minute appointment slots in Google Calendar."""
    try:
        import pytz
        from datetime import datetime, timedelta

        service = _get_calendar_service()
        tz = pytz.timezone("America/Toronto")

        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        end_dt   = datetime.strptime(end_date,   "%Y-%m-%d")

        available_slots = []
        current = start_dt

        while current <= end_dt:
            if current.weekday() < 5:                      # Mon–Fri only
                for hour in range(9, 17):
                    for minute in [0, 30]:
                        slot_start = tz.localize(
                            current.replace(hour=hour, minute=minute, second=0)
                        )
                        slot_end = slot_start + timedelta(minutes=30)

                        body = {
                            "timeMin": slot_start.isoformat(),
                            "timeMax": slot_end.isoformat(),
                            "items": [{"id": "primary"}]
                        }
                        result = service.freebusy().query(body=body).execute()
                        busy = result["calendars"]["primary"]["busy"]

                        if not busy:
                            available_slots.append(
                                f"{current.strftime('%A %b %d')} at "
                                f"{slot_start.strftime('%I:%M %p')} – "
                                f"{slot_end.strftime('%I:%M %p')}"
                            )
            current += timedelta(days=1)

        if not available_slots:
            return f"No available slots found between {start_date} and {end_date}."

        return "Available slots:\n" + "\n".join(available_slots[:12])

    except RuntimeError as e:
        return f"⚠️ Calendar not configured: {str(e)}"
    except Exception as e:
        return f"Calendar error: {str(e)}"


@tool
def book_appointment(
    summary: str,
    start_time: str,
    end_time: str,
    attendee_email: str = None
) -> str:
    """Book a confirmed appointment in Google Calendar."""
    try:
        service = _get_calendar_service()
        tz = "America/Toronto"

        event = {
            "summary": summary,
            "start": {"dateTime": start_time, "timeZone": tz},
            "end":   {"dateTime": end_time,   "timeZone": tz},
        }
        if attendee_email:
            event["attendees"] = [{"email": attendee_email}]

        created = service.events().insert(
            calendarId="primary", body=event, sendUpdates="all"
        ).execute()

        return (
            f"✅ Appointment booked!\n"
            f"Title: {summary}\n"
            f"Start: {start_time}\n"
            f"End:   {end_time}\n"
            f"Link:  {created.get('htmlLink', 'N/A')}"
        )

    except RuntimeError as e:
        return f"⚠️ Calendar not configured: {str(e)}"
    except Exception as e:
        return f"Calendar error: {str(e)}"


print("✅ Tools loaded successfully with ORCHELIX KNOWLEDGE BASE (.md files)!")