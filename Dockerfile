FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080

# Secrets are provided at runtime as Railway environment variables — NEVER baked
# into the image. Set these in the Railway dashboard (environment/shared scope):
#   GOOGLE_TOKEN_B64       (or GOOGLE_REFRESH_TOKEN + GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET)
#   SENDGRID_API_KEY       (or SENDGRID_API_KEY_B64)
#   VAPI_API_KEY           (or VAPI_API_KEY_B64)
#   VAPI_SERVER_SECRET     (webhook auth — see api.py)
#   TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_SMS_FROM
# tools.py / api.py read all of the above from the process environment.

CMD ["sh", "-c", "uvicorn api:app --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers --forwarded-allow-ips='*'"]
