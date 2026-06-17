import contextlib
import html
import json
import os
import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import Column, DateTime, String, Text, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from airesearch.planning_agent import executor_agent_step, planner_agent

# === Load env vars ===
load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    msg = "DATABASE_URL not set"
    raise RuntimeError(msg)

# Fix for Heroku's postgres:// URL format
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)


# === DB setup ===
Base = declarative_base()
engine = create_engine(DATABASE_URL, echo=False, future=True)
SessionLocal = sessionmaker(bind=engine)


class Task(Base):
    __tablename__ = "tasks"
    id = Column(String, primary_key=True, index=True)
    prompt = Column(Text)
    status = Column(String)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    result = Column(Text)


with contextlib.suppress(Exception):
    Base.metadata.create_all(bind=engine)

# === FastAPI ===
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

task_progress = {}


class PromptRequest(BaseModel):
    prompt: str


@app.get("/", response_class=HTMLResponse)
def read_index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "index.html")


@app.get("/api", response_class=JSONResponse)
def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/generate_report")
def generate_report(req: PromptRequest) -> dict[str, str]:
    task_id = str(uuid.uuid4())
    db = SessionLocal()
    db.add(Task(id=task_id, prompt=req.prompt, status="running"))
    db.commit()
    db.close()

    task_progress[task_id] = {"steps": []}
    initial_plan_steps = planner_agent(req.prompt)
    for step_title in initial_plan_steps:
        task_progress[task_id]["steps"].append({
            "title": step_title,
            "status": "pending",
            "description": "Awaiting execution",
            "substeps": [],
        })

    thread = threading.Thread(
        target=run_agent_workflow, args=(task_id, req.prompt, initial_plan_steps)
    )
    thread.start()
    return {"task_id": task_id}


@app.get("/task_progress/{task_id}")
def get_task_progress(task_id: str) -> dict[str, Any]:
    return task_progress.get(task_id, {"steps": []})


@app.get("/task_status/{task_id}")
def get_task_status(task_id: str) -> dict[str, Any]:
    db = SessionLocal()
    task = db.query(Task).filter(Task.id == task_id).first()
    db.close()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return {
        "status": task.status,
        "result": json.loads(task.result) if task.result else None,
    }


def format_history(history: list[list[str]]) -> str:
    parts = []
    for title, desc, output in history:
        parts.append(
            f"🔹 {html.escape(title or '')}\n{html.escape(desc or '')}"
            f"\n\n📝 Output:\n{html.escape(output or '')}"
        )
    return "\n\n".join(parts)


def _build_step_card(
    prompt: str,
    agent_name: str,
    description: str,
    output: str,
    history: list,
) -> dict[str, str]:
    def esc(s: str) -> str:
        return html.escape(s or "")

    content = f"""
<div style='border:1px solid #ccc; border-radius:8px; padding:10px; margin:8px 0; background:#fff;'>
  <div style='font-weight:bold; color:#2563eb;'>📘 User Prompt</div>
  <div style='white-space:pre-wrap;'>{esc(prompt)}</div>

  <div style='font-weight:bold; color:#16a34a; margin-top:8px;'>📜 Previous Step</div>
  <pre style='white-space:pre-wrap; background:#f9fafb; padding:6px; border-radius:6px; margin:0;'>
{format_history(history[-2:-1])}
  </pre>

  <div style='font-weight:bold; color:#f59e0b; margin-top:8px;'>🧹 Your next task</div>
  <div style='white-space:pre-wrap;'>{esc(description)}</div>

  <div style='font-weight:bold; color:#10b981; margin-top:8px;'>✅ Output</div>
  <div style='white-space:pre-wrap;'>
{esc(output)}
  </div>
</div>
""".strip()
    return {"title": f"Called {agent_name}", "content": content}


def _persist_task_done(task_id: str, result: dict[str, Any]) -> None:
    db = SessionLocal()
    task = db.query(Task).filter(Task.id == task_id).first()
    if task:
        task.status = "done"
        task.result = json.dumps(result)
        task.updated_at = datetime.now(timezone.utc)
        db.commit()
    db.close()


def _persist_task_error(task_id: str) -> None:
    db = SessionLocal()
    task = db.query(Task).filter(Task.id == task_id).first()
    if task:
        task.status = "error"
        task.updated_at = datetime.now(timezone.utc)
        db.commit()
    db.close()


UpdateFn = Callable[[int, str, str, dict[str, Any] | None], None]


def _execute_plan_steps(
    plan_steps: list,
    prompt: str,
    execution_history: list,
    update_fn: UpdateFn,
) -> None:
    for i, step_title in enumerate(plan_steps):
        update_fn(i, "running", f"Executing: {step_title}", None)
        description, agent_name, output = executor_agent_step(step_title, execution_history, prompt)
        execution_history.append([step_title, description, output])
        update_fn(
            i,
            "done",
            f"Completed: {step_title}",
            _build_step_card(prompt, agent_name, description, output, execution_history),
        )


def run_agent_workflow(task_id: str, prompt: str, initial_plan_steps: list) -> None:
    steps_data = task_progress[task_id]["steps"]
    execution_history: list = []

    def update_step_status(
        index: int, status: str, description: str = "", substep: dict[str, Any] | None = None
    ) -> None:
        if index < len(steps_data):
            steps_data[index]["status"] = status
            if description:
                steps_data[index]["description"] = description
            if substep:
                steps_data[index]["substeps"].append(substep)
            steps_data[index]["updated_at"] = datetime.now(timezone.utc).isoformat()

    try:
        _execute_plan_steps(initial_plan_steps, prompt, execution_history, update_step_status)
        final_report = execution_history[-1][-1] if execution_history else "No report generated."
        _persist_task_done(task_id, {"html_report": final_report, "history": steps_data})

    except (RuntimeError, OSError, ValueError) as e:
        if steps_data:
            error_index = next(
                (i for i, s in enumerate(steps_data) if s["status"] == "running"),
                len(steps_data) - 1,
            )
            if error_index >= 0:
                update_step_status(
                    error_index,
                    "error",
                    f"Error during execution: {e}",
                    {"title": "Error", "content": str(e)},
                )
        _persist_task_error(task_id)
