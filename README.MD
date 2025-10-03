
---

# Storyblok CLI AI

An **AI-powered developer toolchain** to scaffold Storyblok-powered frontend apps with an **interactive CLI wizard** and an **AI backend server**.

This project consists of two main components:

* **CLI (Go)** — interactive command-line tool that prompts the user for requirements and scaffolds a project.
* **Server (FastAPI + LangChain + Google Gemini)** — backend service that generates code, follow-up questions, and dependencies for the CLI.

---

## ✨ Features

### CLI (Go)

* Interactive wizard with follow-up questions.
* Scaffolds apps using `npx @storyblok/create-demo` (React, Vue, Next.js, etc.).
* Merges AI-generated files into the scaffold.
* Streams generation results from backend.
* Supports caching of answers and environment-aware configuration.

### Server (FastAPI + LangChain + Gemini)

* Generates structured code files and dependencies.
* Asks clarifying follow-up questions dynamically.
* Ensures structured JSON responses.
* Resilient parsing and urgency-based question prioritization.

---

## ⚙️ Setup

### Prerequisites

* **Go** 1.20+
* **Node.js** + `npx`
* `npm` or `yarn`
* **Python 3.10+** and `pip`
* Google Gemini API key

---

### 1. Install & Run the Server

```bash
cd server
pip install -r requirements.txt
```

Create `.env`:

```env
GOOGLE_API_KEY_GEMINI=your_gemini_api_key_here
AI_SERVER_LOG_DIR=./ai_server_logs
```

Run server:

```bash
fastapi dev app/main.py
```

Server runs on [http://127.0.0.1:8000](http://127.0.0.1:8000).

---

### 2. Install & Run the CLI

```bash
cd cli
go build -o storyblok-cli ./
./storyblok-cli create
```

run directly:

```bash
go run ./main.go create
```

#### Download Release (Windows)

Download the prebuilt binary from the [Releases page](https://github.com/zyzzlegiggle/storyblok-cli-ai/releases/tag/releaseV2) and run:

```bash
./storyblok-cli-ai.exe create

---

## 🧩 API Overview (Server)

### `POST /generate`

Generate project files and dependencies.

```json
{
  "project_name": "my-app",
  "files": [
    {"path": "src/App.tsx", "content": "export default function App(){ return <div>Hello</div> }"}
  ],
  "dependencies": ["react", "storyblok-js-client"],
  "followups": []
}
```

### `POST /followups`

Generate follow-up questions.

```json
{
  "followups": [
    {"id": "pages", "question": "Which pages do you need?", "urgency": 0.9}
  ]
}
```

### `POST /generate/stream`

Stream NDJSON responses for progressive generation (used by CLI).

---

## 🚀 Usage (End-to-End)

```bash
./storyblok-cli create
```

You will be prompted for:

* Freeform description
* Storyblok API token
* Project name
* Framework (`astro`, `react`, `nextjs`, `sveltekit`, `vue`, `nuxt`)
* Package manager (`npm`/`yarn`)
* Space region (EU/US/CN/CA)

The CLI:

1. Runs `npx @storyblok/create-demo`
2. Collects scaffold files
3. Calls AI backend for generated code & follow-ups
4. Streams incremental files & merges into project
5. Installs dependencies

---

## 🛠️ Troubleshooting

* **`npx` issues** → Ensure `node` & `npx` installed.
* **Backend errors** → Confirm server at `http://127.0.0.1:8000/`.
* **Formatter skipped** → Install `prettier`, `black`, or `gofmt`.
* **Dir already exists** → Use another `--output`.

---

## 🔒 Security Notes

* API token stored in `.env` (auto-added to `.gitignore`).
* Cached answers stored locally at `~/.storyblok-ai-cli/answers.json` with restricted permissions.

---

## 🤝 Contributing

* Keep PRs small and focused
* Add tests if possible
* Update README when behavior changes
---

## 🙌 Acknowledgements

* [spf13/cobra](https://github.com/spf13/cobra) (CLI framework)
* [AlecAivazis/survey](https://github.com/AlecAivazis/survey) (interactive prompts)
* [Storyblok create-demo](https://www.storyblok.com/) (scaffolding)
* [LangChain](https://www.langchain.com/) + Google Gemini (AI backend)

---
