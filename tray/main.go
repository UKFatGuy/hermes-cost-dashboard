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
	"strings"
	"time"

	"github.com/getlantern/systray"
)

// ─── Config ────────────────────────────────────────────────────────────────

var apiBaseURL = getEnvOrDefault("COST_API_URL", "https://cost.omoikane.icu")
var refreshInterval = getDurationEnv("COST_REFRESH_INTERVAL", 5*time.Minute)
// Optional HTTP Basic Auth (Caddy basic_auth on the dashboard URL).
// Set COST_API_USER / COST_API_PASS as Windows user env vars.
var apiUser = os.Getenv("COST_API_USER")
var apiPass = os.Getenv("COST_API_PASS")

// USD -> GBP. TODO(inc 3): make dynamic/configurable via API.
const gbpRate = 0.79

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
	SessionCount      int           `json:"session_count"`
	TotalInputTokens  int64         `json:"total_input_tokens"`
	TotalOutputTokens int64         `json:"total_output_tokens"`
	TotalCost         float64       `json:"total_cost"`
	FreeTierCost      float64       `json:"free_tier_cost"`
	TodayCost         float64       `json:"today_cost"`
	AvgDailyBurn      float64       `json:"avg_daily_burn"`
	ProjectedMonthly  float64       `json:"projected_monthly"`
	Alerts            Alerts        `json:"alerts"`
	CacheReadTokens   int64         `json:"cache_read_tokens"`
	ReasoningTokens   int64         `json:"reasoning_tokens"`
	ByModel           []ModelEntry  `json:"by_model"`
	ByProfile         []ProfileEntry `json:"by_profile"`
	Daily             []DailyEntry  `json:"daily"`
	Databases         []string      `json:"databases"`
}

type Alerts struct {
	Level   string      `json:"level"`
	Daily   AlertStatus `json:"daily"`
	Monthly AlertStatus `json:"monthly"`
}

type AlertStatus struct {
	ThresholdUSD float64 `json:"threshold_usd"`
	CurrentUSD   float64 `json:"current_usd"`
	Breached     bool    `json:"breached"`
}

type ModelEntry struct {
	Model      string  `json:"model"`
	Provider   string  `json:"billing_provider"`
	Cost       float64 `json:"cost"`
	InputTok   int64   `json:"input_tokens"`
	OutputTok  int64   `json:"output_tokens"`
	APICalls   int     `json:"api_calls"`
}

type ProfileEntry struct {
	Profile      string  `json:"profile"`
	Cost         float64 `json:"cost"`
	InputTokens  int64   `json:"input_tokens"`
	OutputTokens int64   `json:"output_tokens"`
	SessionCount int     `json:"session_count"`
}

type DailyEntry struct {
	Day          string  `json:"day"`
	Cost         float64 `json:"cost"`
	SessionCount int     `json:"session_count"`
}

// ─── Icon generation ───────────────────────────────────────────────────────

func generateIcon(level string) []byte {
	// Build a simple bar-chart PNG first
	img := image.NewRGBA(image.Rect(0, 0, 32, 32))
	bg := color.RGBA{13, 17, 23, 255}
	green := color.RGBA{63, 185, 80, 255}
	blue := color.RGBA{88, 166, 255, 255}
	purple := color.RGBA{188, 140, 255, 255}
	// Inc 4: alert colouring — tint the bars red (critical) / orange (warn)
	if level == "critical" {
		red := color.RGBA{248, 81, 73, 255}
		green, blue, purple = red, red, red
	} else if level == "warn" {
		orange := color.RGBA{210, 153, 34, 255}
		green, blue, purple = orange, orange, orange
	}

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
	req, err := http.NewRequest("GET", u.String(), nil)
	if err != nil {
		return nil, fmt.Errorf("request build failed: %w", err)
	}
	if apiUser != "" || apiPass != "" {
		req.SetBasicAuth(apiUser, apiPass)
	}
	resp, err := client.Do(req)
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
	gbp := c * gbpRate
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
	mQuit    *systray.MenuItem
	mTotal   *systray.MenuItem
	mToday   *systray.MenuItem
	mProjected *systray.MenuItem
	mAlert   *systray.MenuItem
	// Fixed set of known profiles; dynamic additions handled at runtime.
	mProfileItems = map[string]*systray.MenuItem{}
)

