"""
FreelanceFlow backend
---------------------
FastAPI + SQLite backend for FreelanceFlow_Single_File_Frontend.html.

Run:
    py -m pip install -r requirements.txt
    py -m uvicorn main:app --reload

The database file (freelancer.db) is created automatically beside this file.
AI assistant uses a local Ollama server at http://127.0.0.1:11434 by default.
"""

from __future__ import annotations

import json
import os
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("FREELANCEFLOW_DB", str(BASE_DIR / "freelancer.db")))
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/generate")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2:latest")

app = FastAPI(title="FreelanceFlow API", version="1.0.0")

# Allows the frontend to call this API whether opened from file:// or a local web server.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def parse_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        return parsed
    except (TypeError, ValueError):
        return None


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS clients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                hourly_rate REAL NOT NULL CHECK(hourly_rate >= 0),
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS timelogs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT,
                duration REAL NOT NULL DEFAULT 0,
                accumulated_seconds REAL NOT NULL DEFAULT 0,
                running_since TEXT,
                paused INTEGER NOT NULL DEFAULT 0,
                invoiced INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY(client_id) REFERENCES clients(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                total_hours REAL NOT NULL,
                amount REAL NOT NULL,
                date TEXT NOT NULL,
                log_ids TEXT NOT NULL DEFAULT '[]',
                FOREIGN KEY(client_id) REFERENCES clients(id) ON DELETE CASCADE
            );
            """
        )
        # Lightweight migrations for an older freelancer.db made by a previous version.
        columns = {row["name"] for row in con.execute("PRAGMA table_info(timelogs)")}
        migrations = {
            "accumulated_seconds": "ALTER TABLE timelogs ADD COLUMN accumulated_seconds REAL NOT NULL DEFAULT 0",
            "running_since": "ALTER TABLE timelogs ADD COLUMN running_since TEXT",
            "paused": "ALTER TABLE timelogs ADD COLUMN paused INTEGER NOT NULL DEFAULT 0",
            "invoiced": "ALTER TABLE timelogs ADD COLUMN invoiced INTEGER NOT NULL DEFAULT 0",
        }
        for column, sql in migrations.items():
            if column not in columns:
                con.execute(sql)
        invoice_columns = {row["name"] for row in con.execute("PRAGMA table_info(invoices)")}
        if "log_ids" not in invoice_columns:
            con.execute("ALTER TABLE invoices ADD COLUMN log_ids TEXT NOT NULL DEFAULT '[]'")
        # Restore running_since for legacy open logs where possible.
        con.execute(
            "UPDATE timelogs SET running_since = start_time "
            "WHERE end_time IS NULL AND paused = 0 AND running_since IS NULL"
        )


@app.on_event("startup")
def startup() -> None:
    init_db()


class ClientCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    hourly_rate: float = Field(ge=0)


class AIQuestion(BaseModel):
    question: str = Field(min_length=1, max_length=5000)
    client_id: Optional[int] = None


def client_or_404(con: sqlite3.Connection, client_id: int) -> sqlite3.Row:
    client = con.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found.")
    return client


def elapsed_seconds(log: sqlite3.Row) -> float:
    seconds = float(log["accumulated_seconds"] or 0)
    if not log["paused"] and not log["end_time"] and log["running_since"]:
        started = parse_time(log["running_since"])
        if started:
            seconds += max(0.0, (datetime.now().astimezone() - started).total_seconds())
    return seconds


def log_payload(log: sqlite3.Row) -> dict:
    seconds = elapsed_seconds(log)
    return {
        "id": log["id"],
        "client_id": log["client_id"],
        "start_time": log["start_time"],
        "end_time": log["end_time"],
        "duration": round(float(log["duration"] or (seconds / 3600)), 6),
        "invoiced": bool(log["invoiced"]),
        "paused": bool(log["paused"]),
    }


@app.get("/")
def root():
    """Serve the FreelanceFlow website from the same server as the API."""
    index_file = BASE_DIR / "index.html"
    if not index_file.exists():
        raise HTTPException(status_code=404, detail="Website file index.html was not found")
    return FileResponse(index_file, media_type="text/html")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/clients")
def list_clients() -> list[dict]:
    with connect() as con:
        rows = con.execute(
            "SELECT id, name, hourly_rate, created_at FROM clients ORDER BY id DESC"
        ).fetchall()
        return [dict(row) for row in rows]


@app.post("/clients")
def create_client(payload: ClientCreate) -> dict:
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Client name is required.")
    with connect() as con:
        cursor = con.execute(
            "INSERT INTO clients(name, hourly_rate, created_at) VALUES (?, ?, ?)",
            (name, float(payload.hourly_rate), now_iso()),
        )
        row = con.execute("SELECT * FROM clients WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return dict(row)


@app.delete("/clients/{client_id}")
def delete_client(client_id: int) -> dict:
    with connect() as con:
        client_or_404(con, client_id)
        con.execute("DELETE FROM invoices WHERE client_id = ?", (client_id,))
        con.execute("DELETE FROM timelogs WHERE client_id = ?", (client_id,))
        con.execute("DELETE FROM clients WHERE id = ?", (client_id,))
        return {"message": "Client deleted successfully."}


@app.post("/start/{client_id}")
def start_timer(client_id: int) -> dict:
    with connect() as con:
        client_or_404(con, client_id)
        running = con.execute(
            "SELECT id FROM timelogs WHERE end_time IS NULL AND paused = 0 LIMIT 1"
        ).fetchone()
        if running:
            raise HTTPException(
                status_code=409,
                detail="A timer is already running. Stop or pause it before starting another.",
            )
        # If this client has a paused timer, resume it rather than creating a duplicate.
        paused = con.execute(
            "SELECT * FROM timelogs WHERE client_id = ? AND end_time IS NULL AND paused = 1 "
            "ORDER BY id DESC LIMIT 1",
            (client_id,),
        ).fetchone()
        started = now_iso()
        if paused:
            con.execute(
                "UPDATE timelogs SET paused = 0, running_since = ? WHERE id = ?",
                (started, paused["id"]),
            )
            return {"log_id": paused["id"], "start_time": paused["start_time"], "resumed": True}
        cursor = con.execute(
            """INSERT INTO timelogs
               (client_id, start_time, end_time, duration, accumulated_seconds, running_since, paused, invoiced)
               VALUES (?, ?, NULL, 0, 0, ?, 0, 0)""",
            (client_id, started, started),
        )
        return {"log_id": cursor.lastrowid, "start_time": started, "resumed": False}


@app.post("/stop/{log_id}")
def stop_timer(log_id: int) -> dict:
    with connect() as con:
        log = con.execute("SELECT * FROM timelogs WHERE id = ?", (log_id,)).fetchone()
        if not log:
            raise HTTPException(status_code=404, detail="Time log not found.")
        if log["end_time"]:
            raise HTTPException(status_code=400, detail="This timer has already been stopped.")
        seconds = elapsed_seconds(log)
        end = now_iso()
        con.execute(
            """UPDATE timelogs SET end_time = ?, duration = ?, accumulated_seconds = ?,
               running_since = NULL, paused = 0 WHERE id = ?""",
            (end, seconds / 3600, seconds, log_id),
        )
        return {"message": "Timer stopped.", "log_id": log_id, "end_time": end, "duration": round(seconds / 3600, 6)}


@app.post("/auto-pause/{log_id}")
def auto_pause_timer(log_id: int) -> dict:
    with connect() as con:
        log = con.execute("SELECT * FROM timelogs WHERE id = ?", (log_id,)).fetchone()
        if not log:
            raise HTTPException(status_code=404, detail="Time log not found.")
        if log["end_time"]:
            raise HTTPException(status_code=400, detail="This timer has already stopped.")
        if not log["paused"]:
            seconds = elapsed_seconds(log)
            con.execute(
                "UPDATE timelogs SET accumulated_seconds = ?, running_since = NULL, paused = 1 WHERE id = ?",
                (seconds, log_id),
            )
        return {"message": "Timer paused.", "log_id": log_id, "paused": True}


@app.post("/auto-resume/{client_id}")
def auto_resume_timer(client_id: int) -> dict:
    with connect() as con:
        client_or_404(con, client_id)
        running = con.execute(
            "SELECT id FROM timelogs WHERE end_time IS NULL AND paused = 0 LIMIT 1"
        ).fetchone()
        if running:
            raise HTTPException(status_code=409, detail="Another timer is already running.")
        log = con.execute(
            "SELECT * FROM timelogs WHERE client_id = ? AND end_time IS NULL AND paused = 1 "
            "ORDER BY id DESC LIMIT 1",
            (client_id,),
        ).fetchone()
        if not log:
            # A resume request without a paused log safely starts a fresh log.
            started = now_iso()
            cursor = con.execute(
                """INSERT INTO timelogs
                   (client_id, start_time, duration, accumulated_seconds, running_since, paused, invoiced)
                   VALUES (?, ?, 0, 0, ?, 0, 0)""",
                (client_id, started, started),
            )
            return {"log_id": cursor.lastrowid, "start_time": started, "resumed": False}
        started = now_iso()
        con.execute(
            "UPDATE timelogs SET paused = 0, running_since = ? WHERE id = ?",
            (started, log["id"]),
        )
        return {"log_id": log["id"], "start_time": log["start_time"], "resumed": True}


@app.get("/timelogs/{client_id}")
def list_timelogs(client_id: int) -> list[dict]:
    with connect() as con:
        client_or_404(con, client_id)
        rows = con.execute(
            "SELECT * FROM timelogs WHERE client_id = ? ORDER BY id DESC", (client_id,)
        ).fetchall()
        return [log_payload(row) for row in rows]


@app.get("/total-hours")
def total_hours() -> dict:
    with connect() as con:
        rows = con.execute("SELECT * FROM timelogs").fetchall()
        total = 0.0
        for row in rows:
            total += float(row["duration"] or 0) if row["end_time"] else elapsed_seconds(row) / 3600
        return {"total_hours": round(total, 4)}


@app.get("/total-earnings")
def total_earnings() -> dict:
    with connect() as con:
        rows = con.execute(
            """SELECT t.*, c.hourly_rate FROM timelogs t
               JOIN clients c ON c.id = t.client_id"""
        ).fetchall()
        total = 0.0
        for row in rows:
            hours = float(row["duration"] or 0) if row["end_time"] else elapsed_seconds(row) / 3600
            total += hours * float(row["hourly_rate"])
        return {"total_earnings": round(total, 2)}


@app.get("/invoices/{client_id}")
def list_invoices(client_id: int) -> list[dict]:
    with connect() as con:
        client_or_404(con, client_id)
        rows = con.execute(
            "SELECT * FROM invoices WHERE client_id = ? ORDER BY id DESC", (client_id,)
        ).fetchall()
        return [dict(row) for row in rows]


@app.post("/invoice/{client_id}")
def create_invoice(client_id: int) -> dict:
    with connect() as con:
        client = client_or_404(con, client_id)
        logs = con.execute(
            """SELECT * FROM timelogs WHERE client_id = ? AND end_time IS NOT NULL
               AND invoiced = 0 ORDER BY id""",
            (client_id,),
        ).fetchall()
        if not logs:
            raise HTTPException(
                status_code=400,
                detail="No completed, unbilled time logs are available for this client.",
            )
        hours = sum(float(row["duration"] or 0) for row in logs)
        amount = round(hours * float(client["hourly_rate"]), 2)
        date = datetime.now().astimezone().strftime("%Y-%m-%d")
        log_ids = [row["id"] for row in logs]
        cursor = con.execute(
            "INSERT INTO invoices(client_id, total_hours, amount, date, log_ids) VALUES (?, ?, ?, ?, ?)",
            (client_id, hours, amount, date, json.dumps(log_ids)),
        )
        con.executemany("UPDATE timelogs SET invoiced = 1 WHERE id = ?", [(item,) for item in log_ids])
        return {
            "invoice_id": cursor.lastrowid,
            "client_id": client_id,
            "total_hours": round(hours, 4),
            "amount": amount,
            "date": date,
        }


@app.get("/invoice_detail/{invoice_id}")
def invoice_detail(invoice_id: int) -> dict:
    with connect() as con:
        row = con.execute(
            """SELECT i.*, c.name AS client_name, c.hourly_rate
               FROM invoices i JOIN clients c ON c.id = i.client_id WHERE i.id = ?""",
            (invoice_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Invoice not found.")
        return dict(row)


@app.get("/invoice_pdf/{invoice_id}")
def invoice_pdf(invoice_id: int) -> Response:
    with connect() as con:
        row = con.execute(
            """SELECT i.*, c.name AS client_name, c.hourly_rate
               FROM invoices i JOIN clients c ON c.id = i.client_id WHERE i.id = ?""",
            (invoice_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Invoice not found.")
        data = dict(row)

    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
    except ImportError as exc:
        raise HTTPException(
            status_code=500,
            detail="PDF support is missing. Install dependencies from requirements.txt.",
        ) from exc

    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    y = height - 80
    pdf.setTitle(f"FreelanceFlow Invoice {invoice_id}")
    pdf.setFont("Helvetica-Bold", 22)
    pdf.drawString(55, y, "FreelanceFlow Invoice")
    y -= 42
    pdf.setFont("Helvetica", 12)
    lines = [
        f"Invoice number: #{data['id']}",
        f"Client: {data['client_name']}",
        f"Invoice date: {data['date']}",
        f"Total hours: {float(data['total_hours']):.2f}",
        f"Hourly rate: INR {float(data['hourly_rate']):.2f}",
        f"Total amount: INR {float(data['amount']):.2f}",
        "",
        "Thank you for your work!",
    ]
    for line in lines:
        pdf.drawString(55, y, line)
        y -= 25
    pdf.save()
    buffer.seek(0)
    return Response(
        content=buffer.getvalue(),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="invoice-{invoice_id}.pdf"'},
    )


@app.post("/ai/assistant")
def ai_assistant(payload: AIQuestion) -> dict:
    question = payload.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Please enter a question.")

    context = ""
    if payload.client_id is not None:
        with connect() as con:
            client = con.execute("SELECT * FROM clients WHERE id = ?", (payload.client_id,)).fetchone()
            if not client:
                raise HTTPException(status_code=404, detail="Selected client not found.")
            logs = con.execute("SELECT * FROM timelogs WHERE client_id = ?", (payload.client_id,)).fetchall()
            hours = sum(
                (float(log["duration"] or 0) if log["end_time"] else elapsed_seconds(log) / 3600)
                for log in logs
            )
            invoices = con.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(amount), 0) AS amount FROM invoices WHERE client_id = ?",
                (payload.client_id,),
            ).fetchone()
            context = (
                f"\nSelected client: {client['name']}; hourly rate: INR {client['hourly_rate']:.2f}; "
                f"tracked hours: {hours:.2f}; invoices: {invoices['n']}; "
                f"invoiced amount: INR {invoices['amount']:.2f}."
            )

    prompt = (
        "You are FreelanceFlow, a helpful and concise freelance-work assistant. "
        "Give practical, beginner-friendly advice. For financial or invoice questions, "
        "explain assumptions and do not invent stored data.\n"
        f"{context}\nUser question: {question}"
    )
    request_body = json.dumps(
        {"model": OLLAMA_MODEL, "prompt": prompt, "stream": False}
    ).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_URL,
        data=request_body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as response:
            result = json.loads(response.read().decode("utf-8"))
        answer = (result.get("response") or "").strip()
        if not answer:
            raise HTTPException(status_code=502, detail="The AI model returned an empty response.")
        return {"answer": answer}
    except urllib.error.URLError as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                f"Cannot connect to Ollama at {OLLAMA_URL}. Start Ollama and download "
                f"the model with: ollama run {OLLAMA_MODEL}"
            ),
        ) from exc
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="The AI assistant timed out. Please try again.") from exc
