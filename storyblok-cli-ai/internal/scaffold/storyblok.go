package scaffold

import (
	"fmt"
	"os"
	"os/exec"
)

// EnsureStoryblokCLI checks if the Storyblok CLI is installed, and installs it if user agrees.
func EnsureStoryblokCLI() error {
	// Check if "storyblok" is already available
	_, err := exec.LookPath("storyblok")
	if err == nil {
		return nil // Already installed
	}

	// Prompt user
	fmt.Print("Storyblok CLI not found. Install it now with `npm install -g storyblok@beta`? (Y/n): ")
	var resp string
	fmt.Scanln(&resp)
	if resp != "" && (resp[0] == 'n' || resp[0] == 'N') {
		return fmt.Errorf("Storyblok CLI is required but not installed")
	}

	// Run installation
	cmd := exec.Command("npm", "install", "-g", "storyblok@beta")
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("failed to install Storyblok CLI: %w", err)
	}

	// Verify installation succeeded
	_, err = exec.LookPath("storyblok")
	if err != nil {
		return fmt.Errorf("Storyblok CLI installation did not succeed")
	}

	fmt.Println("✅ Storyblok CLI installed successfully.")
	return nil
}
