package cmd

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"io/ioutil"
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
		// ---- Storyblok CLI integration (requirement, with validation) ----
		// if err := scaffold.EnsureStoryblokCLI(); err != nil {
		// 	printStructuredError(err)
		// 	os.Exit(1)
		// }

		// // Attempt to read existing credentials
		// creds, cErr := readStoryblokCredentials()
		// if cErr != nil {
		// 	// warn but continue to interactive login
		// 	fmt.Fprintf(os.Stderr, "warning: failed to read Storyblok credentials: %v\n", cErr)
		// }

		// // Validate existing credentials if present, otherwise run login.
		// maxLoginAttempts := 2
		// attempt := 0
		// credsValid := false

		// for attempt < maxLoginAttempts {
		// 	attempt++

		// 	if creds == nil {
		// 		fmt.Println("No Storyblok credentials found. Launching 'storyblok login' (interactive).")
		// 		if err := runStoryblokLogin(); err != nil {
		// 			// login failed to run (process error)
		// 			printStructuredError(fmt.Errorf("storyblok login failed: %w", err))
		// 			os.Exit(1)
		// 		}
		// 		// re-read creds after login
		// 		creds, _ = readStoryblokCredentials()
		// 		if creds == nil {
		// 			// credentials file still missing; try again or abort
		// 			fmt.Fprintf(os.Stderr, "storyblok credentials not found after login attempt %d\n", attempt)
		// 			if attempt >= maxLoginAttempts {
		// 				printStructuredError(fmt.Errorf("storyblok credentials not found after %d attempts; aborting", maxLoginAttempts))
		// 				os.Exit(1)
		// 			}
		// 			// loop to attempt login again
		// 			continue
		// 		}
		// 	}

		// 	// If we have credentials, validate by calling `storyblok user`
		// 	if err := validateStoryblokAuth(); err != nil {
		// 		fmt.Fprintf(os.Stderr, "Storyblok credentials appear invalid: %v\n", err)
		// 		// if we still have attempts left, prompt login
		// 		if attempt < maxLoginAttempts {
		// 			fmt.Println("Attempting interactive 'storyblok login' to refresh credentials...")
		// 			if err := runStoryblokLogin(); err != nil {
		// 				printStructuredError(fmt.Errorf("storyblok login failed: %w", err))
		// 				os.Exit(1)
		// 			}
		// 			// re-read credentials after login and re-validate on next loop iteration
		// 			creds, _ = readStoryblokCredentials()
		// 			continue
		// 		}
		// 		// no attempts left -> abort
		// 		printStructuredError(fmt.Errorf("storyblok credentials invalid after %d attempts; aborting", maxLoginAttempts))
		// 		os.Exit(1)
		// 	}

		// 	// success
		// 	credsValid = true
		// 	break
		// }

		// if !credsValid {
		// 	printStructuredError(fmt.Errorf("failed to validate Storyblok credentials; aborting"))
		// 	os.Exit(1)
		// }

		// // Now attempt to pull components into a temp file and use as schema
		// storyblokSchema, cleanupSchema := map[string]interface{}{}, func() {}
		// if schema, cleanup, err := pullStoryblokComponentsTemp(); err == nil {
		// 	storyblokSchema = schema
		// 	cleanupSchema = cleanup
		// 	fmt.Println("Pulled Storyblok components schema and will use it for scaffolding.")
		// } else {
		// 	fmt.Fprintf(os.Stderr, "warning: failed to pull Storyblok components: %v\n", err)
		// 	// continue with empty schema (backend will ask followups)
		// }
		// // ensure temp schema cleanup when runCreateWizard returns
		// defer cleanupSchema()
		// // ---- end Storyblok CLI integration ----


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
	// ---- Storyblok CLI integration (requirement) ----
	if err := ensureStoryblokInstalled(); err != nil {
		return fmt.Errorf("storyblok CLI required but missing: %w", err)
	}

	creds, cErr := readStoryblokCredentials()
	if cErr != nil {
		// we can still attempt interactive login
		fmt.Fprintf(os.Stderr, "warning: failed to read Storyblok credentials: %v\n", cErr)
	}

	if creds == nil {
		fmt.Println("No Storyblok credentials found. Launching 'storyblok login' (interactive).")
		if err := runStoryblokLogin(); err != nil {
			return fmt.Errorf("storyblok login failed: %w", err)
		}
		// attempt to read credentials again
		creds, _ = readStoryblokCredentials()
		if creds == nil {
			// still none — abort because we require the CLI for this flow
			return fmt.Errorf("storyblok credentials not found after login; aborting")
		}
	}
	
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

	qResp, err := callBackend(backendURL, questionPayload)
	if err != nil {
		// non-fatal: continue to main loop (backend may not be available)
		fmt.Fprintf(os.Stderr, "warning: question-generation failed: %v\n", err)
	} else {
		// extract top-level followups if any
		var initialFollowups []map[string]interface{}
		if fRaw, ok := qResp["followups"]; ok {
			if arr, ok := fRaw.([]interface{}); ok {
				for _, it := range arr {
					if m, ok := it.(map[string]interface{}); ok {
						initialFollowups = append(initialFollowups, m)
					} else if s, ok := it.(string); ok {
						initialFollowups = append(initialFollowups, map[string]interface{}{"id": "", "question": s, "type": "text", "default": ""})
					}
				}
			}
		}
		// fallback: if backend didn't return followups, ask a generic question locally
		if len(initialFollowups) == 0 {
			initialFollowups = []map[string]interface{}{
				{"id": "", "question": "Please list the key requirements for the app (pages, main features, and visual style).", "type": "text", "default": ""},
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
				fmt.Println("  - If you prefer not to store the token, remove .env and set VITE_STORYBLOK_TOKEN in your environment.")
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
			case "file_complete":
				m, _ := payloadEv.(map[string]interface{})
				path, _ := m["path"].(string)
				tf := tempFiles[path]
				// read temp file into memory
				contentBytes, _ := os.ReadFile(tf)
				content := string(contentBytes)
				completedFiles = append(completedFiles, scaffold.FileOut{Path: path, Content: content})
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
			fmt.Println("Detected package.json — generating Storyblok TypeScript types with 'storyblok types generate' ...")
			if err := runStoryblokTypesGenerate(absTarget); err != nil {
				fmt.Fprintf(os.Stderr, "warning: storyblok types generate failed: %v\n", err)
			} else {
				fmt.Println("Storyblok TypeScript types generated successfully.")
			}
		} else {
			fmt.Println("No package.json found — skipping Storyblok types generation.")
		}
		fmt.Println("\n⚠️  Security note:")
		fmt.Println("  - A .env file containing your Storyblok token may have been written to the project root.")
		fmt.Println("  - Do NOT commit .env to source control. .gitignore includes .env by default.")
		fmt.Println("  - If you prefer not to store the token, remove .env and set VITE_STORYBLOK_TOKEN in your environment.")
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


// ensureStoryblokInstalled returns nil if `storyblok` is available on PATH; otherwise returns an error
func ensureStoryblokInstalled() error {
	_, err := exec.LookPath("storyblok")
	if err != nil {
		return fmt.Errorf("storyblok CLI not found on PATH. Install via: npm install -g storyblok@beta (or see https://www.storyblok.com)")
	}
	return nil
}

// readStoryblokCredentials attempts to read ~/.storyblok/credentials.json and returns parsed JSON map
// if the file doesn't exist, returns (nil, nil)
func readStoryblokCredentials() (map[string]interface{}, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return nil, err
	}
	credPath := filepath.Join(home, ".storyblok", "credentials.json")
	if _, err := os.Stat(credPath); os.IsNotExist(err) {
		return nil, nil
	}
	b, err := ioutil.ReadFile(credPath)
	if err != nil {
		return nil, err
	}
	var m map[string]interface{}
	if err := json.Unmarshal(b, &m); err != nil {
		return nil, err
	}
	return m, nil
}

// runStoryblokLogin runs `storyblok login` interactively (attached to current stdio)
func runStoryblokLogin() error {
	cmd := exec.Command("storyblok", "login")
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	cmd.Stdin = os.Stdin
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("failed to run 'storyblok login': %w", err)
	}
	// small delay giving CLI time to write credentials
	time.Sleep(300 * time.Millisecond)
	return nil
}

