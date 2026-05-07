---
name: email-pdf-attachment
description: Read a PDF attachment from an email (Outlook or Gmail). Use when someone forwards a booking confirmation, eTicket, invoice, or any PDF you need to extract data from. DO NOT use the browser for this -- the browser cannot open Outlook/Gmail attachments. DO NOT ask the user for the PDF contents -- read them yourself.
version: 1.1.0
---

# Read a PDF attachment from email

When someone forwards or mentions an email that has a PDF attachment, such as an eTicket, booking confirmation, invoice, medical report, flight itinerary, or similar document, use this workflow.

## Principle

Use OpenClaw's native PDF capability. Do not invent a local PDF parser path.

- Download the attachment to an OpenClaw-allowed media path.
- Read the PDF through the native `pdf` tool or OpenClaw's bundled `document-extract` PDF extractor.
- Use the LLM for interpretation/extraction.
- Use deterministic code for downstream writes.

## Step 1 -- Find the email

Outlook:

```bash
~/.config/spratt-email/skills/outlook-graph/scripts/outlook-mail.sh \
  --account personal \
  query --has-attachments --after 2026-04-10 --count 10
```

Use `--account outlook` for the work account. Use `--from "sender@..."` or `search "subject text"` to narrow.

Gmail:

```bash
gog gmail search "has:attachment newer_than:3d" -a manankakkar@gmail.com -j
```

## Step 2 -- List attachments

Outlook:

```bash
~/.config/spratt-email/skills/outlook-graph/scripts/outlook-mail.sh \
  --account personal \
  attachments <EMAIL_ID>
```

Gmail does not expose a simple native attachment download in `gog gmail messages`; use the raw API workaround below.

## Step 3 -- Download to an allowed directory

The PDF tool enforces a path allowlist. Download only to:

- `/Users/spratt/.openclaw/media/` -- preferred for transient files.
- `/Users/spratt/.config/spratt/` or a subdirectory -- allowed, but git-tracked.
- `/Users/spratt/.openclaw/canvas/` or `/Users/spratt/.openclaw/workspace/`.

Do not download to `/tmp`. Do not pass `~/...` paths. Use absolute paths.

Outlook download:

```bash
~/.config/spratt-email/skills/outlook-graph/scripts/outlook-mail.sh \
  --account personal \
  download <EMAIL_ID> "<ATTACHMENT_NAME>" /Users/spratt/.openclaw/media/
```

Optional rename:

```bash
mv "/Users/spratt/.openclaw/media/<ATTACHMENT_NAME>" /Users/spratt/.openclaw/media/attachment.pdf
```

## Step 4 -- Read the PDF with OpenClaw

Interactive/manual path:

```text
Tool: pdf
Args: {
  "pdf": "/Users/spratt/.openclaw/media/attachment.pdf",
  "prompt": "Extract all flight details: airline, flight numbers, dates, departure and arrival airports with times and timezones, passenger names, and confirmation number. Return as structured JSON."
}
```

Scheduled/script path:

Use OpenClaw's bundled `document-extract` PDF extractor. In Spratt's email scan pipeline this is wrapped by:

```bash
node /Users/spratt/.config/spratt/infrastructure/email-scan/openclaw-pdf-extract.mjs \
  /Users/spratt/.openclaw/media/attachment.pdf
```

That helper must report:

```json
{
  "extractor": "openclaw:document-extract/pdf",
  "text": "..."
}
```

The extracted text can then be passed to the LLM for structured JSON extraction.

## Step 5 -- Act on extracted data

- Flight booking -> `trip-db.py add-trip` and `trip-db.py add-flight`.
- Hotel confirmation -> `trip-db.py add-hotel`.
- Restaurant reservation -> `trip-db.py add-reservation` or Apple Reminder.
- Invoice/bill -> `remindctl add "Pay <payee>" --due <date>`.
- Medical report -> save summary to the relevant person memory file.

## Gmail attachment workaround

```bash
TOKEN=$(gog auth token -a manankakkar@gmail.com 2>/dev/null)

curl -s -H "Authorization: Bearer $TOKEN" \
  "https://gmail.googleapis.com/gmail/v1/users/me/messages/<MESSAGE_ID>/attachments/<ATTACHMENT_ID>" \
  | jq -r '.data' | base64 -D > /Users/spratt/.openclaw/media/gmail-attachment.pdf
```

Then read through the native `pdf` tool or OpenClaw `document-extract` path above.

## What not to do

- Do not use the browser for email attachments.
- Do not use `web_fetch` on Gmail or Outlook attachment URLs.
- Do not ask the user to paste the PDF contents.
- Do not download to `/tmp`.
- Do not pass relative paths or `~/...` paths to the PDF tool.
- Do not use ad hoc local PDF parsers (`pypdf`, `pdftotext`, `tesseract`, `convert`, etc.) as the first-line path when OpenClaw PDF extraction is available.
