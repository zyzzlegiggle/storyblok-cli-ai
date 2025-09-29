package cmd

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"github.com/AlecAivazis/survey/v2"
	"github.com/schollz/progressbar/v3"
	"github.com/spf13/cobra"

	"storyblok-cli-ai/internal/scaffold"
)

var createAppCmd = &cobra.Command{
	Use:   "create",
	Short: "Scaffold a React + Storyblok app (AI-powered)",
	Long:  "Interactive wizard that scaffolds a React + Tailwind app integrated with Storyblok using the AI backend.",
	Run: func(cmd *cobra.Command, args []string) {
		if err := runCreateWizard(cmd); err != nil {
			printStructuredError(err)
			os.Exit(1)
		}
	},
}

func init() {
	createAppCmd.Flags().StringP("output", "o", "", "Output folder for the generated project (default: ./<app_name>)")
}

// ---------------- Cache helpers ----------------

func answersCachePath() string {
	home, _ := os.UserHomeDir()
	dir := filepath.Join(home, ".storyblok-ai-cli")
	_ = os.MkdirAll(dir, 0o755)
	return filepath.Join(dir, "answers.json")
}

func loadCachedAnswers() map[string]string {
	path := answersCachePath()
	out := map[string]string{}
	b, err := os.ReadFile(path)
	if err != nil {
		return out
	}
	_ = json.Unmarshal(b, &out)
	return out
}

