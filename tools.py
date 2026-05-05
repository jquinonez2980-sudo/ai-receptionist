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
        os.unlink(tmp_path)
        return build("calendar", "v3", credentials=creds)
    except Exception:
        pass

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


# ── EMAIL NOTIFICATION HELPER ─────────────────────────────────────────────
def _send_booking_notification(
    summary: str,
    start_time: str,
    end_time: str,
    attendee_email: str = None
):
    """
    Send a branded email notification to info@orchelix.com
    when a new appointment is booked.
    Silently skips if SENDGRID_API_KEY is not set.
    """
    try:
        # Get API key — try Streamlit secrets first, then .env
        api_key = None
        try:
            import streamlit as st
            api_key = st.secrets.get("SENDGRID_API_KEY")
        except Exception:
            pass
        if not api_key:
            api_key = os.environ.get("SENDGRID_API_KEY")
        if not api_key:
            print("⚠️ SENDGRID_API_KEY not set — skipping email notification.")
            return

        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail
        from datetime import datetime

        # Format date and time nicely
        try:
            dt = datetime.fromisoformat(start_time)
            formatted_date = dt.strftime("%A, %B %d, %Y")
            formatted_time = dt.strftime("%I:%M %p")
        except Exception:
            formatted_date = start_time
            formatted_time = ""

        try:
            dt_end = datetime.fromisoformat(end_time)
            formatted_end_time = dt_end.strftime("%I:%M %p")
        except Exception:
            formatted_end_time = end_time

        subject = f"📅 New Booking — {summary}"

        html_content = f"""
        <div style="font-family: Inter, Arial, sans-serif; max-width: 600px; margin: 0 auto;">

            <!-- Header -->
            <div style="background: linear-gradient(135deg, #0A2540, #0e3460);
                        padding: 24px 28px; border-radius: 12px 12px 0 0;">
                <h1 style="color: #ffffff; margin: 0; font-size: 20px;">
                    📅 New Appointment Booked
                </h1>
                <p style="color: #00D4EE; margin: 4px 0 0; font-size: 12px;
                           letter-spacing: 0.06em; text-transform: uppercase;">
                    Orchelix AI Consulting — Esmi Receptionist
                </p>
            </div>

            <!-- Body -->
            <div style="background: #f8f9fa; padding: 28px;
                        border: 1px solid #e2e8f0; border-radius: 0 0 12px 12px;">

                <p style="color: #0A2540; font-size: 15px; margin-top: 0;">
                    A new appointment has been booked through <strong>Esmi</strong>.
                    Here are the details:
                </p>

                <!-- Details card -->
                <div style="background: #ffffff; border: 1px solid #B2EBF2;
                            border-radius: 10px; padding: 20px; margin: 16px 0;">
                    <table style="width: 100%; border-collapse: collapse;">
                        <tr>
                            <td style="padding: 10px 0; color: #94a3b8; font-size: 12px;
                                       text-transform: uppercase; letter-spacing: 0.08em;
                                       width: 140px;">Title</td>
                            <td style="padding: 10px 0; color: #0A2540;
                                       font-size: 15px; font-weight: 600;">{summary}</td>
                        </tr>
                        <tr style="border-top: 1px solid #f1f5f9;">
                            <td style="padding: 10px 0; color: #94a3b8; font-size: 12px;
                                       text-transform: uppercase; letter-spacing: 0.08em;">Date</td>
                            <td style="padding: 10px 0; color: #0A2540;
                                       font-size: 15px; font-weight: 600;">{formatted_date}</td>
                        </tr>
                        <tr style="border-top: 1px solid #f1f5f9;">
                            <td style="padding: 10px 0; color: #94a3b8; font-size: 12px;
                                       text-transform: uppercase; letter-spacing: 0.08em;">Time</td>
                            <td style="padding: 10px 0; color: #0A2540;
                                       font-size: 15px; font-weight: 600;">
                                {formatted_time} – {formatted_end_time}
                            </td>
                        </tr>
                        <tr style="border-top: 1px solid #f1f5f9;">
                            <td style="padding: 10px 0; color: #94a3b8; font-size: 12px;
                                       text-transform: uppercase; letter-spacing: 0.08em;">Client Email</td>
                            <td style="padding: 10px 0; color: #0A2540; font-size: 15px;">
                                {attendee_email if attendee_email else "Not provided"}
                            </td>
                        </tr>
                    </table>
                </div>

                <p style="color: #64748b; font-size: 13px; margin-bottom: 0;">
                    This notification was sent automatically by <strong>Esmi</strong>,
                    your AI receptionist at Orchelix AI Consulting.
                </p>
            </div>

            <!-- Footer -->
            <div style="text-align: center; padding: 16px; color: #94a3b8; font-size: 11px;">
                © Orchelix AI Consulting &nbsp;·&nbsp;
                <span style="color: #00B8D4;">Orchestrating the Future of AI</span>
            </div>
        </div>
        """

        message = Mail(
            from_email="info@orchelix.com",
            to_emails="info@orchelix.com",
            subject=subject,
            html_content=html_content
        )

        sg = SendGridAPIClient(api_key)
        response = sg.send(message)
        print(f"✅ Booking notification sent! Status: {response.status_code}")

    except Exception as e:
        # Never let email failure break the booking
        print(f"⚠️ Email notification failed: {str(e)}")


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
    """Book a confirmed appointment in Google Calendar and send email notification."""
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

        # ── Send email notification to info@orchelix.com ──────────────────
        _send_booking_notification(
            summary=summary,
            start_time=start_time,
            end_time=end_time,
            attendee_email=attendee_email
        )

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