import os
import gc
import json
import time
import queue
import signal
import subprocess
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import schedule
from playwright.sync_api import sync_playwright

# ==================== PATHS & CONSTANTS ====================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "schedule_config.json")
USER_DATA_DIR = os.path.expanduser("~/.church_yt_music_profile")

# Permanently blocked playlist IDs (cannot be overridden)
HARD_BLOCKED_IDS = {"LM"}

# Exact XPath for the main playlist Play button
PLAY_BUTTON_XPATH = (
    "/html/body/ytmusic-app/ytmusic-app-layout/div[4]/ytmusic-browse-response/"
    "div[2]/div[4]/div[1]/ytmusic-two-column-browse-results-renderer/div[1]/"
    "ytmusic-section-list-renderer/div[2]/ytmusic-editable-playlist-detail-header-renderer/"
    "ytmusic-responsive-header-renderer/div[7]/ytmusic-play-button-renderer/div/yt-icon/span"
)

DEFAULT_CONFIG = {
    "blocked_playlist_ids": ["LM"],
    "scenarios": {
        "tuesday_prayer": {
            "name": "Tuesday Prayer",
            "url": "https://music.youtube.com/playlist?list=PLdzFGqur9FYG8XYMCyGw46UB4ZHQ7lRRr",
            "days": ["tuesday"],
            "start_time": "08:15",
            "end_time": "08:55"
        },
        "friday_media": {
            "name": "Friday Media",
            "url": "https://music.youtube.com/playlist?list=PLdzFGqur9FYEBRNHS10nJ1JtJ4uR2DQSr",
            "days": ["friday"],
            "start_time": "17:30",
            "end_time": "18:30"
        },
        "weekend_media": {
            "name": "Weekend Media",
            "url": "https://music.youtube.com/playlist?list=PLdzFGqur9FYHIGL8mHmqukkZ1wK7JDswH",
            "days": ["saturday", "sunday"],
            "start_time": "08:45",
            "end_time": "09:55"
        }
    }
}

VALID_DAYS = {
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"
}
# ===========================================================

current_playwright = None
current_context = None
current_page = None

config_lock = threading.Lock()
current_config = {}
last_config_mtime = 0

command_queue = queue.Queue()
log_history = []
log_lock = threading.Lock()
stop_event = threading.Event()


def log(message):
    """Prints to Terminal/service.log and stores logs for the browser control panel."""
    timestamp = time.strftime("%I:%M:%S %p")
    formatted = f"[{timestamp}] {message}"
    print(formatted, flush=True)
    with log_lock:
        log_history.append(formatted)
        if len(log_history) > 200:
            log_history.pop(0)


def extract_playlist_id(url):
    """Extracts the 'list' parameter from a YouTube Music URL."""
    if not url or "list=" not in url:
        return ""
    return url.split("list=", 1)[1].split("&", 1)[0].strip()


def get_blocked_ids():
    """Returns all blocked playlist IDs, always including 'LM'."""
    with config_lock:
        extra = set(current_config.get("blocked_playlist_ids", []))
    return HARD_BLOCKED_IDS | extra


def is_blocked_url(url):
    """Returns True if the URL points to a blocked playlist ID."""
    if not url:
        return True
    playlist_id = extract_playlist_id(url)
    return playlist_id in get_blocked_ids()


def ensure_config_exists():
    """Creates schedule_config.json with default values if it doesn't exist yet."""
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        log(f"[Config] Created default configuration file at {CONFIG_PATH}")


