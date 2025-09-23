package cmd

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
    "encoding/json"
	"github.com/AlecAivazis/survey/v2"
	"github.com/spf13/cobra"

	// adjust the module path if your module name is different
	"storyblok-cli-ai/internal/scaffold"
)

// createAppCmd implements: storyblok-cli create
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
	// Register the command with root in cmd/root.go (rootCmd.AddCommand(createAppCmd))
	createAppCmd.Flags().StringP("output", "o", "", "Output folder for the generated project (default: ./<app_name>)")
	// No backend-url flag per your requirement (uses fixed localhost backend)
}

func runCreateWizard(cmd *cobra.Command) error {
	// 1) Collect interactive inputs
	var appName string
	var token string
	var pagesCSV string
	var featuresCSV string
	var includePages bool

	qs := []*survey.Question{
		{
			Name:     "appName",
			Prompt:   &survey.Input{Message: "App name:"},
			Validate: survey.Required,
		},
		{
			Name:   "token",
			Prompt: &survey.Input{Message: "Storyblok API token (will be saved to the generated project env):"},
		},
		{
			Name:   "pages",
			Prompt: &survey.Input{Message: "Primary pages (comma-separated, e.g. home,about):"},
		},
		{
			Name:   "features",
			Prompt: &survey.Input{Message: "Features (comma-separated, e.g. navigation,forms,lists):"},
		},
		{
			Name:   "includePages",
			Prompt: &survey.Confirm{Message: "Include default pages scaffolded?"},
		},
	}

	answers := struct {
		AppName      string
		Token        string
		Pages        string
		Features     string
		IncludePages bool
	}{}

	if err := survey.Ask(qs, &answers); err != nil {
		return fmt.Errorf("prompt aborted: %w", err)
	}

	appName = strings.TrimSpace(answers.AppName)
	token = strings.TrimSpace(answers.Token)
	pagesCSV = strings.TrimSpace(answers.Pages)
	featuresCSV = strings.TrimSpace(answers.Features)
	includePages = answers.IncludePages

	// 2) Prepare Storyblok schema fetch (for the hackathon, we can ask user to paste or rely on minimal)
	// For now we will prompt for a very small schema sample or rely on backend to work with empty schema.
	var schemaChoice string
	if err := survey.AskOne(&survey.Select{
		Message: "Storyblok schema source:",
		Options: []string{"Fetch from Storyblok (requires token & network)", "Provide minimal sample schema", "Skip (generate generic components)"},
	}, &schemaChoice); err != nil {
		return fmt.Errorf("schema choice aborted: %w", err)
	}

	var storyblokSchema map[string]interface{}
	switch schemaChoice {
	case "Fetch from Storyblok (requires token & network)":
		// attempt to fetch via Storyblok Management API
		fmt.Println("Fetching schema from Storyblok is not yet implemented in this command. Using empty schema fallback.")
		storyblokSchema = map[string]interface{}{}
	case "Provide minimal sample schema":
		// Ask for one or two component names (simple)
		var compNames string
		if err := survey.AskOne(&survey.Input{Message: "Component names (comma-separated, e.g. Hero,Article):"}, &compNames); err != nil {
			return fmt.Errorf("component names aborted: %w", err)
		}
		names := parseCSV(compNames)
		comps := []map[string]interface{}{}
		for _, n := range names {
			comps = append(comps, map[string]interface{}{
				"name":   n,
				"schema": map[string]interface{}{"title": "text", "description": "text"},
			})
		}
		storyblokSchema = map[string]interface{}{"components": comps}
	default:
		storyblokSchema = map[string]interface{}{}
	}

	// 3) Build payload for backend
	payload := scaffold.GenerateRequest{
		UserAnswers: map[string]interface{}{
			"app_name": appName,
			"token":    token,
			"pages":    parseCSV(pagesCSV),
			"features": parseCSV(featuresCSV),
		},
		StoryblokSchema: storyblokSchema,
		Options: map[string]interface{}{
			"typescript":   true,
			"include_pages": includePages,
		},
	}

	// 4) Determine output directory
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

	// 5) Call backend and write files
	backendURL := "http://127.0.0.1:8000/generate/"
	if err := scaffold.GenerateAndWriteProject(backendURL, payload, absTarget); err != nil {
		return err
	}
    // After GenerateAndWriteProject succeeds
    fmt.Println("\n⚠️  Security note:")
    fmt.Println("  - A .env file containing your Storyblok token was written to the project root.")
    fmt.Println("  - Do NOT commit .env to source control. Add it to .gitignore (already included).")
    fmt.Println("  - If you prefer not to store the token, remove .env and set VITE_STORYBLOK_TOKEN in your environment.")


	fmt.Println("Done. Next steps:")
	fmt.Printf("  cd %s\n", absTarget)
	fmt.Println("  npm install")
	fmt.Println("  npm run dev")
	return nil
}

func parseCSV(s string) []string {
	s = strings.TrimSpace(s)
	if s == "" {
		return []string{}
	}
	parts := strings.Split(s, ",")
	out := make([]string, 0, len(parts))
	for _, p := range parts {
		if t := strings.TrimSpace(p); t != "" {
			out = append(out, t)
		}
	}
	return out
}

func printStructuredError(err error) {
	type Out struct {
		Error   string `json:"error"`
		Message string `json:"message,omitempty"`
	}
	out := Out{
		Error:   "create-app-failed",
		Message: err.Error(),
	}
	b, _ := jsonMarshalIndent(out, "", "  ")
	fmt.Fprintln(os.Stderr, string(b))
}

// Small JSON indent helper (so we avoid importing encoding/json in many places)
func jsonMarshalIndent(v interface{}, prefix, indent string) ([]byte, error) {
	// import here to keep top imports tidy
	type alias interface{}
	return json.MarshalIndent(v, prefix, indent)
}
