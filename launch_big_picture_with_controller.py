"""
Lives in the system tray and watches connected Xbox controllers for presses
of the Guide/Xbox button:
  - Exactly 2 presses in quick succession -> opens Steam Big Picture.
    The controller gets a strong confirm pulse, immediately followed by a
    light, high-frequency "whisper" buzz that fades out over ~2 seconds.
  - Exactly 3 presses in quick succession -> closes Steam Big Picture
    (with the same strong confirm pulse, fired immediately - not after
    everything else finishes), then closes the regular Steam window
    (which minimizes it to the tray if that Steam setting is enabled).
  - Any other count (1, 4, 5...) is ignored entirely.

Tray icon:
  - Uses this program's own icon (the .exe's embedded icon once compiled;
    pythonw's icon when run as a plain script). When disabled, the same icon
    is shown with its saturation set to 0 (greyscale).
  - Right-click for:  Enabled (checkbox, click to toggle)  /  Quit
  - Left-click (double-click) also toggles enabled/disabled.

Dependencies:
    pip install pygetwindow pystray pillow

Everything else uses only the standard library (ctypes) to call:
  - XInputGetStateEx (undocumented, ordinal 100) to read the guide button,
    since the public XInputGetState hides it on purpose.
  - XInputSetState (documented/public) to drive rumble motors.

Run with pythonw.exe (no console window):
    pythonw.exe launch_big_picture_with_controller.py

"""

import ctypes
import ctypes.wintypes
import os
import sys
import threading
import time

import pygetwindow as gw
import pystray
from PIL import Image

# ---------------------------------------------------------------------------
# Simple file logging (since this normally runs with no console window).
# ---------------------------------------------------------------------------
LOG_PATH = os.path.join(os.environ.get("TEMP", "."), "xbox_guide_bigpicture.log")


def _log(message):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {message}\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
PRESS_WINDOW = 0.45          # seconds allowed between presses in one "burst"
POLL_INTERVAL = 0.03          # ~33 times per second
MAX_CONTROLLERS = 4
GUIDE_BUTTON_BIT = 0x0400
ACTION_COOLDOWN = 1.0         # seconds to ignore new bursts after an action fires

# ---------------------------------------------------------------------------
# Single-instance guard
# ---------------------------------------------------------------------------
_mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "XboxGuideBigPictureMutex")
if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
    sys.exit(0)

# ---------------------------------------------------------------------------
# XInput structures
# ---------------------------------------------------------------------------
class XINPUT_GAMEPAD(ctypes.Structure):
    _fields_ = [
        ("wButtons", ctypes.c_ushort),
        ("bLeftTrigger", ctypes.c_ubyte),
        ("bRightTrigger", ctypes.c_ubyte),
        ("sThumbLX", ctypes.c_short),
        ("sThumbLY", ctypes.c_short),
        ("sThumbRX", ctypes.c_short),
        ("sThumbRY", ctypes.c_short),
    ]


class XINPUT_STATE(ctypes.Structure):
    _fields_ = [
        ("dwPacketNumber", ctypes.c_ulong),
        ("Gamepad", XINPUT_GAMEPAD),
    ]


class XINPUT_VIBRATION(ctypes.Structure):
    _fields_ = [
        ("wLeftMotorSpeed", ctypes.c_ushort),   # low-frequency / strong motor
        ("wRightMotorSpeed", ctypes.c_ushort),  # high-frequency / weak motor
    ]


def load_xinput_dll():
    for dll_name in ("xinput1_4.dll", "xinput1_3.dll", "xinput1_2.dll", "xinput1_1.dll"):
        try:
            return ctypes.WinDLL(dll_name)
        except OSError:
            continue
    return None


_xinput_dll = load_xinput_dll()
if _xinput_dll is None:
    sys.exit("Could not load any xinput DLL.")

# Guide button state - undocumented ordinal 100
XInputGetStateEx = _xinput_dll[100]
XInputGetStateEx.argtypes = [ctypes.c_uint, ctypes.POINTER(XINPUT_STATE)]
XInputGetStateEx.restype = ctypes.c_uint