def load_config_and_schedules(is_reload=False):
    """Reads schedule_config.json and rebuilds all weekly timers."""
    global current_config, last_config_mtime

    ensure_config_exists()

    try:
        mtime = os.path.getmtime(CONFIG_PATH)
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            parsed = json.load(f)

        if "scenarios" not in parsed or not isinstance(parsed["scenarios"], dict):
            raise ValueError("JSON must contain a 'scenarios' object.")

        with config_lock:
            current_config = parsed
            last_config_mtime = mtime

        schedule.clear()
        scenarios = parsed.get("scenarios", {})
        job_count = 0

        for key, item in scenarios.items():
            name = item.get("name", key)
            days = item.get("days", [])
            start_time = str(item.get("start_time", "")).strip()
            end_time = str(item.get("end_time", "")).strip()

            for raw_day in days:
                day = str(raw_day).strip().lower()
                if day not in VALID_DAYS:
                    log(f"[Config] ⚠ Skipping invalid day '{raw_day}' in '{name}'.")
                    continue

                day_scheduler = getattr(schedule.every(), day)

                if start_time:
                    try:
                        day_scheduler.at(start_time).do(open_youtube_music, scenario_key=key)
                        job_count += 1
                    except Exception as e:
                        log(f"[Config] ✖ Invalid start_time '{start_time}' for '{name}' (use 24-hr HH:MM): {e}")

                if end_time:
                    try:
                        getattr(schedule.every(), day).at(end_time).do(close_youtube_music_with_fade)
                        job_count += 1
                    except Exception as e:
                        log(f"[Config] ✖ Invalid end_time '{end_time}' for '{name}' (use 24-hr HH:MM): {e}")

        action_word = "Reloaded" if is_reload else "Loaded"
        log(f"[Config] ✔ {action_word} {len(scenarios)} scenario(s) ({job_count} scheduled timers active).")
        return True

    except json.JSONDecodeError as e:
        log(f"[Config] ✖ Syntax error in schedule_config.json (line {e.lineno}): {e.msg}")
        return False
    except Exception as e:
        log(f"[Config] ✖ Error reading schedule_config.json: {e}")
        return False


def terminate_lingering_profile_processes():
    """Terminates only Chrome processes bound to USER_DATA_DIR (never touches regular Chrome)."""
    try:
        output = subprocess.check_output(["ps", "-ax", "-o", "pid=,command="], text=True)
    except Exception:
        return

    my_pid = os.getpid()
    matching_pids = []

    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        cmd = parts[1]
        if pid != my_pid and USER_DATA_DIR in cmd:
            matching_pids.append(pid)

    if not matching_pids:
        return

    for pid in matching_pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass

    time.sleep(0.8)

    for pid in matching_pids:
        try:
            os.kill(pid, 0)
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass


def sanitize_profile_directory():
    """Removes stale Chrome lock symlinks and marks exit as clean to prevent 'Restore pages?' popups."""
    if not os.path.isdir(USER_DATA_DIR):
        return

    for lock_name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        lock_path = os.path.join(USER_DATA_DIR, lock_name)
        try:
            if os.path.lexists(lock_path):
                os.unlink(lock_path)
        except Exception:
            pass

    prefs_path = os.path.join(USER_DATA_DIR, "Default", "Preferences")
    if os.path.isfile(prefs_path):
        try:
            with open(prefs_path, "r", encoding="utf-8") as f:
                prefs = json.load(f)
            profile_section = prefs.setdefault("profile", {})
            if profile_section.get("exit_type") != "Normal" or profile_section.get("exited_cleanly") is not True:
                profile_section["exit_type"] = "Normal"
                profile_section["exited_cleanly"] = True
                tmp_path = prefs_path + ".tmp"
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(prefs, f, separators=(",", ":"))
                os.replace(tmp_path, prefs_path)
        except Exception:
            pass


def is_session_alive():
    """Returns True if an automation Chrome window is still open."""
    if not current_context:
        return False
    try:
        pages = current_context.pages
        return any(not p.is_closed() for p in pages)
    except Exception:
        return False


def ensure_shuffle_disabled_on_page(page):
    """Turns off shuffle on the YouTube Music player bar if it is currently enabled."""
    if not page:
        return
    try:
        if page.is_closed():
            return
        page.set_default_timeout(2000)
        turned_off = page.evaluate("""
        (function() {
            var shuffleBtn = document.querySelector(
                'ytmusic-player-bar tp-yt-paper-icon-button.shuffle, ' +
                'ytmusic-player-bar button[aria-label*="Shuffle"]'
            );
            if (shuffleBtn && shuffleBtn.getAttribute('aria-pressed') === 'true') {
                shuffleBtn.click();
                return true;
            }
            return false;
        })();
        """)
        if turned_off:
            time.sleep(0.35)
    except Exception:
        pass


