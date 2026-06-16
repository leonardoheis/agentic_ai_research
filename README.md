# Reflective Research Agent (FastAPI + Postgres, single container)

A FastAPI web app that plans a research workflow, runs tool-using agents (Tavily, arXiv, Wikipedia), and stores task state/results in Postgres.
This repo includes a Docker setup that runs **Postgres + the API in one container** (for local/dev).

## Features

* `/` serves a simple UI (Jinja2 template) to kick off a research task.
* `/generate_report` kicks off a threaded, multi-step agent workflow (planner → research/writer/editor).
* `/task_progress/{task_id}` live status for each step/substep.
* `/task_status/{task_id}` final status + report.

---

## Project layout (key paths)

```
.
├─ main.py                      # FastAPI app (your file shown above)
├─ src/
│  ├─ planning_agent.py         # planner_agent(), executor_agent_step()
│  ├─ agents.py                 # research_agent, writer_agent, editor_agent  (example)
│  └─ research_tools.py         # tavily_search_tool, arxiv_search_tool, wikipedia_search_tool
├─ templates/
│  └─ index.html                # UI page rendered by "/"
├─ static/                      # optional static assets (css/js)
├─ docker/
│  └─ entrypoint.sh             # starts Postgres, prepares DB, then launches Uvicorn
├─ requirements.txt
├─ Dockerfile
├─ .dockerignore                # excludes .venv, __pycache__, .env, .git from image
└─ README.md
```

> Make sure `templates/index.html` and (optionally) `static/` exist and are copied into the image.

---

## Prerequisites

* **Docker** (Desktop on Windows/macOS, or engine on Linux).


* API keys stored in a `.env` file:

  ```
  OPENAI_API_KEY=your-open-api-key
  TAVILY_API_KEY=your-tavily-api-key
  ```

  > **Do not add `DATABASE_URL` to `.env`.**  The entrypoint builds it automatically from `POSTGRES_USER`, `POSTGRES_PASSWORD`, and `POSTGRES_DB`. Hardcoding it (especially pointing at the `postgres` superuser) will cause authentication failures.

* Python deps are installed by Docker from `requirements.txt`:

  * `fastapi`, `uvicorn`, `sqlalchemy`, `python-dotenv`, `jinja2`, `requests`, `wikipedia`, etc.
  * Plus any libs used by your `aisuite` client.

---

## Environment variables

The app **reads only `DATABASE_URL`** at startup.

* The container’s entrypoint sets a sane default for local dev:

  ```
  postgresql://app:local@127.0.0.1:5432/appdb
  ```
* To use Tavily:

  * Provide `TAVILY_API_KEY` (via `.env` or `-e`).

Optional (if you want to override defaults done by the entrypoint):

* `POSTGRES_USER` (default `app`)
* `POSTGRES_PASSWORD` (default `local`)
* `POSTGRES_DB` (default `appdb`)

---

## Build & Run (local/dev)

### 1) Build

```bash
docker build -t fastapi-postgres-service .
```

### 2) Run (foreground)

```bash
docker run --rm -it  -p 8000:8000  -p 5432:5432  --name fpsvc  --env-file .env  fastapi-postgres-service
```

You should see logs like:

```
🚀 Starting Postgres cluster 17/main...
✅ Postgres is ready
CREATE ROLE
CREATE DATABASE
🔗 DATABASE_URL=postgresql://app:local@127.0.0.1:5432/appdb
INFO:     Uvicorn running on http://0.0.0.0:8000
```

### 3) Open the app

* UI: [http://localhost:8000/](http://localhost:8000/)
* Docs: [http://localhost:8000/docs](http://localhost:8000/docs)

---

## API quickstart

### Kick off a run

```bash
curl -X POST http://localhost:8000/generate_report \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Large Language Models for scientific discovery", "model":"openai:gpt-4o"}'
# -> {"task_id": "UUID..."}
```

### Poll progress

```bash
curl http://localhost:8000/task_progress/<TASK_ID>
```

### Final status + report

```bash
curl http://localhost:8000/task_status/<TASK_ID>
```

---

## Troubleshooting

**`env: ‘bash\r’: No such file or directory` on container start**

* Caused by Windows CRLF line endings in `docker/entrypoint.sh`.
  The Dockerfile already strips them at build time (`sed -i ‘s/\r//’`), so a fresh `docker build` fixes it.
  If you edit `entrypoint.sh` on Windows, make sure your editor saves with LF endings (in VS Code: bottom-right status bar → change `CRLF` to `LF`).

**`password authentication failed for user "postgres"`**

* You have `DATABASE_URL=postgresql://postgres:...` in your `.env`.
  The `postgres` superuser has no password by default.
  **Fix:** remove `DATABASE_URL` from `.env` entirely and let the entrypoint build it, or override only via `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB`.
  Also use `127.0.0.1` not `localhost` — on some systems `localhost` resolves to `::1` (IPv6) which can trip up the connection.

**`TypeError: unhashable type: ‘dict’` on `GET /`**

* Caused by a Starlette 1.x API change. The old calling convention was:
  ```python
  templates.TemplateResponse("index.html", {"request": request})
  ```
  The new one (Starlette ≥ 0.37 / 1.x) is:
  ```python
  templates.TemplateResponse(request, "index.html")
  ```
  This is already fixed in `main.py`.

**I open [http://localhost:8000](http://localhost:8000) and see nothing / errors**

* Confirm `templates/index.html` exists inside the container:

  ```bash
  docker exec -it fpsvc bash -lc "ls -l /app/templates && ls -l /app/static || true"
  ```
* Watch logs while you load the page:

  ```bash
  docker logs -f fpsvc
  ```

**`DATABASE_URL not set` error**

* The entrypoint exports a default DSN. If you overrode it, ensure it’s valid:

  ```
  postgresql://<user>:<password>@127.0.0.1:<port>/<database>
  ```

**Tables disappear on restart**

* In your `main.py` you call `Base.metadata.drop_all(...)` on startup.
  Comment it out or guard with an env flag:

  ```python
  if os.getenv("RESET_DB_ON_STARTUP") == "1":
      Base.metadata.drop_all(bind=engine)
  ```

**Tavily / arXiv / Wikipedia errors**

* Provide `TAVILY_API_KEY` and ensure network access. Your `.env` should look like:

  ```
  OPENAI_API_KEY=your-open-api-key
  TAVILY_API_KEY=your-tavily-api-key
  ```

* Wikipedia rate limits sometimes; try later or handle exceptions gracefully.

---

## Development tips

* **Hot reload** (optional): For dev, you can run Uvicorn with `--reload` if you mount your code:

  ```bash
  docker run --rm -it -p 8000:8000 -p 5432:5432 \
    -v "$PWD":/app \
    --name fpsvc fastapi-postgres-service \
    bash -lc "pg_ctlcluster \$(psql -V | awk '{print \$3}' | cut -d. -f1) main start && uvicorn main:app --host 0.0.0.0 --port 8000 --reload"
  ```

* **Connect to DB from host:**

  ```bash
  psql "postgresql://app:local@localhost:5432/appdb"
  ```

---