func onReady() {
	systray.SetIcon(generateIcon("ok"))
	systray.SetTooltip("Hermes Cost Dashboard — loading...")

	mOpen = systray.AddMenuItem("Open Dashboard", "Open the web dashboard in your browser")
	mRefresh = systray.AddMenuItem("Refresh Now", "Fetch latest cost data")
	systray.AddSeparator()

	mTotal = systray.AddMenuItem("Loading...", "")
	mTotal.Disable()
	mToday = systray.AddMenuItem("Loading...", "")
	mToday.Disable()
	mProjected = systray.AddMenuItem("Loading...", "")
	mProjected.Disable()
	mAlert = systray.AddMenuItem("", "")
	mAlert.Disable()

	for _, name := range []string{"default", "ukfatguy", "issy", "billy", "chronicler"} {
		item := systray.AddMenuItem("  "+name+": —", "")
		item.Disable()
		mProfileItems[name] = item
	}

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
		mTotal.SetTitle(fmt.Sprintf("⚠ Error: %v", err))
		return
	}

	// Today's spend — prefer the backend-computed field, fall back to the
	// daily series (older backend without today_cost).
	todayCost := summary.TodayCost
	if todayCost == 0 {
		todayStr := time.Now().UTC().Format("2006-01-02")
		for _, d := range summary.Daily {
			if d.Day == todayStr {
				todayCost = d.Cost
				break
			}
		}
	}
	projected := summary.ProjectedMonthly

	tooltip := fmt.Sprintf("Hermes Cost (30d): %s · Today: %s · Projected/mo: %s · %d sessions · %s tokens",
		fmtCost(summary.TotalCost),
		fmtCost(todayCost),
		fmtCost(projected),
		summary.SessionCount,
		fmtTokens(summary.TotalInputTokens+summary.TotalOutputTokens),
	)
	systray.SetTooltip(tooltip)

	mTotal.SetTitle(fmt.Sprintf("💰 Total (30d): %s · %d sessions", fmtCost(summary.TotalCost), summary.SessionCount))
	mToday.SetTitle(fmt.Sprintf("📅 Today: %s", fmtCost(todayCost)))
	mProjected.SetTitle(fmt.Sprintf("📈 Projected (30d): %s", fmtCost(projected)))

	// Inc 4: alert state — tint the icon + surface threshold breaches
	level := summary.Alerts.Level
	if level == "" {
		level = "ok"
	}
	systray.SetIcon(generateIcon(level))
	if level != "ok" {
		parts := []string{}
		if summary.Alerts.Daily.Breached {
			parts = append(parts, fmt.Sprintf("today %s over %s cap", fmtCost(summary.Alerts.Daily.CurrentUSD), fmtCost(summary.Alerts.Daily.ThresholdUSD)))
		}
		if summary.Alerts.Monthly.Breached {
			parts = append(parts, fmt.Sprintf("month %s over %s cap", fmtCost(summary.Alerts.Monthly.CurrentUSD), fmtCost(summary.Alerts.Monthly.ThresholdUSD)))
		}
		if len(parts) == 0 {
			parts = append(parts, "approaching threshold")
		}
		mAlert.SetTitle("⚠ ALERT: " + strings.Join(parts, " · "))
	} else {
		mAlert.SetTitle("")
	}

	// Per-profile lines
	for _, p := range summary.ByProfile {
		item, ok := mProfileItems[p.Profile]
		if !ok {
			// Unknown profile — add dynamically
			item = systray.AddMenuItem("  "+p.Profile+": —", "")
			item.Disable()
			mProfileItems[p.Profile] = item
		}
		item.SetTitle(fmt.Sprintf("  %s: %s · %d sessions", p.Profile, fmtCost(p.Cost), p.SessionCount))
	}
	// Zero out profiles that exist in the menu but weren't in the payload
	for name, item := range mProfileItems {
		found := false
		for _, p := range summary.ByProfile {
			if p.Profile == name {
				found = true
				break
			}
		}
		if !found {
			item.SetTitle(fmt.Sprintf("  %s: £0.00 · 0 sessions", name))
		}
	}
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
