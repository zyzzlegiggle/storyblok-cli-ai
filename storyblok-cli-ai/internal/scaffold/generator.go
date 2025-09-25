package scaffold

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// GenerateRequest and response shapes (match the backend)
type GenerateRequest struct {
	UserAnswers     map[string]interface{} `json:"user_answers"`
	StoryblokSchema map[string]interface{} `json:"storyblok_schema"`
	Options         map[string]interface{} `json:"options,omitempty"`
}

type FileOut struct {
	Path    string `json:"path"`
	Content string `json:"content"`
}

type GenerateResponse struct {
	ProjectName string                 `json:"project_name"`
	Files       []FileOut              `json:"files"`
	Metadata    map[string]interface{} `json:"metadata,omitempty"`
}

// GenerateAndWriteProject posts payload to backend and atomically writes files into projectDir.
// backendURL must be the full endpoint, e.g., http://127.0.0.1:8000/generate/
func GenerateAndWriteProject(backendURL string, payload GenerateRequest, projectDir string) error {
	// 1) POST the request
	ctx, cancel := context.WithTimeout(context.Background(), 300*time.Second)
	defer cancel()

	reqBody, err := json.Marshal(payload)
	if err != nil {
		return fmt.Errorf("marshal request: %w", err)
	}

	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, backendURL, bytes.NewReader(reqBody))
	if err != nil {
		return fmt.Errorf("create request: %w", err)
	}
	httpReq.Header.Set("Content-Type", "application/json")

	resp, err := http.DefaultClient.Do(httpReq)
	if err != nil {
		return fmt.Errorf("call backend: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		body, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("backend returned status %d: %s", resp.StatusCode, string(body))
	}

	var genResp GenerateResponse
	dec := json.NewDecoder(resp.Body)
	if err := dec.Decode(&genResp); err != nil {
		return fmt.Errorf("decode backend response: %w", err)
	}

	if len(genResp.Files) == 0 {
		return errors.New("backend returned no files")
	}

	// 2) Validate projectDir & tmp dir
	absTarget, err := filepath.Abs(projectDir)
	if err != nil {
		return fmt.Errorf("determine abs path: %w", err)
	}
	if exists(absTarget) {
		return fmt.Errorf("target directory already exists: %s (remove or choose another name)", absTarget)
	}

	parent := filepath.Dir(absTarget)
	tmp, err := os.MkdirTemp(parent, ".tmp-"+filepath.Base(absTarget)+"-")
	if err != nil {
		return fmt.Errorf("create temp dir: %w", err)
	}
	// cleanup tmp on error
	cleanup := func() {
		_ = os.RemoveAll(tmp)
	}
	defer func() {
		if !exists(absTarget) {
			cleanup()
		}
	}()

	// 3) Write files to tmp
	for _, f := range genResp.Files {
		if err := writeFileToDir(tmp, f.Path, f.Content); err != nil {
			return fmt.Errorf("write file %q: %w", f.Path, err)
		}
	}

	// 4) Move tmp -> target (atomic when possible)
	if err := moveDirAtomic(tmp, absTarget); err != nil {
		return fmt.Errorf("move project into place: %w", err)
	}

	fmt.Println("Project scaffolded at:", absTarget)

	// Print warnings from metadata if any
	if genResp.Metadata != nil {
		if w, ok := genResp.Metadata["warnings"]; ok {
			if ws, ok := w.([]interface{}); ok && len(ws) > 0 {
				fmt.Println("Warnings from generator:")
				for _, warn := range ws {
					fmt.Printf(" - %v\n", warn)
				}
			}
		}
	}

	return nil
}

func exists(path string) bool {
	_, err := os.Stat(path)
	return err == nil
}

func writeFileToDir(root string, relPath string, content string) error {
	// sanitize: disallow absolute and ".." traversal
	if filepath.IsAbs(relPath) {
		return fmt.Errorf("absolute path not allowed: %s", relPath)
	}
	clean := filepath.Clean(relPath)
	// Prevent path traversal that escapes root
	if clean == ".." || strings.HasPrefix(clean, ".."+string(filepath.Separator)) || strings.Contains(clean, ".."+string(filepath.Separator)) {
		return fmt.Errorf("path traversal not allowed: %s", relPath)
	}

	fullPath := filepath.Join(root, clean)
	dir := filepath.Dir(fullPath)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return fmt.Errorf("mkdirall %s: %w", dir, err)
	}

	// atomic-ish write: write to tmp file and rename
	tmpFile := fullPath + ".tmp"
	if err := os.WriteFile(tmpFile, []byte(content), 0o644); err != nil {
		return fmt.Errorf("write tmp file: %w", err)
	}
	if err := os.Rename(tmpFile, fullPath); err != nil {
		// fallback to copyFile if rename fails
		if err2 := copyFile(tmpFile, fullPath); err2 != nil {
			return fmt.Errorf("rename fallback failed: %v (orig: %v)", err2, err)
		}
		_ = os.Remove(tmpFile)
	}
	return nil
}

func moveDirAtomic(tmp string, target string) error {
	// Try rename
	if err := os.Rename(tmp, target); err == nil {
		return nil
	}
	// Fallback: copy and remove tmp
	if err := copyDir(tmp, target); err != nil {
		return fmt.Errorf("copy fallback failed: %w", err)
	}
	if err := os.RemoveAll(tmp); err != nil {
		return fmt.Errorf("remove tmp after copy: %w", err)
	}
	return nil
}

func copyDir(src string, dst string) error {
	if err := os.MkdirAll(dst, 0o755); err != nil {
		return err
	}
	return filepath.WalkDir(src, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		rel, err := filepath.Rel(src, path)
		if err != nil {
			return err
		}
		targetPath := filepath.Join(dst, rel)
		if d.IsDir() {
			return os.MkdirAll(targetPath, 0o755)
		}
		return copyFile(path, targetPath)
	})
}

func copyFile(src, dst string) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()

	if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
		return err
	}

	out, err := os.Create(dst)
	if err != nil {
		return err
	}
	defer func() { _ = out.Close() }()

	if _, err := io.Copy(out, in); err != nil {
		return err
	}
	if fi, err := in.Stat(); err == nil {
		_ = out.Chmod(fi.Mode())
	}
	return nil
}


func WriteFilesAtomically(files []FileOut, projectDir string) error {
	absTarget, err := filepath.Abs(projectDir)
	if err != nil {
		return err
	}
	if exists(absTarget) {
		return &os.PathError{Op: "write", Path: absTarget, Err: os.ErrExist}
	}

	parent := filepath.Dir(absTarget)
	tmp, err := os.MkdirTemp(parent, ".tmp-"+filepath.Base(absTarget)+"-")
	if err != nil {
		return err
	}
	// cleanup if any error and target does not exist
	cleanup := func() {
		_ = os.RemoveAll(tmp)
	}
	defer func() {
		if !exists(absTarget) {
			cleanup()
		}
	}()

	// write files to tmp
	for _, f := range files {
		if err := writeFileToDir(tmp, f.Path, f.Content); err != nil {
			return err
		}
	}

	// try rename (atomic if same FS)
	if err := os.Rename(tmp, absTarget); err == nil {
		return nil
	}
	// fallback: copy recursively then remove tmp
	if err := copyDir(tmp, absTarget); err != nil {
		return err
	}
	if err := os.RemoveAll(tmp); err != nil {
		return err
	}
	return nil
}