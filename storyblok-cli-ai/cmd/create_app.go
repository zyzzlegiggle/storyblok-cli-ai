package cmd

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"github.com/AlecAivazis/survey/v2"
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
		if err := survey.AskOne(&survey.Input{
			Message: question,
			Default: defaultVal,
		}, &resp); err != nil {
			return nil, err
		}
		resp = strings.TrimSpace(resp)
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
			"debug":         true, // first run: enable debug to collect llm_debug for tuning
		},
	}

	backendURL := "http://127.0.0.1:8000/generate/"

	// --- New: ask backend to generate structured requirements questions (guarantee at least one) ---
	questionPayload := map[string]interface{}{
		"user_answers":     payload["user_answers"],
		"storyblok_schema": payload["storyblok_schema"],
		"options": map[string]interface{}{
			"request_questions": true,
			"max_questions":     3,
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
		respMap, err := callBackend(backendURL, payload)
		if err != nil {
			return fmt.Errorf("call backend: %w", err)
		}

		// Check for top-level followups
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
			// final response: expect files
			files := parseFilesFromResponse(respMap)
			if len(files) == 0 {
				return fmt.Errorf("backend returned no files and no followups")
			}
			// Write files atomically
			if err := scaffold.WriteFilesAtomically(files, absTarget); err != nil {
				return fmt.Errorf("writing project files: %w", err)
			}

			fmt.Println("Project created successfully at:", absTarget)
			// security note
			fmt.Println("\n⚠️  Security note:")
			fmt.Println("  - A .env file containing your Storyblok token may have been written to the project root.")
			fmt.Println("  - Do NOT commit .env to source control. .gitignore includes .env by default.")
			fmt.Println("  - If you prefer not to store the token, remove .env and set VITE_STORYBLOK_TOKEN in your environment.")
			return nil
		}

		// We have followup questions to ask
		answersMap, err := promptFollowupsAndCollect(followups)
		if err != nil {
			return fmt.Errorf("aborted while answering followups: %w", err)
		}

		// attach followup answers into payload.user_answers.followup_answers
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
		// convert back to map[string]interface{} for JSON marshaling
		faInterface := map[string]interface{}{}
		for k, v := range existing {
			faInterface[k] = v
		}
		userAns["followup_answers"] = faInterface
		payload["user_answers"] = userAns

		// loop again
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