# Rumble - normal documented export
XInputSetState = _xinput_dll.XInputSetState
XInputSetState.argtypes = [ctypes.c_uint, ctypes.POINTER(XINPUT_VIBRATION)]
XInputSetState.restype = ctypes.c_uint


def rumble(index, left, right, duration):
    """Set both motors, hold for `duration` seconds, then stop."""
    vib = XINPUT_VIBRATION(left, right)
    XInputSetState(index, ctypes.byref(vib))
    time.sleep(duration)
    XInputSetState(index, ctypes.byref(XINPUT_VIBRATION(0, 0)))


def rumble_confirm_feedback(index):
    """Short confirm pulse used for BOTH 'opening Big Picture' and 'closing
    Big Picture' - same feel for both, on purpose.

    Ramped up and back down instead of snapping straight to full strength,
    so it reads as a firm tap rather than a jolt, and leans mostly on the
    low-frequency/strong motor (with a bit more of the high-frequency motor
    than before, for extra bite) the way a solid physical thump would feel.
    Stronger than the original version of this pulse."""
    _rumble_ramp(index, peak_left=26000, peak_right=12000,
                 ramp_up=0.05, hold=0.04, ramp_down=0.08)


def rumble_open_whisper(index, duration=4.0, peak_right=7500, steps=80):
    """Subtle high-frequency cue that plays right after the open confirm
    pulse: only the high-frequency/weak motor is used, at low amplitude,
    and it fades out continuously starting the instant it begins (no hold
    at peak) over `duration` seconds - a light buzz trailing off, rather
    than a pulse that holds then drops."""
    if steps <= 0 or duration <= 0:
        return
    step_time = duration / steps
    for s in range(steps):
        # Linear fade from full peak_right down toward 0 as s goes 0 -> steps.
        remaining = 1.0 - (s / steps)
        right = int(peak_right * remaining)
        XInputSetState(index, ctypes.byref(XINPUT_VIBRATION(0, right)))
        time.sleep(step_time)
    XInputSetState(index, ctypes.byref(XINPUT_VIBRATION(0, 0)))


def _rumble_ramp(index, peak_left, peak_right, ramp_up, hold, ramp_down, steps=8):
    """Fade both motors up to (peak_left, peak_right), hold briefly, then
    fade back down to 0 in small steps, instead of a single hard on/off."""

    def _segment(start_l, start_r, end_l, end_r, seconds):
        if steps <= 0 or seconds <= 0:
            return
        step_time = seconds / steps
        for s in range(1, steps + 1):
            t = s / steps
            left = int(start_l + (end_l - start_l) * t)
            right = int(start_r + (end_r - start_r) * t)
            XInputSetState(index, ctypes.byref(XINPUT_VIBRATION(left, right)))
            time.sleep(step_time)

    _segment(0, 0, peak_left, peak_right, ramp_up)
    if hold > 0:
        XInputSetState(index, ctypes.byref(XINPUT_VIBRATION(peak_left, peak_right)))
        time.sleep(hold)
    _segment(peak_left, peak_right, 0, 0, ramp_down)
    XInputSetState(index, ctypes.byref(XINPUT_VIBRATION(0, 0)))


# ---------------------------------------------------------------------------
# Steam actions
# ---------------------------------------------------------------------------
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
WM_CLOSE = 0x0010


def open_big_picture():
    try:
        os.startfile("steam://open/bigpicture")
    except OSError:
        for candidate in (
            r"C:\Program Files (x86)\Steam\steam.exe",
            r"C:\Program Files\Steam\steam.exe",
        ):
            if os.path.exists(candidate):
                os.startfile(candidate + " -bigpicture")
                break


def close_big_picture():
    # Primary method: Steam's own close command.
    try:
        os.startfile("steam://close/bigpicture")
    except OSError:
        pass

    # Fallback: find the Big Picture window directly and ask it to close.
    hwnd = user32.FindWindowW(None, "Steam Big Picture Mode")
    if hwnd:
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)


