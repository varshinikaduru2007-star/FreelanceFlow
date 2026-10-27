FreelanceFlow — Complete Website
================================

This folder combines the frontend and backend. FastAPI serves index.html and handles the API from the same address.

SETUP (Windows)
1. Extract the ZIP to a folder, for example Desktop\FreelanceFlow_Website.
2. Open that folder in File Explorer. Click the address bar, type cmd, and press Enter.
3. Install dependencies:
   py -m pip install -r requirements.txt
4. Start the website server:
   py -m uvicorn main:app --reload
5. Open this address in your browser:
   http://127.0.0.1:8000

The first run automatically creates freelancer.db in this folder. If you already have an older freelancer.db, copy it into this folder before starting the server; keep a backup first. Do not overwrite your only copy.

AI ASSISTANT (optional)
The AI assistant uses Ollama locally. Install/run Ollama and make sure the model is available:
   ollama run llama3.2:latest
Keep the Ollama window running while using the AI assistant. The rest of the website can be used without Ollama.

API documentation (optional): http://127.0.0.1:8000/docs
Stop the server by pressing Ctrl+C in the terminal.

NOTE: This is a recreated backend based on the available frontend API calls, not a recovery of the original deleted backend. Test your features and keep database backups.
