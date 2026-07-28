package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"image"
	"image/color"
	"image/png"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
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
	img := image.NewRGBA(image.Rect(0, 0, 16, 16))
	// Dark background with a green bar chart
	bg := color.RGBA{13, 17, 23, 255}     // #0d1117
	green := color.RGBA{63, 185, 80, 255}  // #3fb950
	blue := color.RGBA{88, 166, 255, 255}  // #58a6ff
	purple := color.RGBA{188, 140, 255, 255} // #bc8cff

	// Fill background
	for y := 0; y < 16; y++ {
		for x := 0; x < 16; x++ {
			img.Set(x, y, bg)
		}
	}

	// Bar chart: 3 bars
	for y := 0; y < 3; y++ {
		for x := 0; x < 16; x++ {
			switch {
			case x >= 2 && x < 5 && y >= 9:
				img.Set(x, y+3, green)
			case x >= 6 && x < 9 && y >= 5:
				img.Set(x, y+3, blue)
			case x >= 10 && x < 13 && y >= 7:
				img.Set(x, y+3, purple)
			}
		}
	}

	var buf bytes.Buffer
	if err := png.Encode(&buf, img); err != nil {
		return nil
	}
	return buf.Bytes()
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
	var cmd string
	var args []string

	switch {
	case isWindows():
		cmd = "cmd"
		args = []string{"/c", "start", url}
	case isMac():
		cmd = "open"
		args = []string{url}
	default: // Linux
		cmd = "xdg-open"
		args = []string{url}
	}

	proc, err := os.StartProcess(cmd, append([]string{cmd}, args...), &os.ProcAttr{})
	if err != nil {
		log.Printf("Failed to open browser: %v", err)
	} else {
		proc.Release()
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
