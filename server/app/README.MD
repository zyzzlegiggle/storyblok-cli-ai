

---

# Storyblok CLI AI Server

This server powers a **Go-based CLI** that generates frontend scaffolds connected to [Storyblok](https://www.storyblok.com/).
It uses **FastAPI**, **LangChain**, and **Google Gemini** to generate project code, dependencies, and follow-up questions that clarify user requirements.

---

## 🚀 Features

* **Code Generation**

  * Generates frontend project files (React, Vue, etc.) based on user requirements.
  * Outputs structured JSON with `files`, `dependencies`, and `metadata`.

* **Follow-up Question Agent**

  * Dynamically asks clarifying questions to refine project requirements.
  * Avoids repeating questions and uses urgency scoring.

* **LLM Integration**

  * Uses **Google Gemini** via `langchain-google-genai`.
  * Strictly enforces JSON-only structured responses.

* **CLI-First Workflow**

  * Go CLI collects initial input (framework, Storyblok API token, region).
  * server guides users with follow-ups until enough details exist to scaffold the project.

---

## 📂 Project Structure

```
ai_server_demo/
│
├── app/
│   ├── core/
│   │   ├── followup_agent.py   # follow-up question generator
│   │   ├── llm_client.py       # Gemini client (LangChain wrapper)
│   │   ├── prompts.py          # system & user prompts
│   │
│   ├── main.py                 # FastAPI entrypoint
│   └── ...
│
├── requirements.txt
└── README.md
```

---

## ⚙️ Setup

### 1. Clone the repo

```bash
git clone https://github.com/your-org/ai-server-demo.git
cd ai-server-demo
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure environment

Create a `.env` file with:

```env
GOOGLE_API_KEY_GEMINI=your_gemini_api_key_here
AI_server_LOG_DIR=./ai_server_logs
```

### 4. Run the FastAPI server

```bash
fastapi dev main.py
```

server will be available at:
👉 [http://localhost:8000](http://localhost:8000)

---

## 🧩 API Overview

### Generate Code

**Endpoint:** `POST /generate`
**Input:** JSON payload with user requirements & options.
**Output:**

```json
{
  "project_name": "my-app",
  "files": [
    {"path": "src/App.tsx", "content": "export default function App(){ return <div>Hello</div> }"}
  ],
  "dependencies": ["react", "storyblok-js-client"],
  "metadata": {
    "warnings": [],
    "validation": {"checked": false, "ok": null, "output": null},
    "followups": []
  },
  "followups": []
}
```

---

### Generate Follow-up Questions

**Endpoint:** `POST /followups`
**Input:** JSON with `user_answers` and `options`.
**Output:**

```json
{
  "followups": [
    {"id": "pages", "question": "Which pages do you need?", "urgency": 0.9},
    {"id": "style", "question": "Do you have a preferred theme or color scheme?", "urgency": 0.7}
  ]
}
```

---

## 🛠️ Development

* Logs are saved under `./ai_server_logs`
* Debug mode can be enabled per-request with `options.debug=true`.
* Fallback parsing ensures resilience if the model outputs malformed JSON.

---
