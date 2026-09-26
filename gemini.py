"""
Phase 2 smoke test: confirm the Gemini API key + SDK call shape work in
isolation, before wiring it into the Flask app. Run from the repo root:

    python3 gemini.py

NOTE: this previously had a real API key hardcoded in this file. That key
must be treated as compromised - revoke/regenerate it at
https://aistudio.google.com/apikey and put the NEW key only in
backend/.env (GEMINI_API_KEY=...), which is gitignored and never committed.
"""

import os
import sys

from dotenv import load_dotenv
from google import genai

# backend/.env holds GEMINI_API_KEY; load it whether you run this from the
# repo root or from backend/.
load_dotenv(os.path.join(os.path.dirname(__file__), "backend", ".env"))

api_key = os.getenv("GEMINI_API_KEY")
if not api_key:
    sys.exit(
        "GEMINI_API_KEY is not set. Copy backend/.env.example to backend/.env "
        "and fill in a real key from https://aistudio.google.com/apikey"
    )

model = os.getenv("GEMINI_TEXT_MODEL", "gemini-2.5-flash")

client = genai.Client(api_key=api_key)

response = client.models.generate_content(
    model=model,
    contents=(
        "A patient is already labeled as Level 1 Triage. Ask one short "
        "clarifying question about whether the pain is internal or "
        "external. Do not diagnose - only ask and, if useful, note what "
        "categories of injury this could help distinguish."
    ),
)

print(response.text)
