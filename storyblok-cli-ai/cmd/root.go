package cmd

import (
	"fmt"
	"os"

	"github.com/spf13/cobra"
)

var rootCmd = &cobra.Command{
	Use:   "storyblok-cli",
	Short: "AI-powered Code Generator with Storyblok",
	Long:  "AI-powered Code Generator with Storyblok",
}

// Execute runs the root command (called from main.go)
func Execute() error {
	return rootCmd.Execute()
}

func init() {
	// Add subcommands here
	rootCmd.AddCommand(createAppCmd)

	// Optional: global persistent flags
	rootCmd.PersistentFlags().BoolP("verbose", "v", false, "Enable verbose logging")
}

// Utility for structured fatal errors at the root level
func fatal(err error) {
	fmt.Fprintln(os.Stderr, err)
	os.Exit(1)
}
