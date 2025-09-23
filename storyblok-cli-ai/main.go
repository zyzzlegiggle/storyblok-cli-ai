package main

import (
	"fmt"
	"os"

	"storyblok-cli-ai/cmd" // adjust to your module name
)

func main() {
	if err := cmd.Execute(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