// pullStoryblokComponentsTemp runs `storyblok components pull` in a temp dir, ensuring login first.
// Returns parsed JSON schema, a cleanup func to remove temp dir, or error.
func pullStoryblokComponentsTemp() (map[string]interface{}, func(), error) {
	// Ensure we are authenticated (try validateStoryblokAuth, if fails, run login once)
	if err := validateStoryblokAuth(); err != nil {
		// not authenticated — run interactive login
		fmt.Println("Storyblok CLI not authenticated. Launching 'storyblok login' (interactive).")
		if err2 := runStoryblokLogin(); err2 != nil {
			return nil, func() {}, fmt.Errorf("login failed: %w", err2)
		}
		// re-validate
		if err3 := validateStoryblokAuth(); err3 != nil {
			return nil, func() {}, fmt.Errorf("authentication failed after login: %w", err3)
		}
	}

	// Create temp dir and run the pull command inside it so .storyblok/ is created there
	tmpDir, err := ioutil.TempDir("", "storyblok_components_*")
	if err != nil {
		return nil, func() {}, fmt.Errorf("failed to create temp dir: %w", err)
	}
	cleanup := func() { _ = os.RemoveAll(tmpDir) }

	cmd := exec.Command("storyblok", "components", "pull")
	cmd.Dir = tmpDir
	// attach stdio so the command can be interactive / show progress if needed
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	cmd.Stdin = os.Stdin

	if err := cmd.Run(); err != nil {
		// on failure, clean up and return the error
		cleanup()
		return nil, func() {}, fmt.Errorf("storyblok components pull failed: %w", err)
	}

	// find the generated file under tmpDir/.storyblok/
	globPattern := filepath.Join(tmpDir, ".storyblok", "components.*.json")
	matches, gerr := filepath.Glob(globPattern)
	if gerr != nil || len(matches) == 0 {
		// try to read entire .storyblok dir for debugging
		_ = filepath.Walk(filepath.Join(tmpDir, ".storyblok"), func(p string, info os.FileInfo, e error) error {
			if e == nil {
				fmt.Fprintf(os.Stderr, "found file while scanning: %s\n", p)
			}
			return nil
		})
		cleanup()
		return nil, func() {}, fmt.Errorf("could not find pulled components file (expected pattern %s)", globPattern)
	}

	// read the first match
	b, err := os.ReadFile(matches[0])
	if err != nil {
		cleanup()
		return nil, func() {}, fmt.Errorf("failed to read components file: %w", err)
	}

	var schema map[string]interface{}
	if err := json.Unmarshal(b, &schema); err != nil {
		cleanup()
		return nil, func() {}, fmt.Errorf("failed to parse components JSON: %w", err)
	}

	return schema, cleanup, nil
}


// runStoryblokTypesGenerate runs `storyblok types generate` in the given project directory
// and streams output to stdout/stderr. Caller receives error if command fails.
func runStoryblokTypesGenerate(projectDir string) error {
	cmd := exec.Command("storyblok", "types", "generate")
	cmd.Dir = projectDir
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	// attach stdin for safety
	cmd.Stdin = os.Stdin
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("storyblok types generate failed: %w", err)
	}
	return nil
}

// validateStoryblokAuth runs a lightweight storyblok command to verify auth works.
// It returns nil if credentials are valid, otherwise an error.
func validateStoryblokAuth() error {
	// `storyblok user` returns info about the current user when logged in; it fails if not authenticated.
	cmd := exec.Command("storyblok", "user")
	// We don't need to attach stdin; capture output for debugging
	out, err := cmd.CombinedOutput()
	if err != nil {
		// include output to aid debugging
		return fmt.Errorf("storyblok auth validation failed: %v - output: %s", err, strings.TrimSpace(string(out)))
	}
	// If command succeeded, assume credentials are valid
	return nil
}
