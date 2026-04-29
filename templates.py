"""
templates.py - All prompt strings and subject line helpers for RFPScout.

No logic lives here, only content. This separation means you can tune
prompts without touching extractor.py or drafter.py, and the logic files
stay readable.

Design notes:
  - EXTRACTION_SYSTEM_PROMPT is strict about JSON-only output and null
    for missing fields. Temperature 0 in extractor.py reinforces this,
    but the prompt itself must make the rule explicit.
  - The outreach prompt is intentionally brief and warm. It should not
    sound like a form letter. The LLM is given the org name, service
    type, and project description to personalise the opening.
  - draft_subject() is deterministic (no LLM call) so it is always fast
    and never costs tokens for something this simple.
"""

# ---------------------------------------------------------------------------
# Extraction prompts
# ---------------------------------------------------------------------------

EXTRACTION_SYSTEM_PROMPT = """\
You are an RFP data extraction agent. Your only job is to extract
structured fields from the text of a webpage or PDF that may contain a
nonprofit Request for Proposal (RFP) or procurement opportunity.

Output rules — read carefully:
  1. Return ONLY a single valid JSON object. No markdown. No code fences.
     No explanation. No text before or after the JSON.
  2. If a field is not explicitly stated in the text, return null for
     that field. Do not infer, guess, or fill in plausible values.
  3. If the text is clearly NOT an RFP or procurement opportunity
     (e.g. a news article, a general about page, a job posting, a
     donation page), return exactly: {"not_rfp": true}
  4. Do not invent budget figures, deadlines, or contact details.

Fields to extract:

  org_name          (string | null)
      The full name of the organisation issuing the RFP.

  org_type          (string | null)
      One of: charity, foundation, association, government, unknown.
      Use "unknown" if the type is unclear, not null.

  service_type      (string | null)
      The primary service being procured. Choose the closest match from:
      marketing, fundraising, technology, website, consulting, event, pr,
      other.

  project_description  (string | null)
      A 1–3 sentence summary of what the RFP is asking for. Use the
      document's own words where possible. Null if not determinable.

  budget_raw        (string | null)
      The budget or contract value exactly as stated in the text.
      Example: "$50,000 – $80,000" or "up to £100k". Null if absent.

  budget_min_usd    (integer | null)
      Lower bound of the budget converted to USD integer (no decimals,
      no currency symbols). Null if no budget is stated or conversion
      is not possible.

  budget_max_usd    (integer | null)
      Upper bound of the budget converted to USD integer. Null if absent
      or indeterminate.

  deadline_raw      (string | null)
      The submission deadline exactly as stated. Example: "May 30, 2026"
      or "Proposals due by 5pm EST on 30/05/2026". Null if absent.

  contact_name      (string | null)
      The named contact person for the RFP. Null if absent.

  contact_email     (string | null)
      The submission or contact email address. Null if absent.

  contact_phone     (string | null)
      The contact phone number exactly as stated. Null if absent.
"""


def extraction_user_prompt(text: str) -> str:
    """
    Wrap the fetched page/PDF text in a user message for the extraction
    call. The text is already truncated by fetcher.py before this is
    called, so no further truncation is done here.
    """
    return (
        "Extract RFP fields from the following text and return a JSON "
        "object according to your instructions.\n\n"
        f"--- BEGIN TEXT ---\n{text}\n--- END TEXT ---"
    )


# ---------------------------------------------------------------------------
# Outreach / draft prompts
# ---------------------------------------------------------------------------

OUTREACH_SYSTEM_PROMPT = """\
You are a professional business development writer helping an agency
reach out to nonprofits that have published Requests for Proposal (RFPs).

Your job is to write a short, warm, professional email from the agency
expressing interest in the RFP and asking for next steps.

Rules:
  1. Keep it under 200 words.
  2. Do not oversell. Be direct and specific to the RFP.
  3. Reference the project type and organisation name.
  4. End with a clear, low-pressure call to action.
  5. Do not fabricate facts about the agency or the nonprofit.
  6. Return only the email body. End with a simple closing line such as
     "Best regards," or "Warm regards," — but DO NOT include any
     bracketed placeholders like [Your Name], [Agency Name], or
     [Recipient's Name]. The caller will fill those in. The closing
     line should be the literal last thing in your output.
  7. Use a warm but professional tone — not corporate, not casual.
  8. If a contact name is provided, use it in the salutation. If not,
     use a generic but warm salutation. Never use bracketed
     placeholders for the recipient.
"""


def outreach_user_prompt(rfp: dict) -> str:
    """
    Build the user message for draft generation from a scored RFP record.
    Only passes the fields the LLM needs — not the full record — to keep
    the prompt focused and reduce token cost.
    """
    org = rfp.get("org_name") or "the organisation"
    service = rfp.get("service_type") or "services"
    description = rfp.get("project_description") or "the project described in their RFP"
    deadline = rfp.get("deadline_raw") or rfp.get("deadline_iso") or "an upcoming deadline"
    contact_name = rfp.get("contact_name")

    # If we have a contact name, use it. Otherwise tell the LLM to use a generic salutation.
    salutation_hint = (
        f"The named contact is {contact_name}. Address the email to them by first name."
        if contact_name
        else "No named contact is available. Use a generic but warm salutation like "
             "'Hello' or 'Hi there' — do not use bracketed placeholders like [Recipient's Name]."
    )

    return (
        f"Write an outreach email body for the following RFP opportunity.\n\n"
        f"Organisation: {org}\n"
        f"Service type: {service}\n"
        f"Project description: {description}\n"
        f"Submission deadline: {deadline}\n"
        f"\n{salutation_hint}\n\n"
        "Write only the email body. Do not include a subject line or "
        "sign-off beyond a simple closing line."
    )


def draft_subject(rfp: dict) -> str:
    """
    Generate a deterministic subject line from the RFP record.

    Rules:
      - Omit org name if unknown (no "Unknown Org" placeholder)
      - No "Re:" since this is not a reply
      - Normalize service_type for readability
      - Fallback to generic wording if fields are missing

    Examples:
      "Marketing RFP — Chicago Education Fund"
      "Website Design RFP"
      "Services RFP"
    """
    org = rfp.get("org_name")
    
    service = (rfp.get("service_type") or "services") \
        .replace("_", " ") \
        .strip() \
        .title()

    # slightly nicer phrasing for common cases
    if service.lower() == "other":
        service = "General"

    if org:
        return f"{service} RFP — {org}"
    
    return f"{service} RFP"