def _get_process_name(pid):
    """Return the lowercase exe filename for a process ID, or '' on failure.
    Used only for diagnostics."""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    hproc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not hproc:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(260)
        size = ctypes.wintypes.DWORD(260)
        if kernel32.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
        return ""
    finally:
        kernel32.CloseHandle(hproc)


def _steam_window_exists():
    """Return True if a window titled exactly 'Steam' or 'Steam ' is currently open."""
    try:
        return any(window.title in ("Steam", "Steam ") for window in gw.getAllWindows())
    except Exception as e:
        _log(f"pygetwindow error while checking for Steam window: {e}")
        # Unknown state - treat as "still there" so we keep trying rather than
        # stopping on a false negative.
        return True


def minimize_steam_to_tray(duration=5.0, interval=0.2):
    """
    For up to `duration` seconds, repeatedly attempt to close any window
    titled exactly "Steam" or "Steam " (trailing space seen in some Steam
    builds), via pygetwindow. Closing it this way is the same as clicking
    the window's X button, so it minimizes to the tray if that Steam
    setting is enabled, or exits Steam if it isn't.

    Before and after each close attempt, this checks whether the Steam
    window actually exists. If it existed beforehand and is confirmed gone
    afterward, the close succeeded and we stop immediately instead of
    continuing to poll for the rest of `duration`.
    """
    deadline = time.monotonic() + duration

    while time.monotonic() < deadline:
        existed_before = _steam_window_exists()

        if existed_before:
            try:
                for window in gw.getAllWindows():
                    if window.title in ("Steam", "Steam "):
                        _log(f"Closing window titled {window.title!r} via pygetwindow")
                        window.close()
            except Exception as e:
                _log(f"pygetwindow error while closing: {e}")

            existed_after = _steam_window_exists()

            if existed_before and not existed_after:
                _log("Steam window existed before close and is gone after - done.")
                return

            _log("Steam window still present after close attempt; will keep polling.")

        time.sleep(interval)

    # Nothing found - dump every window with "steam" in the title for diagnosis.
    _log(f"Timed out after {duration}s without finding a window titled 'Steam'. "
         "Windows with 'steam' in the title:")
    try:
        for window in gw.getAllWindows():
            if "steam" in window.title.lower():
                pid = ctypes.wintypes.DWORD()
                user32.GetWindowThreadProcessId(window._hWnd, ctypes.byref(pid))
                proc = _get_process_name(pid.value) or "<access denied>"
                _log(f"  hwnd={window._hWnd} title={window.title!r} process={proc}")
    except Exception as e:
        _log(f"pygetwindow error while dumping diagnostics: {e}")


# ---------------------------------------------------------------------------
# Shared state between the tray (main thread) and the poller (worker thread)
# ---------------------------------------------------------------------------
enabled_event = threading.Event()
enabled_event.set()              # start enabled
stop_event = threading.Event()   # set when the user picks Quit