def cleanup_browser_handles(silent=False):
    """Disables shuffle, closes pages & context, stops Playwright, and quits the profile Chrome process."""
    global current_playwright, current_context, current_page

    had_handles = any(x is not None for x in (current_page, current_context, current_playwright))

    # 1. Disable shuffle and close open tabs cleanly
    if current_context:
        try:
            for page in list(current_context.pages):
                try:
                    if not page.is_closed():
                        ensure_shuffle_disabled_on_page(page)
                        page.close(run_before_unload=False)
                except Exception:
                    pass
        except Exception:
            pass

        try:
            current_context.close()
        except Exception:
            pass

    # 2. Stop the Playwright driver process
    if current_playwright:
        try:
            current_playwright.stop()
        except Exception:
            pass

    current_page = None
    current_context = None
    current_playwright = None

    # 3. Terminate any remaining Chrome process tied strictly to USER_DATA_DIR (removes Dock icon)
    time.sleep(0.3)
    terminate_lingering_profile_processes()
    sanitize_profile_directory()
    gc.collect()

    if had_handles and not silent:
        log("[Cleanup] ✔ Shuffle reset, Chrome app closed, and profile locks cleaned up.")


def open_youtube_music(scenario_key):
    """Opens YouTube Music for the given config scenario, plays, shuffles, and sets volume to 100%."""
    global current_playwright, current_context, current_page

    with config_lock:
        scenario = current_config.get("scenarios", {}).get(scenario_key)

    if not scenario:
        log(f"[Automation] ✖ Scenario '{scenario_key}' not found in schedule_config.json.")
        return

    name = scenario.get("name", scenario_key)
    target_url = str(scenario.get("url", "")).strip()
    playlist_id = extract_playlist_id(target_url)

    # Hard safeguard against blocked playlist IDs (e.g., list=LM)
    if not target_url or is_blocked_url(target_url) or not playlist_id:
        log(f"[Automation] ✖ Blocked or invalid playlist URL for '{name}': {target_url}")
        return

    if current_context or current_playwright:
        log("[Automation] Cleaning up existing session before launching new scenario...")
        cleanup_browser_handles(silent=True)
    else:
        terminate_lingering_profile_processes()
        sanitize_profile_directory()

    log(f"[Automation] Starting '{name}' (ID: {playlist_id}) -> {target_url}")

    try:
        current_playwright = sync_playwright().start()
        launch_options = {
            "user_data_dir": USER_DATA_DIR,
            "headless": False,
            "ignore_default_args": ["--enable-automation"],
            "args": [
                "--start-maximized",
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
            "no_viewport": True,
        }

        try:
            current_context = current_playwright.chromium.launch_persistent_context(
                channel="chrome", **launch_options
            )
        except Exception:
            current_context = current_playwright.chromium.launch_persistent_context(
                **launch_options
            )

        current_page = current_context.pages[0] if current_context.pages else current_context.new_page()
        current_page.goto(target_url, wait_until="domcontentloaded")

        blocked_json = json.dumps(list(get_blocked_ids()))

        js_setup = f"""
        (function() {{
            var targetUrl = {json.dumps(target_url)};
            var targetListId = {json.dumps(playlist_id)};
            var blockedIds = {blocked_json};
            var exactXpath = {json.dumps(PLAY_BUTTON_XPATH)};
            var attempts = 0;
            var maxAttempts = 50;

            function isBlockedHref(href) {{
                if (!href) return false;
                for (var i = 0; i < blockedIds.length; i++) {{
                    if (href.indexOf('list=' + blockedIds[i]) !== -1) {{
                        return true;
                    }}
                }}
                return false;
            }}

            function findTargetPlayButton() {{
                // 1. Try the exact XPath first
                var xpathResult = document.evaluate(
                    exactXpath,
                    document,
                    null,
                    XPathResult.FIRST_ORDERED_NODE_TYPE,
                    null
                );
                var exactNode = xpathResult.singleNodeValue;
                if (exactNode && !exactNode.closest('ytmusic-guide-renderer')) {{
                    return exactNode;
                }}

                // 2. Fallback strictly scoped to the main playlist header (never the left sidebar)
                var mainPage = document.querySelector('ytmusic-browse-response:not([hidden])') || document.querySelector('ytmusic-browse-response');
                if (!mainPage) return null;

                var fallbackBtn = mainPage.querySelector(
                    'ytmusic-editable-playlist-detail-header-renderer ytmusic-responsive-header-renderer ytmusic-play-button-renderer, ' +
                    'ytmusic-responsive-header-renderer ytmusic-play-button-renderer, ' +
                    'ytmusic-detail-header-renderer ytmusic-play-button-renderer'
                );
                if (fallbackBtn && !fallbackBtn.closest('ytmusic-guide-renderer')) {{
                    return fallbackBtn;
                }}

                return null;
            }}

            var poll = setInterval(function() {{
                attempts++;

                // Redirect immediately if browser lands on a blocked playlist (like list=LM)
                if (isBlockedHref(window.location.href)) {{
                    window.location.href = targetUrl;
                    return;
                }}

                // Wait until the exact requested playlist URL is active
                if (window.location.href.indexOf('list=' + targetListId) === -1) {{
                    return;
                }}

                var playTarget = findTargetPlayButton();
                if (playTarget) {{
                    playTarget.click();

                    // After playback starts, enable Shuffle on the player bar and set volume to 100%
                    setTimeout(function() {{
                        // Guard against any blocked playlist accidentally loading
                        var playerLinks = document.querySelectorAll('ytmusic-player-bar a[href*="list="]');
                        for (var j = 0; j < playerLinks.length; j++) {{
                            if (isBlockedHref(playerLinks[j].getAttribute('href'))) {{
                                var pauseBtn = document.querySelector('ytmusic-player-bar #play-pause-button');
                                if (pauseBtn) pauseBtn.click();
                                window.location.href = targetUrl;
                                return;
                            }}
                        }}

                        // Enable Shuffle if not already active
                        var shuffleBtn = document.querySelector(
                            'ytmusic-player-bar tp-yt-paper-icon-button.shuffle, ' +
                            'ytmusic-player-bar button[aria-label*="Shuffle"]'
                        );
                        if (shuffleBtn && shuffleBtn.getAttribute('aria-pressed') !== 'true') {{
                            shuffleBtn.click();
                        }}

                        // Set volume to 100%
                        var video = document.querySelector('video');
                        var slider = document.querySelector('#volume-slider');
                        if (video) {{ video.volume = 1.0; }}
                        if (slider) {{
                            slider.setAttribute('value', '100');
                            slider.setAttribute('aria-valuenow', '100');
                            slider.dispatchEvent(new Event('value-changed', {{ bubbles: true }}));
                            slider.dispatchEvent(new Event('change', {{ bubbles: true }}));
                            slider.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        }}
                    }}, 1500);

                    clearInterval(poll);
                }} else if (attempts >= maxAttempts) {{
                    clearInterval(poll);
                }}
            }}, 500);
        }})();
        """
        current_page.evaluate(js_setup)
        log(f"[Automation] ✔ '{name}' configured and playing at 100% volume.")
    except Exception as e:
        log(f"[Automation] ✖ Error launching '{name}': {e}")
        cleanup_browser_handles()


def close_youtube_music_with_fade():
    """Fades volume down smoothly over 3 seconds, turns off shuffle, stops media, and cleans up."""
    if not current_page or not current_context:
        log("[Automation] No active YouTube Music session to close.")
        cleanup_browser_handles(silent=True)
        return

    if not is_session_alive():
        log("[Automation] YouTube Music window was already closed manually; running cleanup...")
        cleanup_browser_handles()
        return

    log("[Automation] Initiating 3-second smooth volume fade out and disabling shuffle...")

    js_fade_and_stop = """
    (function() {
        var slider = document.querySelector('#volume-slider');
        var video = document.querySelector('video');

        function disableShuffle() {
            try {
                var shuffleBtn = document.querySelector(
                    'ytmusic-player-bar tp-yt-paper-icon-button.shuffle, ' +
                    'ytmusic-player-bar button[aria-label*="Shuffle"]'
                );
                if (shuffleBtn && shuffleBtn.getAttribute('aria-pressed') === 'true') {
                    shuffleBtn.click();
                }
            } catch (e) {}
        }

        if (!video) {
            disableShuffle();
            return new Promise((resolve) => setTimeout(resolve, 350));
        }

        var startVol = video.volume;
        var duration = 3000;
        var startTime = Date.now();
        var done = false;

        return new Promise((resolve) => {
            function finish() {
                if (done) return;
                done = true;
                clearInterval(timer);
                try {
                    video.volume = 0;
                    video.pause();
                    video.muted = true;
                } catch (e) {}

                disableShuffle();

                setTimeout(function() {
                    try {
                        video.removeAttribute('src');
                        video.load();
                    } catch (e) {}
                    resolve();
                }, 350);
            }

            // Use setInterval (not requestAnimationFrame) so backgrounded/minimized windows still fade smoothly
            var timer = setInterval(function() {
                var elapsed = Date.now() - startTime;
                var progress = Math.min(elapsed / duration, 1);
                var currentVol = startVol * (1 - progress);
                video.volume = currentVol;

                if (slider) {
                    var sliderVal = Math.round(currentVol * 100);
                    slider.setAttribute('value', sliderVal);
                    slider.setAttribute('aria-valuenow', sliderVal);
                    slider.dispatchEvent(new Event('value-changed', { bubbles: true }));
                    slider.dispatchEvent(new Event('change', { bubbles: true }));
                    slider.dispatchEvent(new Event('input', { bubbles: true }));
                }

                if (progress >= 1) {
                    finish();
                }
            }, 50);

            // Hard failsafe so fade never stalls past 3.5 seconds
            setTimeout(finish, 3500);
        });
    })();
    """

    try:
        current_page.set_default_timeout(5000)
        current_page.evaluate(js_fade_and_stop)
    except Exception as e:
        log(f"[Automation] Fade note: {e}")

    cleanup_browser_handles()
    log("[Automation] ✔ Shuffle turned off and YouTube Music closed successfully.")


def automation_worker():
    """Single worker thread that owns config watching, scheduler, and Playwright actions."""
    terminate_lingering_profile_processes()
    sanitize_profile_directory()

    load_config_and_schedules(is_reload=False)
    log("Church Automation Service is running and listening for scheduled events.")

    last_file_check = 0

    while not stop_event.is_set():
        # 1. Process manual test/reload commands from the web control panel
        try:
            action, scenario_key = command_queue.get_nowait()
            if action == "open":
                open_youtube_music(scenario_key=scenario_key)
            elif action == "close":
                close_youtube_music_with_fade()
            elif action == "reload":
                load_config_and_schedules(is_reload=True)
        except queue.Empty:
            pass

        # 2. Detect if someone manually closed the Chrome window and clean up stale handles
        if (current_context or current_playwright) and not is_session_alive():
            log("[Automation] Detected manual browser close; cleaning up session handles...")
            cleanup_browser_handles()

        # 3. Auto-reload if schedule_config.json was saved on disk
        now = time.time()
        if now - last_file_check >= 2.0:
            last_file_check = now
            try:
                if os.path.exists(CONFIG_PATH):
                    mtime = os.path.getmtime(CONFIG_PATH)
                    if mtime != last_config_mtime:
                        load_config_and_schedules(is_reload=True)
            except Exception:
                pass

        # 4. Run scheduled jobs
        schedule.run_pending()
        time.sleep(0.2)

    cleanup_browser_handles()


# ==================== LOCAL WEB CONTROL PANEL ====================
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Church Media Automation & Scenario Tester</title>
<style>
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: #18181b;
    color: #f4f4f5;
    max-width: 720px;
    margin: 36px auto;
    padding: 0 20px;
  }
  h1 { margin-bottom: 4px; font-size: 24px; }
  .sub { color: #a1a1aa; font-size: 14px; margin-bottom: 18px; }
  .top-bar {
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-wrap: wrap;
    gap: 10px;
    margin-bottom: 18px;
  }
  .badge {
    display: inline-block;
    background: #14532d;
    color: #86efac;
    padding: 4px 12px;
    border-radius: 999px;
    font-size: 12px;
    font-weight: 600;
  }
  .config-path {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 11.5px;
    color: #a1a1aa;
    background: #27272a;
    padding: 4px 10px;
    border-radius: 6px;
  }
  .grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 12px;
    margin-bottom: 12px;
  }
  .controls-row {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 12px;
    margin-bottom: 22px;
  }
  button {
    border: none;
    border-radius: 8px;
    padding: 14px 16px;
    color: #fff;
    font-size: 14px;
    font-weight: 600;
    cursor: pointer;
    text-align: left;
    line-height: 1.4;
    transition: opacity 0.15s;
  }
  button:hover { opacity: 0.9; }
  button:active { transform: scale(0.99); }
  .btn-blue { background: #2563eb; }
  .btn-red { background: #dc2626; }
  .btn-gray { background: #3f3f46; }
  .btn-sub { display: block; font-size: 12px; font-weight: 400; opacity: 0.85; }
  .section-title { font-size: 13px; font-weight: 600; color: #a1a1aa; margin-bottom: 6px; }
  table {
    width: 100%;
    border-collapse: collapse;
    background: #09090b;
    border: 1px solid #27272a;
    border-radius: 8px;
    overflow: hidden;
    margin-bottom: 20px;
    font-size: 12.5px;
  }
  th, td {
    padding: 9px 12px;
    text-align: left;
    border-bottom: 1px solid #27272a;
  }
  th { color: #a1a1aa; background: #121215; font-weight: 600; }
  tr:last-child td { border-bottom: none; }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; color: #93c5fd; }
  #logs {
    background: #09090b;
    border: 1px solid #27272a;
    border-radius: 8px;
    padding: 14px;
    height: 220px;
    overflow-y: auto;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 12.5px;
    line-height: 1.5;
    white-space: pre-wrap;
    color: #d4d4d8;
  }
</style>
</head>
<body>
  <h1>Church Media Automation</h1>
  <div class="sub">Edit <strong>schedule_config.json</strong> to update playlists or service times anytime—changes auto-reload.</div>

  <div class="top-bar">
    <div class="badge" id="statusBadge">● Schedule Active</div>
    <div class="config-path" id="configPath">schedule_config.json</div>
  </div>

  <div class="grid" id="scenarioButtons"></div>

  <div class="controls-row">
    <button class="btn-red" onclick="trigger('close', null)">
      ⏹ Test Fade Out & Close
      <span class="btn-sub">3-Sec Volume Fade + Quit Browser</span>
    </button>
    <button class="btn-gray" onclick="trigger('reload', null)">
      🔄 Reload Config File
      <span class="btn-sub">Re-read schedule_config.json now</span>
    </button>
  </div>

  <div class="section-title">Active Schedule (from schedule_config.json)</div>
  <table>
    <thead>
      <tr>
        <th>Service</th>
        <th>Days</th>
        <th>Start – End</th>
        <th>Playlist ID</th>
      </tr>
    </thead>
    <tbody id="scheduleTable"></tbody>
  </table>

  <div class="section-title">Live Activity Log</div>
  <div id="logs">Connecting...</div>

<script>
  function formatTime12(t24) {
    if (!t24 || !t24.includes(':')) return t24 || '—';
    const [hStr, mStr] = t24.split(':');
    let h = parseInt(hStr, 10);
    const ampm = h >= 12 ? 'PM' : 'AM';
    h = h % 12 || 12;
    return `${h}:${mStr} ${ampm}`;
  }

  function formatDays(days) {
    if (!Array.isArray(days) || days.length === 0) return 'None';
    return days.map(d => d.charAt(0).toUpperCase() + d.slice(1, 3)).join(', ');
  }

  function extractId(url) {
    if (!url || !url.includes('list=')) return 'Invalid URL';
    return url.split('list=')[1].split('&')[0];
  }

  function trigger(action, scenarioKey) {
    fetch('/trigger', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: action, scenario_key: scenarioKey })
    }).then(() => setTimeout(refreshConfig, 300));
  }

  async function refreshConfig() {
    try {
      const res = await fetch('/config');
      const data = await res.json();
      document.getElementById('configPath').textContent = data.config_path;

      const scenarios = data.scenarios || {};
      const btnContainer = document.getElementById('scenarioButtons');
      const tableBody = document.getElementById('scheduleTable');

      btnContainer.innerHTML = '';
      tableBody.innerHTML = '';

      const keys = Object.keys(scenarios);
      document.getElementById('statusBadge').textContent = `● Schedule Active (${keys.length} Services)`;

      keys.forEach(key => {
        const item = scenarios[key];
        const name = item.name || key;
        const dayText = formatDays(item.days);
        const start12 = formatTime12(item.start_time);
        const end12 = formatTime12(item.end_time);
        const listId = extractId(item.url);

        const btn = document.createElement('button');
        btn.className = 'btn-blue';
        btn.onclick = () => trigger('open', key);
        btn.innerHTML = `▶ Test ${name}<span class="btn-sub">${dayText} • ${start12} – ${end12}</span>`;
        btnContainer.appendChild(btn);

        const row = document.createElement('tr');
        row.innerHTML = `
          <td><strong>${name}</strong></td>
          <td>${dayText}</td>
          <td>${start12} – ${end12} (${item.start_time}–${item.end_time})</td>
          <td class="mono">${listId}</td>
        `;
        tableBody.appendChild(row);
      });
    } catch (e) {}
  }

  async function refreshLogs() {
    try {
      const res = await fetch('/logs');
      const lines = await res.json();
      const box = document.getElementById('logs');
      const atBottom = box.scrollHeight - box.scrollTop <= box.clientHeight + 30;
      box.textContent = lines.join('\n');
      if (atBottom) box.scrollTop = box.scrollHeight;
    } catch (e) {}
  }

  setInterval(refreshLogs, 1000);
  setInterval(refreshConfig, 3000);
  refreshConfig();
  refreshLogs();
</script>
</body>
</html>
"""


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


class ControlPanelHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path == "/":
            body = HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/logs":
            with log_lock:
                body = json.dumps(log_history).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/config":
            with config_lock:
                payload = {
                    "config_path": CONFIG_PATH,
                    "scenarios": current_config.get("scenarios", {})
                }
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/trigger":
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length) or b"{}")
            command_queue.put((
                data.get("action"),
                data.get("scenario_key")
            ))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
        else:
            self.send_response(404)
            self.end_headers()


if __name__ == "__main__":
    worker_thread = threading.Thread(target=automation_worker, daemon=True)
    worker_thread.start()

    server = None
    port = 8765
    for candidate_port in range(8765, 8775):
        try:
            server = ReusableThreadingHTTPServer(("127.0.0.1", candidate_port), ControlPanelHandler)
            port = candidate_port
            break
        except OSError:
            continue

    def handle_shutdown_signal(signum, frame):
        if not stop_event.is_set():
            log("Shutting down Church Automation Service...")
            stop_event.set()
            if server:
                threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, handle_shutdown_signal)
    signal.signal(signal.SIGTERM, handle_shutdown_signal)

    url = f"http://127.0.0.1:{port}"
    log(f"Control Panel ready at {url}")
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    finally:
        stop_event.set()
        worker_thread.join(timeout=6.0)
        cleanup_browser_handles(silent=True)
        server.server_close()