func saveCachedAnswers(m map[string]string) error {
	path := answersCachePath()
	b, err := json.MarshalIndent(m, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(path, b, 0o600)
}

// ---------------- Network helper ----------------

func callBackend(backendURL string, payload map[string]interface{}) (map[string]interface{}, error) {
	body, _ := json.Marshal(payload)
	req, err := http.NewRequest("POST", backendURL, bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	client := &http.Client{Timeout: 180 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(resp.Body)
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, fmt.Errorf("backend returned status %d: %s", resp.StatusCode, string(b))
	}
	var parsed map[string]interface{}
	if err := json.Unmarshal(b, &parsed); err != nil {
		return nil, fmt.Errorf("failed to parse backend response: %w", err)
	}
	return parsed, nil
}

// ---------------- Streaming helper ----------------

// callBackendStream posts the payload to the /generate/stream endpoint and returns the http.Response (caller must close body)
func callBackendStream(backendURL string, payload map[string]interface{}) (*http.Response, error) {
	body, _ := json.Marshal(payload)
	req, err := http.NewRequest("POST", backendURL, bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	// no timeout to allow long streams; use a client with a long timeout
	client := &http.Client{Timeout: 0}
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		// read body for error message
		b, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		return nil, fmt.Errorf("backend returned status %d: %s", resp.StatusCode, string(b))
	}
	return resp, nil
}

// ---------------- Utilities ----------------

func slugify(s string) string {
	// lower, replace non-alnum with hyphen, collapse hyphens, trim
	s = strings.ToLower(strings.TrimSpace(s))
	re := regexp.MustCompile(`[^\p{L}\p{N}]+`)
	s = re.ReplaceAllString(s, "-")
	re2 := regexp.MustCompile(`-+`)
	s = re2.ReplaceAllString(s, "-")
	s = strings.Trim(s, "-")
	if s == "" {
		return "storyblok-app"
	}
	return s
}

// promptFollowupsAndCollect prompts the user for each followup item and returns answers map[id]=value
func promptFollowupsAndCollect(followups []map[string]interface{}) (map[string]string, error) {
	cache := loadCachedAnswers()
	answers := map[string]string{}

	for _, fu := range followups {
		idRaw, _ := fu["id"].(string)
		qid := idRaw
		if qid == "" {
			qid = fmt.Sprintf("q_%d", time.Now().UnixNano())
		}
		question, _ := fu["question"].(string)
		if question == "" {
			continue
		}
		defaultVal := ""
		if d, ok := fu["default"].(string); ok {
			defaultVal = d
		}
		// auto-fill from cache
		if cached, ok := cache[qid]; ok && cached != "" {
			defaultVal = cached
		}

		var resp string
		for {
			if err := survey.AskOne(&survey.Input{
				Message: question,
				Default: defaultVal,
			}, &resp); err != nil {
				return nil, err
			}
			resp = strings.TrimSpace(resp)
			// enforce non-empty answer (since you want natural text)
			if resp == "" {
				fmt.Println("Please provide a non-empty answer.")
				continue
			}
			break
		}

		answers[qid] = resp
		cache[qid] = resp
	}
	// save cache
	_ = saveCachedAnswers(cache)
	return answers, nil
}

// convert backend files array -> scaffold.FileOut slice
func parseFilesFromResponse(resp map[string]interface{}) []scaffold.FileOut {
	out := []scaffold.FileOut{}
	filesRaw, ok := resp["files"].([]interface{})
	if !ok {
		return out
	}
	for _, fr := range filesRaw {
		if fm, ok := fr.(map[string]interface{}); ok {
			path, _ := fm["path"].(string)
			content, _ := fm["content"].(string)
			out = append(out, scaffold.FileOut{Path: path, Content: content})
		}
	}
	return out
}

// ---------------- Helper: read stream events ----------------

type streamEvent struct {
	Event   string      `json:"event"`
	Payload interface{} `json:"payload"`
}

// read one JSON line (ndjson) from reader
func readJSONLine(r *bufio.Reader) ([]byte, error) {
	line, err := r.ReadBytes('\n')
	if err != nil {
		return nil, err
	}
	return bytes.TrimSpace(line), nil
}

// ---------------- Main wizard ----------------

func runCreateWizard(cmd *cobra.Command) error {
	// 1) Single freeform prompt + token prompt
	var description string
	var token string

	if err := survey.AskOne(&survey.Input{
		Message: "What would you like to create?",
		Help:    "Describe purpose, pages, features, and visual style. Be as natural-language as you like.",
	}, &description); err != nil {
		return fmt.Errorf("prompt aborted: %w", err)
	}
	description = strings.TrimSpace(description)

	if err := survey.AskOne(&survey.Input{
		Message: "Storyblok API token (optional, will be written to .env if provided):",
	}, &token); err != nil {
		return fmt.Errorf("token prompt aborted: %w", err)
	}
	token = strings.TrimSpace(token)

	// 2) Determine app name (slugify description). Allow user to edit name before proceeding.
	defaultName := slugify(description)
	var appName string
	if err := survey.AskOne(&survey.Input{
		Message: "Project name:",
		Default: defaultName,
	}, &appName); err != nil {
		return fmt.Errorf("project name prompt aborted: %w", err)
	}
	appName = strings.TrimSpace(appName)
	if appName == "" {
		appName = defaultName
	}

	// 3) Determine output dir (flag override allowed)
	outputFlag, _ := cmd.Flags().GetString("output")
	var targetDir string
	if outputFlag != "" {
		targetDir = outputFlag
	} else {
		targetDir = "./" + appName
	}
	absTarget, err := filepath.Abs(targetDir)
	if err != nil {
		return fmt.Errorf("invalid target path: %w", err)
	}

	// 4) Build initial payload
	payload := map[string]interface{}{
		"user_answers": map[string]interface{}{
			"description": description,
			"app_name":    appName,
			"token":       token,
		},
		"storyblok_schema": map[string]interface{}{}, // backend can fetch or ask for schema if needed
		"options": map[string]interface{}{
			"typescript":    true,
			"include_pages": true,
			"debug":         true,
		},
	}

	backendStreamURL := "http://127.0.0.1:8000/generate/stream"
	backendURL := "http://127.0.0.1:8000/generate/"

	// --- New: ask backend to generate structured requirements questions (guarantee at least one) ---
	questionPayload := map[string]interface{}{
		"user_answers":     payload["user_answers"],
		"storyblok_schema": payload["storyblok_schema"],
		"options": map[string]interface{}{
			"request_questions": true,
			"max_questions":     5,
			"debug":             payload["options"].(map[string]interface{})["debug"],
		},
	}

	qResp, err := callBackend(backendURL+"questions", questionPayload)
	if err != nil {
		// non-fatal: continue to main loop (backend may not be available)
		fmt.Fprintf(os.Stderr, "warning: question-generation failed: %v\n", err)
	} else {
		// extract top-level followups if any
		var initialFollowups []map[string]interface{}
		if fRaw, ok := qResp["followups"]; ok {
			if arr, ok := fRaw.([]interface{}); ok {
				for _, it := range arr {
					if s, ok := it.(string); ok && strings.TrimSpace(s) != "" {
						initialFollowups = append(initialFollowups, map[string]interface{}{"id": "", "question": s, "type": "text", "default": ""})
					}
				}
			}
		}
		// Short generic prompt fallback (if backend returned zero followups)
		if len(initialFollowups) == 0 {
			initialFollowups = []map[string]interface{}{
				{"id": "", "question": "Briefly describe the key requirements (pages, main features, visual style):", "type": "text", "default": ""},
			}
		}

		// prompt the user for these generated questions (auto-fill from cache)
		ansMap, err := promptFollowupsAndCollect(initialFollowups)
		if err != nil {
			return fmt.Errorf("aborted while answering initial requirements: %w", err)
		}

		// attach these initial answers to payload.user_answers.followup_answers
		userAns, _ := payload["user_answers"].(map[string]interface{})
		if userAns == nil {
			userAns = map[string]interface{}{}
		}
		existing := map[string]interface{}{}
		if fa, ok := userAns["followup_answers"].(map[string]interface{}); ok {
			existing = fa
		}
		for k, v := range ansMap {
			existing[k] = v
		}
		userAns["followup_answers"] = existing
		payload["user_answers"] = userAns
	}
	// --- end question-generation step ---

	// 5) followup loop
	maxRounds := 20
	for round := 0; round < maxRounds; round++ {
		// call streaming endpoint to get progress + files
		resp, err := callBackendStream(backendStreamURL, payload)
		if err != nil {
			// fallback to non-streaming behavior (older backend)
			respMap, err2 := callBackend(backendURL, payload)
			if err2 != nil {
				return fmt.Errorf("call backend (stream failed, fallback failed): %v / %v", err, err2)
			}
			// same behavior as before
			var followups []map[string]interface{}
			if fRaw, ok := respMap["followups"]; ok {
				if arr, ok := fRaw.([]interface{}); ok {
					for _, it := range arr {
						if m, ok := it.(map[string]interface{}); ok {
							followups = append(followups, m)
						} else if s, ok := it.(string); ok {
							followups = append(followups, map[string]interface{}{"id": "", "question": s, "type": "text", "default": ""})
						}
					}
				}
			}

			if len(followups) == 0 {
				files := parseFilesFromResponse(respMap)
				if len(files) == 0 {
					return fmt.Errorf("backend returned no files and no followups")
				}
				if err := scaffold.WriteFilesAtomically(files, absTarget); err != nil {
					return fmt.Errorf("writing project files: %w", err)
				}
				fmt.Println("Project created successfully at:", absTarget)
				fmt.Println("\n⚠️  Security note:")
				fmt.Println("  - A .env file containing your Storyblok token may have been written to the project root.")
				fmt.Println("  - Do NOT commit .env to source control. .gitignore includes .env by default.")
				return nil
			}

			// ask followups and continue
			answersMap, err := promptFollowupsAndCollect(followups)
			if err != nil {
				return fmt.Errorf("aborted while answering followups: %w", err)
			}
			userAns, _ := payload["user_answers"].(map[string]interface{})
			if userAns == nil {
				userAns = map[string]interface{}{}
			}
			existing := map[string]string{}
			if fa, ok := userAns["followup_answers"].(map[string]string); ok {
				existing = fa
			} else if fa2, ok := userAns["followup_answers"].(map[string]interface{}); ok {
				for k, v := range fa2 {
					if s, ok := v.(string); ok {
						existing[k] = s
					}
				}
			}
			for k, v := range answersMap {
				existing[k] = v
			}
			faInterface := map[string]interface{}{}
			for k, v := range existing {
				faInterface[k] = v
			}
			userAns["followup_answers"] = faInterface
			payload["user_answers"] = userAns
			continue
		}

		// Stream reader
		reader := bufio.NewReader(resp.Body)

		// temp dir to store files as they stream
		tmpDir, _ := os.MkdirTemp("", "ai_stream_*")
		defer os.RemoveAll(tmpDir)
		// map path -> temp file path
		tempFiles := map[string]string{}
		// set to collect completed files for final atomic write
		completedFiles := []scaffold.FileOut{}

		// progress bar (indeterminate until finished)
		var pb *progressbar.ProgressBar
		generatedCount := 0

		handleFollowups := func(followupsIface interface{}) (bool, error) {
			// convert to []map[string]interface{}
			out := []map[string]interface{}{}
			if arr, ok := followupsIface.([]interface{}); ok {
				for _, it := range arr {
					if m, ok := it.(map[string]interface{}); ok {
						out = append(out, m)
					} else if s, ok := it.(string); ok {
						out = append(out, map[string]interface{}{"id": "", "question": s, "type": "text", "default": ""})
					}
				}
			}
			if len(out) == 0 {
				return false, nil
			}
			// close stream body and prompt user
			_ = resp.Body.Close()
			ansMap, err := promptFollowupsAndCollect(out)
			if err != nil {
				return false, err
			}
			// attach answers and break to outer loop
			userAns, _ := payload["user_answers"].(map[string]interface{})
			if userAns == nil {
				userAns = map[string]interface{}{}
			}
			existing := map[string]string{}
			if fa, ok := userAns["followup_answers"].(map[string]string); ok {
				existing = fa
			} else if fa2, ok := userAns["followup_answers"].(map[string]interface{}); ok {
				for k, v := range fa2 {
					if s, ok := v.(string); ok {
						existing[k] = s
					}
				}
			}
			for k, v := range ansMap {
				existing[k] = v
			}
			faInterface := map[string]interface{}{}
			for k, v := range existing {
				faInterface[k] = v
			}
			userAns["followup_answers"] = faInterface
			payload["user_answers"] = userAns
			return true, nil
		}

		// read loop
		for {
			lineBytes, err := readJSONLine(reader)
			if err != nil {
				if err == io.EOF {
					break
				}
				// close and return on other errors
				_ = resp.Body.Close()
				return fmt.Errorf("error reading stream: %w", err)
			}
			var ev map[string]interface{}
			if err := json.Unmarshal(lineBytes, &ev); err != nil {
				// ignore malformed line
				continue
			}
			etype, _ := ev["event"].(string)
			payloadEv := ev["payload"]

			switch etype {
			case "followups":
				// backend requests clarifying questions: prompt user then continue outer loop
				shouldContinue, err := handleFollowups(payloadEv)
				if err != nil {
					return fmt.Errorf("error while handling followups: %w", err)
				}
				if shouldContinue {
					// break reading and restart outer followup loop
					break
				}
			case "file_start":
				m, _ := payloadEv.(map[string]interface{})
				path, _ := m["path"].(string)
				// create temp file to append chunks
				tf := filepath.Join(tmpDir, strings.ReplaceAll(path, "/", "__"))
				_ = os.MkdirAll(filepath.Dir(tf), 0o755)
				// ensure file exists (trunc)
				_ = os.WriteFile(tf, []byte(""), 0o644)
				tempFiles[path] = tf
			case "file_chunk":
				m, _ := payloadEv.(map[string]interface{})
				path, _ := m["path"].(string)
				chunk, _ := m["chunk"].(string)
				final, _ := m["final"].(bool)
				tf, ok := tempFiles[path]
				if !ok {
					// create if not present
					tf = filepath.Join(tmpDir, strings.ReplaceAll(path, "/", "__"))
					_ = os.WriteFile(tf, []byte(""), 0o644)
					tempFiles[path] = tf
				}
				// append chunk
				f, ferr := os.OpenFile(tf, os.O_APPEND|os.O_WRONLY, 0o644)
				if ferr == nil {
					_, _ = f.WriteString(chunk)
					f.Close()
				}
				_ = final // nothing now
			case "dependency":
				if m, ok := payloadEv.(map[string]interface{}); ok {
					name, _ := m["name"].(string)
					version, _ := m["version"].(string)
					conf, _ := m["confidence"].(float64)
					if version != "" {
						fmt.Printf("Resolved: %s@%s (confidence %.2f)\n", name, version, conf)
					} else {
						// print candidate summary if available
						if cands, ok := m["candidates"].([]interface{}); ok && len(cands) > 0 {
							fmt.Printf("Dependency not found: %s — suggested: %v\n", name, cands)
						} else {
							fmt.Printf("Dependency not found: %s\n", name)
						}
					}
				}

			case "file_complete":
				m, _ := payloadEv.(map[string]interface{})
				path, _ := m["path"].(string)
				tf := tempFiles[path]

				// read temp file into memory
				contentBytes, _ := os.ReadFile(tf)
				content := string(contentBytes)

				// run formatter on the temp file (in place)
				if err := runFormatterForFile(tf); err == nil {
					// re-read file after formatting
					if newBytes, err2 := os.ReadFile(tf); err2 == nil {
						content = string(newBytes)
					}
				}

				completedFiles = append(completedFiles, scaffold.FileOut{
					Path:    path,
					Content: content,
				})
				generatedCount += 1

				// initialize progress bar if needed
				if pb == nil {
					pb = progressbar.NewOptions(-1,
						progressbar.OptionSetDescription("Generating files"),
						progressbar.OptionShowCount(),
						progressbar.OptionSpinnerType(14),
					)
				}
				_ = pb.Add(1)

			case "warning":
				// print warnings
				if s, ok := payloadEv.(string); ok {
					fmt.Printf("\nWARNING: %s\n", s)
				} else {
					bs, _ := json.Marshal(payloadEv)
					fmt.Printf("\nWARNING: %s\n", string(bs))
				}
			case "done":
				// final event; break reading
				// finish progress bar if exists
				if pb != nil {
					_ = pb.Finish()
				}
				break
			default:
				// ignore other events (dependency/validation intentionally ignored)
			}

			// continue reading until done or followups
		}

		// ensure final newline for progress if progress bar not used
		if pb == nil {
			fmt.Printf("\n")
		}

		// close response body now
		_ = resp.Body.Close()

		// If followups were delivered and we handled them, continue outer loop
		// (we detect this because payload["user_answers"] will have updated followup_answers)
		// Check if there are followup answers present and no completedFiles - then skip finalize
		if len(completedFiles) == 0 {
			// continue to next round (likely after prompting followups)
			// loop will restart and call backend again with updated payload
			continue
		}

		// final: write files atomically from completedFiles
		if err := scaffold.WriteFilesAtomically(completedFiles, absTarget); err != nil {
			return fmt.Errorf("writing project files: %w", err)
		}

		fmt.Println("Project created successfully at:", absTarget)
		pkgPath := filepath.Join(absTarget, "package.json")
		if _, err := os.Stat(pkgPath); err == nil {

		} else {
			fmt.Println("No package.json found — skipping Storyblok types generation.")
		}
		fmt.Println("\n⚠️  Security note:")
		fmt.Println("  - A .env file containing your Storyblok token may have been written to the project root.")
		fmt.Println("  - Do NOT commit .env to source control. .gitignore includes .env by default.")
		return nil
	}

	return fmt.Errorf("maximum followup rounds reached (%d). Aborting", maxRounds)
}

// ---------------- helpers ----------------

func printStructuredError(err error) {
	type Out struct {
		Error   string `json:"error"`
		Message string `json:"message,omitempty"`
	}
	out := Out{
		Error:   "create-app-failed",
		Message: err.Error(),
	}
	b, _ := json.MarshalIndent(out, "", "  ")
	fmt.Fprintln(os.Stderr, string(b))
}

func runFormatterForFile(path string) error {
	ext := strings.ToLower(filepath.Ext(path))
	switch ext {
	case ".ts", ".tsx", ".js", ".jsx", ".json", ".css", ".html", ".md":
		if _, err := exec.LookPath("npx"); err == nil {
			// prettier via npx; --yes so it doesn't prompt
			cmd := exec.Command("npx", "--yes", "prettier", "--write", path)
			cmd.Stdout = os.Stdout
			cmd.Stderr = os.Stderr
			return cmd.Run()
		}
	case ".py":
		if _, err := exec.LookPath("black"); err == nil {
			cmd := exec.Command("black", path)
			cmd.Stdout = os.Stdout
			cmd.Stderr = os.Stderr
			return cmd.Run()
		}
	case ".go":
		if _, err := exec.LookPath("gofmt"); err == nil {
			cmd := exec.Command("gofmt", "-w", path)
			cmd.Stdout = os.Stdout
			cmd.Stderr = os.Stderr
			return cmd.Run()
		}
	}
	return nil
}
