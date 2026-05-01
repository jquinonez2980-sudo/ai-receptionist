from langchain_core.tools import tool
from langchain_google_community import CalendarToolkit
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
import os
import glob
from datetime import datetime, timedelta
import pytz

# ── GOOGLE CALENDAR SETUP ─────────────────────────────────────────────────────
creds = Credentials.from_authorized_user_file("token.json")
service = build("calendar", "v3", credentials=creds)
toolkit = CalendarToolkit(service=service)
calendar_tools = toolkit.get_tools()

# Map tool names for easy access
tools_by_name = {t.name: t for t in calendar_tools}

# ── KNOWLEDGE BASE SETUP (PDF) ────────────────────────────────────────────────
KB_FOLDER = "kb"

def load_knowledge_base() -> str:
    pdf_files = glob.glob(os.path.join(KB_FOLDER, "*.pdf"))
    if not pdf_files:
        return "No PDF found in kb folder."
    try:
        from pypdf import PdfReader
    except ImportError:
        return "PDF library not installed. Run: pip install pypdf"
    all_text = []
    for pdf_path in pdf_files:
        reader = PdfReader(pdf_path)
        for page in reader.pages:
            text = page.extract_text()
            if text:
                all_text.append(text)
    return "\n".join(all_text) if all_text else "Could not extract text from PDF."

# ── TOOLS ─────────────────────────────────────────────────────────────────────

@tool
def search_knowledge_base(query: str) -> str:
    """
    Search the business knowledge base for information about services,
    pricing, FAQs, policies, or anything about the business.
    """
    content = load_knowledge_base()
    if content in ["No PDF found in kb folder.", "Could not extract text from PDF."]:
        return content
    query_lower = query.lower()
    lines = content.split("\n")
    relevant = [l for l in lines if any(
        word in l.lower() for word in query_lower.split()
    )]
    return "\n".join(relevant) if relevant else content


@tool
def list_available_slots(start_date: str, end_date: str) -> str:
    """
    List available (free) appointment slots in Google Calendar.
    Call this whenever the user asks about availability or wants to book.

    Args:
        start_date: Start of search window in YYYY-MM-DD format (e.g. "2026-05-06")
        end_date:   End of search window in YYYY-MM-DD format (e.g. "2026-05-13")
    """
    try:
        tz = pytz.timezone("America/New_York")  # ← change to your timezone

        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")

        # Check each day for free slots (9am - 5pm, 30 min slots)
        available_slots = []
        current = start_dt

        while current <= end_dt:
            # Skip weekends
            if current.weekday() < 5:
                for hour in range(9, 17):
                    for minute in [0, 30]:
                        slot_start = tz.localize(current.replace(hour=hour, minute=minute, second=0))
                        slot_end = slot_start + timedelta(minutes=30)

                        # Check if slot is free using Google Calendar freebusy
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
                                f"{slot_start.strftime('%I:%M %p')} - "
                                f"{slot_end.strftime('%I:%M %p')}"
                            )
            current += timedelta(days=1)

        if not available_slots:
            return f"No available slots found between {start_date} and {end_date}."

        return "Available slots:\n" + "\n".join(available_slots[:10])  # show first 10

    except Exception as e:
        return f"Calendar error: {str(e)}"


@tool
def book_appointment(summary: str, start_time: str, end_time: str, attendee_email: str = None) -> str:
    """
    Book a confirmed appointment in Google Calendar.
    Only call after user has explicitly chosen a specific time slot.

    Args:
        summary:        Title (e.g. "Consultation with Jane")
        start_time:     ISO 8601 datetime (e.g. "2026-05-06T10:00:00")
        end_time:       ISO 8601 datetime (e.g. "2026-05-06T10:30:00")
        attendee_email: Optional attendee email address
    """
    try:
        tz = "America/New_York"  # ← change to your timezone

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
            f"Appointment booked successfully!\n"
            f"Title: {summary}\n"
            f"Start: {start_time}\n"
            f"End:   {end_time}\n"
            f"Link:  {created.get('htmlLink', 'N/A')}"
        )
    except Exception as e:
        return f"Booking error: {str(e)}"