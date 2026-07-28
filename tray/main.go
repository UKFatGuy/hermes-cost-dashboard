package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"image"
	"image/color"
	"image/png"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"strconv"
	"time"

	"github.com/getlantern/systray"
)

// ─── Config ────────────────────────────────────────────────────────────────

var apiBaseURL = getEnvOrDefault("COST_API_URL", "https://cost.omoikane.icu")
var refreshInterval = getDurationEnv("COST_REFRESH_INTERVAL", 5*time.Minute)

func getEnvOrDefault(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func getDurationEnv(key string, def time.Duration) time.Duration {
	if v := os.Getenv(key); v != "" {
		d, err := time.ParseDuration(v)
		if err == nil {
			return d
		}
	}
	return def
}

// ─── Data types ────────────────────────────────────────────────────────────

type Summary struct {
	SessionCount     int     `json:"session_count"`
	TotalInputTokens int64   `json:"total_input_tokens"`
	TotalOutputTokens int64  `json:"total_output_tokens"`
	TotalCost        float64 `json:"total_cost"`
	ByModel          []ModelEntry `json:"by_model"`
}

type ModelEntry struct {
	Model      string  `json:"model"`
	Provider   string  `json:"billing_provider"`
	Cost       float64 `json:"cost"`
	InputTok   int64   `json:"input_tokens"`
	OutputTok  int64   `json:"output_tokens"`
	APICalls   int     `json:"api_calls"`
}

// ─── Icon generation ───────────────────────────────────────────────────────

func generateIcon() []byte {
	// Build a simple bar-chart PNG first
	img := image.NewRGBA(image.Rect(0, 0, 32, 32))
	bg := color.RGBA{13, 17, 23, 255}
	green := color.RGBA{63, 185, 80, 255}
	blue := color.RGBA{88, 166, 255, 255}
	purple := color.RGBA{188, 140, 255, 255}

	for y := 0; y < 32; y++ {
		for x := 0; x < 32; x++ {
			img.Set(x, y, bg)
		}
	}
	// Bar chart: 3 bars
	for x := 0; x < 32; x++ {
		for y := 0; y < 32; y++ {
			switch {
			case x >= 4 && x < 11 && y >= 18:
				img.Set(x, y, green)
			case x >= 13 && x < 20 && y >= 8:
				img.Set(x, y, blue)
			case x >= 22 && x < 29 && y >= 13:
				img.Set(x, y, purple)
			}
		}
	}

	var pngBuf bytes.Buffer
	if err := png.Encode(&pngBuf, img); err != nil {
		return nil
	}
	pngData := pngBuf.Bytes()

	// Wrap in ICO format (Windows requires ICO, not raw PNG)
	// ICO header: reserved(2) + type(2=1) + count(2) = 6 bytes
	// Directory entry: w(1) + h(1) + colors(1) + reserved(1) + planes(2) + bpp(2) + size(4) + offset(4) = 16 bytes
	header := make([]byte, 6+16+len(pngData))
	// Type: 1 = ICO
	header[2] = 1
	header[3] = 0
	// Count: 1 image
	header[4] = 1
	// Directory entry
	header[6] = 32          // width
	header[7] = 32          // height
	header[8] = 0           // colors
	header[10] = 1          // planes
	header[12] = 32         // bpp
	// Size of PNG data (little-endian uint32)
	pngSize := len(pngData)
	header[14] = byte(pngSize)
	header[15] = byte(pngSize >> 8)
	header[16] = byte(pngSize >> 16)
	header[17] = byte(pngSize >> 24)
	// Offset: 22 (6 header + 16 dir entry)
	header[18] = 22
	// Image data
	copy(header[22:], pngData)

	return header
}

// ─── API call ──────────────────────────────────────────────────────────────

func fetchSummary(days int) (*Summary, error) {
	u, _ := url.Parse(apiBaseURL + "/api/summary")
	q := u.Query()
	q.Set("days", strconv.Itoa(days))
	u.RawQuery = q.Encode()

	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Get(u.String())
	if err != nil {
		return nil, fmt.Errorf("HTTP request failed: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("read body: %w", err)
	}

	var s Summary
	if err := json.Unmarshal(body, &s); err != nil {
		return nil, fmt.Errorf("JSON parse: %w", err)
	}
	return &s, nil
}

func fmtCost(c float64) string {
	gbp := c * 0.79
	if gbp < 0.01 {
		return "£0.00"
	}
	return fmt.Sprintf("£%.2f", gbp)
}

func fmtTokens(n int64) string {
	switch {
	case n >= 1_000_000:
		return fmt.Sprintf("%.1fM", float64(n)/1_000_000)
	case n >= 1_000:
		return fmt.Sprintf("%.1fK", float64(n)/1_000)
	default:
		return strconv.FormatInt(n, 10)
	}
}

// ─── Tray lifecycle ────────────────────────────────────────────────────────

var (
	mOpen    *systray.MenuItem
	mRefresh *systray.MenuItem
	mCost    *systray.MenuItem
	mSep1    *systray.MenuItem
	mModels  []*systray.MenuItem
	mSep2    *systray.MenuItem
	mQuit    *systray.MenuItem
)

func onReady() {
	systray.SetIcon(generateIcon())
	systray.SetTooltip("Hermes Cost Dashboard — loading...")

	mOpen = systray.AddMenuItem("Open Dashboard", "Open the web dashboard in your browser")
	mRefresh = systray.AddMenuItem("Refresh Now", "Fetch latest cost data")
	systray.AddSeparator()

	mCost = systray.AddMenuItem("Loading...", "")
	mCost.Disable()

	systray.AddSeparator()
	mQuit = systray.AddMenuItem("Quit", "Exit the tray app")

	// Initial fetch
	go func() {
		updateDisplay()
		// Start periodic refresh
		ticker := time.NewTicker(refreshInterval)
		for range ticker.C {
			updateDisplay()
		}
	}()

	// Handle menu clicks
	go func() {
		for {
			select {
			case <-mOpen.ClickedCh:
				openBrowser(apiBaseURL)
			case <-mRefresh.ClickedCh:
				updateDisplay()
			case <-mQuit.ClickedCh:
				systray.Quit()
				return
			}
		}
	}()
}

func onExit() {
	// Cleanup if needed
}

func updateDisplay() {
	summary, err := fetchSummary(30)
	if err != nil {
		systray.SetTooltip("Hermes Cost Dashboard — error")
		mCost.SetTitle(fmt.Sprintf("⚠ Error: %v", err))
		return
	}

	totalGBP := summary.TotalCost * 0.79
	_ = totalGBP // used in tooltip string building below
	tooltip := fmt.Sprintf("Hermes Cost: %s (%d sessions, %s tokens)",
		fmtCost(summary.TotalCost),
		summary.SessionCount,
		fmtTokens(summary.TotalInputTokens+summary.TotalOutputTokens),
	)
	systray.SetTooltip(tooltip)

	// Update menu with breakdown
	var menuLines []string
	menuLines = append(menuLines, fmt.Sprintf("Total: %s", fmtCost(summary.TotalCost)))
	menuLines = append(menuLines, fmt.Sprintf("Sessions: %d", summary.SessionCount))
	menuLines = append(menuLines, fmt.Sprintf("Tokens: %s in / %s out",
		fmtTokens(summary.TotalInputTokens), fmtTokens(summary.TotalOutputTokens)))
	menuLines = append(menuLines, "")
	for _, m := range summary.ByModel {
		if m.Cost > 0.001 {
			menuLines = append(menuLines, fmt.Sprintf("%s: %s (%s calls)",
				m.Model, fmtCost(m.Cost), strconv.Itoa(m.APICalls)))
		}
	}

	// Flatten into single menu item (systray doesn't do multi-line well on all platforms)
	title := fmt.Sprintf("💰 %s  ·  %d sessions", fmtCost(summary.TotalCost), summary.SessionCount)
	mCost.SetTitle(title)
}

func openBrowser(url string) {
	// exec.Command handles PATH resolution correctly on Windows
	if isWindows() {
		// ShellExecute via rundll32 — most reliable on Windows without console
		exec.Command("rundll32", "url.dll,FileProtocolHandler", url).Start()
	} else if isMac() {
		exec.Command("open", url).Start()
	} else {
		exec.Command("xdg-open", url).Start()
	}
}

func isWindows() bool {
	return len(os.Getenv("SYSTEMROOT")) > 0
}

func isMac() bool {
	return false // Simplified; not critical for Windows build
}

// ─── Main ──────────────────────────────────────────────────────────────────

func main() {
	// Allow override via flags (simple env-based for now)
	if v := os.Getenv("COST_API_URL"); v != "" {
		apiBaseURL = v
	}

	systray.Run(onReady, onExit)
}
