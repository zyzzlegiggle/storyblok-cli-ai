# storyblok-cli-ai

AI-powered CLI to scaffold React + Storyblok demo apps with an iterative, interactive wizard.

This repository provides a `cobra`-based Go CLI (entrypoint `main.go`) that drives a local/remote AI backend to generate project files, iteratively asks clarifying questions, runs the Storyblok `create-demo` generator, and optionally installs dependencies.

---

## Quick overview

* Language: Go
* CLI framework: [spf13/cobra]
* Interactive prompts: `github.com/AlecAivazis/survey/v2`
* Streaming progress: reads NDJSON stream from an AI backend `/generate/stream` endpoint
* Scaffolding: runs `npx @storyblok/create-demo` to generate starter app files, then merges with AI-generated content

Main commands:

* `storyblok-cli create` — interactive scaffold wizard (primary functionality)

Files of interest:

* `main.go` — CLI entrypoint
* `cmd/root.go` — cobra root command + registration
* `cmd/create_app.go` — the interactive wizard, backend client, streaming logic
* `internal/scaffold` — utilities to read/write and merge generated files (used by the wizard)

---

## Prerequisites

Make sure your environment has the following installed:

* Go (1.20+ recommended)
* Node.js and `npx` (used to run `@storyblok/create-demo`)
* `npm` or `yarn` (used to install project dependencies after scaffold)
* A running AI backend that implements the following HTTP endpoints (used by the CLI):

  * `POST http://127.0.0.1:8000/generate/` — synchronous generation & followups
  * `POST http://127.0.0.1:8000/generate/stream` — streaming NDJSON generation

The CLI currently uses `http://127.0.0.1:8000/` as the default backend host; see "Configuration" below if you want to change it.

---

## Installation (developer)

Clone the repository and build:

```bash
git clone <your-repo-url>
cd storyblok-cli-ai
# Ensure module name matches imports or adjust `import "storyblok-cli-ai/cmd"` in main.go
go build -o storyblok-cli ./
```

Or run directly during development:

```bash
go run ./main.go create
```

---

## Usage

Basic interactive run:

```bash
./storyblok-cli create
```

Flags

* `-o, --output` — set output folder for the generated project. Default: `./<app_name>`
* `-v, --verbose` — enables verbose logging (root persistent flag)

Example (non-interactive flags while still prompting for required answers):

```bash
./storyblok-cli create -o ./my-app
```

During the wizard you will be prompted for:

* What you want to create (freeform description)
* Your Storyblok API token
* Project name (default is slugified description)
* Framework choice (one of `astro`, `react`, `nextjs`, `sveltekit`, `vue`, `nuxt`)
* Package manager choice (`npm` or `yarn`)
* Space region (EU/US/CN/CA)

The CLI will call the Storyblok `create-demo` generator using `npx`, then send the base files to the AI backend for iterative generation and merging.

---

## Configuration

### Backend host and endpoints

The backend endpoints are hardcoded in `cmd/create_app.go` as:

```go
backendStreamURL := "http://127.0.0.1:8000/generate/stream"
backendURL := "http://127.0.0.1:8000/generate/"
```

If you need to point the CLI at a different backend, modify those variables in `create_app.go` or provide a small wrapper to set them via environment variables before compiling.

### Cache

The CLI caches follow-up answers locally in:

```
~/.storyblok-ai-cli/answers.json
```

This cache is used to auto-fill previously provided values across runs.

---

## Security & privacy notes

* The wizard asks for your Storyblok API token and (by default) will store a `.env` file in the generated project root containing that token. **Do not commit `.env`** to source control. The scaffold includes `.gitignore` entries to ignore `.env` by default, but please verify.
* Cached answers are stored in `~/.storyblok-ai-cli/answers.json` with file permissions set to `0600` where possible.

---

## How the wizard works (high-level)

1. Prompt the user for a description and Storyblok token.
2. Slugify the project name and ask for a final project name and output directory.
3. Run `npx @storyblok/create-demo` to bootstrap a base scaffold into the selected folder.
4. Walk the scaffolded folder and collect non-binary files (assets are tracked separately).
5. Send base files + user answers to the AI backend; the backend will return follow-up questions and/or generated files.
6. The CLI supports iterative rounds of follow-ups (to clarify requirements) and will stream incremental file contents from `/generate/stream`.
7. Received files are written to a temporary location, optionally formatted (Prettier, Black, gofmt when available), and then merged atomically into the target project folder.
8. Optionally runs `npm install` or `yarn install` in the created project folder to install dependencies.

---

## Troubleshooting

### `npx` / `@storyblok/create-demo` failures

* Ensure `node` and `npx` are on your `PATH`.
* Verify you can run `npx @storyblok/create-demo --help` from your terminal.
* If running behind a corporate proxy, ensure `npm` is configured to use the proxy and `npx` can download packages.

### Backend connection / streaming errors

* Confirm the backend is reachable at `http://127.0.0.1:8000/` (or change the URL in `create_app.go`).
* For streaming, the CLI uses `POST /generate/stream` and expects NDJSON lines with `event` + `payload` fields. If the backend returns non-2xx status codes, they are surfaced as errors.

### Dependency installation fails

* If `npm install` or `yarn install` fails, the CLI prints a helpful message and the project is still left on disk. You can manually run `cd <project> && npm install` or `yarn install`.

### Formatter not running

* The CLI attempts to run formatting tools (`npx prettier`, `black`, `gofmt`) if they're available on `PATH`. Install them if you'd like automatic formatting of received files.

### Target directory already exists

* The CLI refuses to overwrite an existing directory. Remove the directory or choose a different `--output` or project name.

---

## Developer notes / extending

* `cmd/create_app.go` contains the core logic for:

  * prompting & caching answers
  * invoking Storyblok's `create-demo`
  * calling the AI backend (sync & streaming)
  * assembling and writing files
* `internal/scaffold` handles file-write semantics; examine it to change merge behavior or to add templating hooks.
* To change backend host/endpoints, either edit `create_app.go` or refactor the code to read from environment variables.

### Allowed frameworks & regions

The interactive prompt currently restricts frameworks to:

```
astro, react, nextjs, sveltekit, vue, nuxt
```

Regions exposed in the prompt map to Storyblok region codes:

* `EU - Europe` → `eu-central-1` (default)
* `US - United States` → `us-east-1`
* `CN - China` → `cn-north-1`
* `CA - Canada` → `ca-central-1`

---

## Example session (short)

```text
$ ./storyblok-cli create
? What would you like to create? A marketing site with blog + product pages
? Storyblok API token: ****
? Project name: marketing-site
? Choose a base framework for the Storyblok demo scaffold: react
? Choose package manager for the scaffold: npm
? Space Region (optional, EU is used by default): EU - Europe

# CLI will run `npx @storyblok/create-demo` and then contact the AI backend to generate additional files.
```

---

## Contributing

Contributions are welcome — please open issues or PRs. When opening PRs:

* Keep changes small and focused
* Add tests where appropriate
* Update this README when behavior or defaults change

---

## License

MIT — feel free to adapt and reuse. Put your own license file in the repository if you prefer a different license.

---

## Acknowledgements

* Built with `cobra`, `survey`, and a streaming NDJSON protocol for progressive generation.
* Uses `@storyblok/create-demo` to bootstrap Storyblok demo projects.

---
