package cmd

import (
	"bufio"
	"bytes"
	"crypto/md5"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"io/fs"
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
	// helper: start an indeterminate progress spinner; returns a channel you should close to stop it
	// helper: start an indeterminate progress spinner; returns a channel you should close to stop it
	startSpinner := func(desc string) chan struct{} {
		done := make(chan struct{})
		pb := progressbar.NewOptions(-1,
			progressbar.OptionSetDescription(desc),
			progressbar.OptionShowCount(),
			progressbar.OptionSpinnerType(14),
		)
		go func() {
			ticker := time.NewTicker(120 * time.Millisecond)
			defer ticker.Stop()
			for {
				select {
				case <-done:
					_ = pb.Finish()
					return
				case <-ticker.C:
					_ = pb.Add(1)
				}
			}
		}()
		return done
	}
	// wrapper for callBackend that shows a spinner while waiting

	// wrapper for callBackend that shows a spinner while waiting
	callBackendWithSpinner := func(url string, payload map[string]interface{}, spinnerDesc string) (map[string]interface{}, error) {
		done := startSpinner(spinnerDesc)
		resp, err := callBackend(url, payload)
		close(done)
		return resp, err
	}

	// wrapper for callBackendStream that shows a spinner while waiting for the initial response
	// wrapper for callBackendStream that shows a spinner while waiting for the initial response
	callBackendStreamWithSpinner := func(url string, payload map[string]interface{}, spinnerDesc string) (*http.Response, error) {
		done := startSpinner(spinnerDesc)
		resp, err := callBackendStream(url, payload)
		// Stop spinner as soon as we have the response (we will handle the streaming read separately).
		close(done)
		return resp, err
	}
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
		Message: "Storyblok API token:",
	}, &token, survey.WithValidator(survey.Required)); err != nil {
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

	// ask user which Storyblok demo framework to use (restricted list)
	var chosenFramework string
	frameworkPrompt := &survey.Select{
		Message: "Choose a base framework for the Storyblok demo scaffold:",
		Options: allowedFrameworks,
		Default: allowedFrameworks[0],
	}
	if err := survey.AskOne(frameworkPrompt, &chosenFramework); err != nil {
		return fmt.Errorf("framework prompt aborted: %w", err)
	}

	// ask package manager (npm or yarn)
	var chosenPM string
	pmPrompt := &survey.Select{
		Message: "Choose package manager for the scaffold:",
		Options: []string{"npm", "yarn"},
		Default: "npm",
	}
	if err := survey.AskOne(pmPrompt, &chosenPM); err != nil {
		return fmt.Errorf("package manager prompt aborted: %w", err)
	}

	// Region selection (replace previous freeform region input)
	var regionChoice string
	regionPrompt := &survey.Select{
		Message: "Space Region (optional, EU is used by default):",
		Options: []string{
			"EU - Europe",
			"US - United States",
			"CN - China",
			"CA - Canada",
		},
		Default: "EU - Europe",
	}
	if err := survey.AskOne(regionPrompt, &regionChoice); err != nil {
		return fmt.Errorf("region prompt aborted: %w", err)
	}

	// map friendly label -> storyblok CLI region codes (example tokens)
	regionMap := map[string]string{
		"EU - Europe":        "eu-central-1",
		"US - United States": "us-east-1",
		"CN - China":         "cn-north-1",
		"CA - Canada":        "ca-central-1",
	}

	// resolve chosenRegion; if not present default to EU
	chosenRegion := regionMap["EU - Europe"]
	if v, ok := regionMap[regionChoice]; ok && strings.TrimSpace(v) != "" {
		chosenRegion = v
	}

	// absTarget should already be resolved from user project name
	preExists := exists(absTarget)
	if preExists {
		return fmt.Errorf("target directory already exists: %s (remove or choose another name)", absTarget)
	}

	_, baseFiles, assetFiles, err := runStoryblokCreateAndCollect(chosenFramework, absTarget, token, chosenPM, chosenRegion)
	if err != nil {
		// If the CLI failed and we didn't have the target before, cleanup the partially created dir
		if !preExists {
			_ = os.RemoveAll(absTarget)
		}
		return fmt.Errorf("storyblok scaffold failed: %w", err)
	}
	// --- before the iterative rounds, declare helpers/state ---
	questionTexts := map[string]string{} // id -> question text
	currentRound := 0
	// --- Iterative question-generation rounds with smart stopping & UI preview ---
	maxFollowupRounds := 2 // number of rounds
	roundQuestions := 5    // requested per round
	urgencyThreshold := 0.25
	askedQuestions := []string{} // normalized previously asked question texts

	// helper to normalize
	normalize := func(s string) string {
		// lower + collapse whitespace
		return strings.Join(strings.Fields(strings.ToLower(strings.TrimSpace(s))), " ")
	}

	// 4) Build initial payload
	payload := map[string]interface{}{
		"user_answers": map[string]interface{}{
			"description": description,
			"app_name":    appName,
			"token":       token,
		},
		"options": map[string]interface{}{
			"include_pages": true,
			"debug":         true,
		},
		"base_files":  baseFiles,  // will be marshaled as JSON array of {path,content}
		"asset_files": assetFiles, // list of binary asset paths (no bytes)
	}

	backendStreamURL := "http://127.0.0.1:8000/generate/stream"
	backendURL := "http://127.0.0.1:8000/generate/"

	for round := 1; round <= maxFollowupRounds; round++ {
		currentRound = round
		// UI preview: show previous followup answers to the user
		userAns, _ := payload["user_answers"].(map[string]interface{})

		// Build payload including previous_questions so backend can avoid repeats
		qPayload := map[string]interface{}{
			"user_answers": payload["user_answers"],
			"options": map[string]interface{}{
				"request_questions":  true,
				"max_questions":      roundQuestions,
				"round_number":       round,
				"debug":              payload["options"].(map[string]interface{})["debug"],
				"previous_questions": askedQuestions,
				"min_urgency":        urgencyThreshold,
				"pad":                true,
			},
		}

		// Use spinner-wrapped backend call so user sees loading after answering follow-ups
		qResp, err := callBackendWithSpinner(backendURL+"questions", qPayload, "Analyzing answers — generating follow-ups...")
		respBytes, _ := json.MarshalIndent(qResp, "", "  ")
		fmt.Println("DEBUG backend response:\n", string(respBytes))
		if err != nil {
			fmt.Fprintf(os.Stderr, "warning: question-generation failed (round %d): %v\n", round, err)
			// fallback to generic prompt only on first round
			if round == 1 {
				initialFollowups := []map[string]interface{}{
					{"id": "", "question": "Briefly describe the key requirements (pages, main features, visual style):", "type": "text", "default": ""},
				}
				ansMap, err := promptFollowupsAndCollect(initialFollowups)
				if err != nil {
					return fmt.Errorf("aborted while answering initial requirements: %w", err)
				}
				// attach and continue
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
			// if backend failed mid-rounds, just break and proceed
			break
		}

		// Parse followups — accept both objects and strings
		currentFollowups := []map[string]interface{}{}
		if fRaw, ok := qResp["followups"]; ok {
			if arr, ok := fRaw.([]interface{}); ok {
				for idx, it := range arr {
					if m, ok := it.(map[string]interface{}); ok {
						qtext, _ := m["question"].(string)
						if strings.TrimSpace(qtext) == "" {
							continue
						}
						qid, _ := m["id"].(string)
						if qid == "" {
							qid = stableIDForQuestion(round, idx, qtext)
						}
						// save text mapping
						questionTexts[qid] = qtext
						// urgency parse
						urg := 0.5
						if u, ok := m["urgency"].(float64); ok {
							urg = u
						} else if u2, ok := m["urgency"].(int); ok {
							urg = float64(u2)
						}
						currentFollowups = append(currentFollowups, map[string]interface{}{"id": qid, "question": qtext, "urgency": urg})
					} else if s, ok := it.(string); ok {
						if strings.TrimSpace(s) != "" {
							qid := stableIDForQuestion(round, idx, s)
							questionTexts[qid] = s
							currentFollowups = append(currentFollowups, map[string]interface{}{"id": qid, "question": s, "urgency": 0.5})
						}
					}
				}
			}
		}

		// Filter out already asked questions (by normalized text) and low-urgency ones
		filteredFollowups := []map[string]interface{}{}
		for _, fu := range currentFollowups {
			qtxt, _ := fu["question"].(string)
			n := normalize(qtxt)
			// skip duplicates
			already := false
			for _, aq := range askedQuestions {
				if aq == n {
					already = true
					break
				}
			}
			if already {
				continue
			}
			// skip low urgency
			urg := 0.5
			if u, ok := fu["urgency"].(float64); ok {
				urg = u
			}
			if urg < urgencyThreshold {
				continue
			}
			filteredFollowups = append(filteredFollowups, fu)
		}

		// If nothing remains after filtering, smart stop
		if len(filteredFollowups) == 0 {
			fmt.Println("No additional high-priority follow-up questions were generated. Continuing.")
			break
		}

		// Prompt user for the remaining followups (convert to expected map shape)
		toPrompt := []map[string]interface{}{}
		for _, fu := range filteredFollowups {
			qid := ""
			if idv, ok := fu["id"].(string); ok {
				qid = idv
			}
			toPrompt = append(toPrompt, map[string]interface{}{"id": qid, "question": fu["question"].(string), "type": "text", "default": ""})
		}

		// record askedQuestions so future rounds don't repeat
		for _, fu := range filteredFollowups {
			if qtxt, ok := fu["question"].(string); ok {
				askedQuestions = append(askedQuestions, normalize(qtxt))
			}
		}

		ansMap, err := promptFollowupsAndCollect(toPrompt)
		if err != nil {
			return fmt.Errorf("aborted while answering followups (round %d): %w", round, err)
		}

		// merge answers into payload.user_answers.followup_answers
		userAns, _ = payload["user_answers"].(map[string]interface{})
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

		// continue to next round (backend will be posted again with updated followup_answers)
	}

	// 5) followup loop (streaming)
	maxRounds := 20
	for round := 1; round < maxRounds; round++ {
		currentRound = round
		// call streaming endpoint to get progress + files
		// show spinner while waiting for the streaming endpoint to accept the request
		resp, err := callBackendStreamWithSpinner(backendStreamURL, payload, "Generating project — this may take a minute...")
		if err != nil {
			// fallback to non-streaming behavior (older backend)
			respMap, err2 := callBackendWithSpinner(backendURL, payload, "Generating project (fallback) — please wait...")
			// attempt to print any new_dependencies from metadata
			if md, ok := respMap["metadata"].(map[string]interface{}); ok {
				if deps, ok := md["dependencies"].(map[string]interface{}); ok {
					if ndRaw, ok := deps["new_dependencies"]; ok {
						if ndArr, ok := ndRaw.([]interface{}); ok && len(ndArr) > 0 {
							fmt.Println("\nSuggested new dependencies:")
							for _, d := range ndArr {
								if s, ok := d.(string); ok {
									fmt.Printf("  - %s\n", s)
								}
							}
						}
					}
				}
			}

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

		handleFollowups := func(followupsIface interface{}, round int) (bool, error) {
			// convert to []map[string]interface{}
			out := []map[string]interface{}{}
			if arr, ok := followupsIface.([]interface{}); ok {
				for idx, it := range arr {
					if m, ok := it.(map[string]interface{}); ok {
						qtext, _ := m["question"].(string)
						if strings.TrimSpace(qtext) == "" {
							continue
						}
						qid, _ := m["id"].(string)
						if qid == "" {
							qid = stableIDForQuestion(round, idx, qtext)
						}
						questionTexts[qid] = qtext
						out = append(out, map[string]interface{}{"id": qid, "question": qtext, "type": "text", "default": ""})
					} else if s, ok := it.(string); ok {
						if strings.TrimSpace(s) == "" {
							continue
						}
						qid := stableIDForQuestion(round, idx, s)
						questionTexts[qid] = s
						out = append(out, map[string]interface{}{"id": qid, "question": s, "type": "text", "default": ""})
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
			existing := map[string]interface{}{}
			if fa, ok := userAns["followup_answers"].(map[string]interface{}); ok {
				existing = fa
			}
			for k, v := range ansMap {
				existing[k] = v
			}
			userAns["followup_answers"] = existing
			payload["user_answers"] = userAns

			// also add these asked question texts to askedQuestions so iterative rounds avoid repeats
			for _, it := range out {
				if qtxt, ok := it["question"].(string); ok {
					askedQuestions = append(askedQuestions, normalize(qtxt))
				}
			}

			return true, nil
		}

		// read loop
		for {
			lineBytes, err := readJSONLine(reader)
			fmt.Println("DEBUG raw line from stream:", string(lineBytes))
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
			fmt.Println("DEBUG parsed event:", ev)
			etype, _ := ev["event"].(string)
			payloadEv := ev["payload"]

			switch etype {
			case "followups":
				// backend requests clarifying questions: prompt user then continue outer loop
				shouldContinue, err := handleFollowups(payloadEv, currentRound)
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
				fmt.Printf("DEBUG file chunk for %s: %s\n", path, chunk)
				final, _ := m["final"].(bool)
				tf, ok := tempFiles[path]
				if !ok {
					// create if not present
					tf = filepath.Join(tmpDir, strings.ReplaceAll(path, "/", "__"))
					_ = os.MkdirAll(filepath.Dir(tf), 0o755)
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
			case "new_dependencies":
				// payloadEv may be []interface{} or other structured data from JSON
				if arr, ok := payloadEv.([]interface{}); ok {
					if len(arr) > 0 {
						fmt.Println("\nSuggested new dependencies from model:")
						for _, it := range arr {
							if s, ok := it.(string); ok {
								fmt.Printf("  - %s\n", s)
							} else {
								bs, _ := json.Marshal(it)
								fmt.Printf("  - %s\n", string(bs))
							}
						}

						// Optional: write a timestamped log file for debugging / audit
						if logDirErr := os.MkdirAll("ai_backend_logs", 0o755); logDirErr == nil {
							if b, err := json.MarshalIndent(arr, "", "  "); err == nil {
								fname := fmt.Sprintf("ai_backend_logs/%d_new_dependencies.json", time.Now().Unix())
								_ = os.WriteFile(fname, b, 0o644)
							}
						}
					}
				} else {
					// fallback generic print + log
					bs, _ := json.MarshalIndent(payloadEv, "", "  ")
					fmt.Printf("\nnew_dependencies: %s\n", string(bs))
					_ = os.MkdirAll("ai_backend_logs", 0o755)
					fname := fmt.Sprintf("ai_backend_logs/%d_new_dependencies_misc.json", time.Now().Unix())
					_ = os.WriteFile(fname, bs, 0o644)
				}

			case "file_complete":
				m, _ := payloadEv.(map[string]interface{})
				path, _ := m["path"].(string)
				tf, ok := tempFiles[path]
				if !ok {
					// missing temp file; skip
					continue
				}

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

		if err := scaffold.WriteFilesMerge(completedFiles, absTarget); err != nil {
			return fmt.Errorf("writing files to project dir: %w", err)
		}
		fmt.Println("Project created successfully at:", absTarget)

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

// helper
func stableIDForQuestion(round int, index int, question string) string {
	// prefer hash of question for stability across runs
	h := md5.Sum([]byte(strings.ToLower(strings.TrimSpace(question))))
	return fmt.Sprintf("r%d_q%d_%s", round, index, hex.EncodeToString(h[:4])) // short hash
}

// Allowed frameworks the user may choose
var allowedFrameworks = []string{"astro", "react", "nextjs", "sveltekit", "vue", "nuxt"}

// runStoryblokCreateAndCollect runs the Storyblok create-demo CLI into `targetDir` and
// returns the absolute path, a list of scaffold.FileOut (excluding package.json & lockfiles),
// AND a list of asset file paths (binary files that were not inlined). targetDir will be created if missing.
func runStoryblokCreateAndCollect(framework, targetDir, token, packagemanager, region string) (string, []scaffold.FileOut, []string, error) {

	// build npx args. Use --yes to avoid prompts when possible.
	args := []string{"--yes", "@storyblok/create-demo@latest", "-d", targetDir, "-f", framework}
	if token != "" {
		args = append(args, "-k", token)
	}
	if packagemanager != "" {
		args = append(args, "-p", packagemanager)
	}
	if region != "" {
		args = append(args, "-r", region)
	}

	cmd := exec.Command("npx", args...)
	// run in the current working directory; CI environments may require env adjustments
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr

	if err := cmd.Run(); err != nil {
		return "", nil, nil, fmt.Errorf("running storyblok create failed: %w", err)
	}

	// define binary extensions we treat as assets (same set used elsewhere)
	binaryExts := map[string]struct{}{
		".png":  {},
		".jpg":  {},
		".jpeg": {},
		".gif":  {},
		".svg":  {},
		".ico":  {},
		".webp": {},
		".avif": {},
	}

	// Walk the generated folder and collect files, excluding package.json and lockfiles and node_modules/.git
	collected := []scaffold.FileOut{}
	assets := []string{}
	err := filepath.WalkDir(targetDir, func(path string, d fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			// ignore problematic files but continue
			return nil
		}
		rel, _ := filepath.Rel(targetDir, path)
		rel = filepath.ToSlash(rel)
		// Skip directories we don't want to descend into
		if d.IsDir() {
			// skip node_modules and .git
			if rel == "node_modules" || strings.HasPrefix(rel, "node_modules/") {
				return filepath.SkipDir
			}
			if rel == ".git" || strings.HasPrefix(rel, ".git/") {
				return filepath.SkipDir
			}
			return nil
		}
		// skip package.json and known lockfiles
		base := filepath.Base(path)
		if base == "package.json" || base == "package-lock.json" || base == "yarn.lock" || base == "pnpm-lock.yaml" {
			return nil
		}

		ext := strings.ToLower(filepath.Ext(rel))
		if _, isBinary := binaryExts[ext]; isBinary {
			// mark as asset (do NOT read bytes)
			assets = append(assets, rel)
			// keep a FileOut entry with empty content so callers can see the path
			collected = append(collected, scaffold.FileOut{Path: rel, Content: ""})
			return nil
		}

		// read file (text)
		b, rerr := os.ReadFile(path)
		if rerr != nil {
			// ignore read errors
			return nil
		}
		collected = append(collected, scaffold.FileOut{Path: rel, Content: string(b)})
		return nil
	})
	if err != nil {
		// non-fatal: return what we collected and the error
		return targetDir, collected, assets, fmt.Errorf("walking generated dir: %w", err)
	}

	return targetDir, collected, assets, nil
}

func exists(path string) bool {
	_, err := os.Stat(path)
	return err == nil
}