# ---------------------------------------------------------------------------
# Poll loop (runs on a background thread)
# ---------------------------------------------------------------------------
def poll_loop():
    state = XINPUT_STATE()
    was_pressed = [False] * MAX_CONTROLLERS
    burst_count = [0] * MAX_CONTROLLERS
    last_press_time = [0.0] * MAX_CONTROLLERS
    cooldown_until = [0.0] * MAX_CONTROLLERS

    while not stop_event.is_set():
        # While disabled, do nothing and forget any half-finished bursts so
        # re-enabling starts from a clean slate.
        if not enabled_event.is_set():
            was_pressed = [False] * MAX_CONTROLLERS
            burst_count = [0] * MAX_CONTROLLERS
            stop_event.wait(0.1)
            continue

        try:
            now = time.monotonic()

            for i in range(MAX_CONTROLLERS):
                ctypes.memset(ctypes.byref(state), 0, ctypes.sizeof(state))
                result = XInputGetStateEx(i, ctypes.byref(state))
                if result != 0:
                    was_pressed[i] = False
                    continue

                is_pressed = bool(state.Gamepad.wButtons & GUIDE_BUTTON_BIT)

                # Rising edge = a new press
                if is_pressed and not was_pressed[i] and now > cooldown_until[i]:
                    if now - last_press_time[i] <= PRESS_WINDOW:
                        burst_count[i] += 1
                    else:
                        burst_count[i] = 1
                    last_press_time[i] = now

                was_pressed[i] = is_pressed

                # Finalize a burst once enough time has passed since the last press
                if burst_count[i] > 0 and now - last_press_time[i] > PRESS_WINDOW:
                    count = burst_count[i]
                    burst_count[i] = 0

                    if count == 2:
                        _log(f"Controller {i}: double press -> opening Big Picture")
                        open_big_picture()
                        cooldown_until[i] = now + ACTION_COOLDOWN
                        rumble_confirm_feedback(i)   # strong pulse first, so the user knows it worked
                        rumble_open_whisper(i)       # then a subtle fading high-frequency cue
                    elif count == 3:
                        _log(f"Controller {i}: triple press -> closing Big Picture")
                        close_big_picture()
                        cooldown_until[i] = now + ACTION_COOLDOWN
                        rumble_confirm_feedback(i)   # fired immediately, not after the tray-minimize wait
                        minimize_steam_to_tray()
                    # any other count: ignored, no action, no rumble
        except Exception as e:
            _log(f"Error in poll loop: {e}")

        stop_event.wait(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Tray icon
# ---------------------------------------------------------------------------
gdi32 = ctypes.windll.gdi32

user32.GetDC.restype = ctypes.c_void_p
user32.GetDC.argtypes = [ctypes.c_void_p]
user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
user32.DrawIconEx.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
                              ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
user32.DestroyIcon.argtypes = [ctypes.c_void_p]
user32.LoadIconW.restype = ctypes.c_void_p
user32.LoadIconW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
user32.PrivateExtractIconsW.restype = ctypes.c_uint
user32.PrivateExtractIconsW.argtypes = [ctypes.c_wchar_p, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_uint),
                                        ctypes.c_uint, ctypes.c_uint]
gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
gdi32.CreateDIBSection.restype = ctypes.c_void_p
gdi32.CreateDIBSection.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
                                   ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.c_uint]
gdi32.SelectObject.restype = ctypes.c_void_p
gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
gdi32.DeleteDC.argtypes = [ctypes.c_void_p]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32), ("biHeight", ctypes.c_int32),
        ("biPlanes", ctypes.c_uint16), ("biBitCount", ctypes.c_uint16),
        ("biCompression", ctypes.c_uint32), ("biSizeImage", ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_int32), ("biYPelsPerMeter", ctypes.c_int32),
        ("biClrUsed", ctypes.c_uint32), ("biClrImportant", ctypes.c_uint32),
    ]


