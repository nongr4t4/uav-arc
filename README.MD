Project test utilities: frontend + backend health-check

Quick test instructions (Windows PowerShell):

1) Run backend (create venv + install requirements)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r backend/requirements.txt
python backend/test_server.py
```

The backend will listen on port `8000` by default. Endpoints:
- `GET /ping` — returns `{"status":"ok","time":"..."}`
- `POST /echo` — echoes JSON payload back as `received` field

2) Open frontend

- Open `frontend/test.html` in a browser on the same host (or serve it from the same origin as the backend). Press the buttons to call `/ping` and `/echo`.

3) Git commit & push (run from repo root)

```powershell
git add frontend/test.html backend/test_server.py backend/requirements.txt README.MD
git commit -m "Add frontend health-check page and simple Flask backend for testing"
git push origin main
```

Notes:
- If frontend and backend are served from different origins, enable CORS appropriately on the backend or serve frontend from the same host.
- I added a minimal `backend/test_server.py` using Flask and `flask-cors` for easy testing.