def _render_hicon_on(hicon, size, fill_byte):
    """Draw an HICON onto a size x size 32-bit bitmap pre-filled with
    `fill_byte` (0x00 = black, 0xFF = white) and return the raw BGRA bytes."""
    screen_dc = user32.GetDC(None)
    mem_dc = gdi32.CreateCompatibleDC(screen_dc)
    bmi = BITMAPINFOHEADER()
    bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.biWidth = size
    bmi.biHeight = -size          # top-down
    bmi.biPlanes = 1
    bmi.biBitCount = 32
    bits = ctypes.c_void_p()
    hbmp = gdi32.CreateDIBSection(screen_dc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
    old = gdi32.SelectObject(mem_dc, hbmp)
    try:
        ctypes.memset(bits.value, fill_byte, size * size * 4)
        user32.DrawIconEx(mem_dc, 0, 0, hicon, size, size, 0, None, 3)  # DI_NORMAL
        gdi32.GdiFlush()
        return bytes((ctypes.c_ubyte * (size * size * 4)).from_address(bits.value))
    finally:
        gdi32.SelectObject(mem_dc, old)
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(None, screen_dc)


def _hicon_to_image(hicon, size):
    """Convert an HICON to an RGBA PIL image, keeping transparency.
    The icon is rendered once over black and once over white; comparing the
    two recovers each pixel's alpha and true colour."""
    on_black = _render_hicon_on(hicon, size, 0x00)
    on_white = _render_hicon_on(hicon, size, 0xFF)
    out = bytearray(size * size * 4)
    for p in range(0, size * size * 4, 4):
        b0, g0, r0 = on_black[p], on_black[p + 1], on_black[p + 2]
        b1, g1, r1 = on_white[p], on_white[p + 1], on_white[p + 2]
        a = 255 - ((r1 - r0) + (g1 - g0) + (b1 - b0)) // 3
        a = max(0, min(255, a))
        if a:
            out[p] = min(255, r0 * 255 // a)
            out[p + 1] = min(255, g0 * 255 // a)
            out[p + 2] = min(255, b0 * 255 // a)
        out[p + 3] = a
    return Image.frombytes("RGBA", (size, size), bytes(out))


def load_app_icon(size=64):
    """Return this program's own icon as an RGBA PIL image: the embedded icon
    of the running executable (the compiled .exe, or pythonw.exe if run as a
    script). Falls back to the stock Windows application icon."""
    hicon = ctypes.c_void_p()
    icon_id = ctypes.c_uint()
    try:
        got = user32.PrivateExtractIconsW(sys.executable, 0, size, size,
                                          ctypes.byref(hicon), ctypes.byref(icon_id), 1, 0)
        if got and hicon.value:
            try:
                return _hicon_to_image(hicon.value, size)
            finally:
                user32.DestroyIcon(hicon.value)
    except Exception as e:
        _log(f"Could not extract icon from {sys.executable}: {e}")

    # Fallback: stock IDI_APPLICATION icon (shared handle, don't destroy).
    try:
        return _hicon_to_image(user32.LoadIconW(None, ctypes.c_void_p(32512)), size)
    except Exception as e:
        _log(f"Could not load fallback icon: {e}")
        return Image.new("RGBA", (size, size), (120, 120, 120, 255))


def desaturate(img):
    """Saturation 0: greyscale the colour channels, keep alpha untouched."""
    r, g, b, a = img.split()
    grey = Image.merge("RGB", (r, g, b)).convert("L")
    return Image.merge("RGBA", (grey, grey, grey, a))


ICON_ON = load_app_icon()
ICON_OFF = desaturate(ICON_ON)


def _tray_title():
    state = "Enabled" if enabled_event.is_set() else "Disabled"
    return f"Launch with Xbox Button ({state})"


def on_toggle(icon, item):
    if enabled_event.is_set():
        enabled_event.clear()
        _log("Disabled from tray")
    else:
        enabled_event.set()
        _log("Enabled from tray")
    icon.icon = ICON_ON if enabled_event.is_set() else ICON_OFF
    icon.title = _tray_title()
    icon.update_menu()


def on_quit(icon, item):
    _log("Quit from tray")
    stop_event.set()
    icon.stop()


def main():
    menu = pystray.Menu(
        pystray.MenuItem(
            "Enabled",
            on_toggle,
            checked=lambda item: enabled_event.is_set(),
            default=True,   # left/double-click on the tray icon toggles too
        ),
        pystray.MenuItem("Quit", on_quit),
    )
    icon = pystray.Icon(
        "xbox_guide_bigpicture",
        ICON_ON,
        _tray_title(),
        menu,
    )

    worker = threading.Thread(target=poll_loop, name="xinput-poll", daemon=True)
    worker.start()

    _log("Started")
    try:
        icon.run()   # blocks on the main thread until icon.stop()
    finally:
        stop_event.set()
        # Make sure the controller isn't left rumbling on exit.
        for i in range(MAX_CONTROLLERS):
            try:
                XInputSetState(i, ctypes.byref(XINPUT_VIBRATION(0, 0)))
            except Exception:
                pass
        _log("Exited")


if __name__ == "__main__":
    main()
