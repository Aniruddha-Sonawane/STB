"""
STB Media Player  –  Tata Sky-inspired
======================================
Keyboard controls
  0-9          type channel number → shown top-right as small 3-digit underlined slots
  Enter        confirm channel / play highlighted programme
  Left / Right browse channels → shows full EPG bar for prev/next channel (video keeps playing)
  Up / Down    navigate programme rows IN the EPG bar (highlighted row scrolls)
  I            toggle EPG bar
  M            toggle mail envelope
  Vol keys     show volume bar on right
  Escape       quit / close programme list
"""

import tkinter as tk
from tkinter import messagebox, filedialog
import vlc, sys, random, threading, os, glob, time as _t, datetime

# ─────────────────────────────────────────────────────────
#  Colour palette
# ─────────────────────────────────────────────────────────
C_BG          = "#0A0C14"
C_PROG_ACTIVE = "#1A4FA3"
C_PROG_NEXT   = "#0D1525"
C_BORDER      = "#2C3254"
C_WHITE       = "#FFFFFF"
C_LIGHT       = "#CBD5E1"
C_DIM         = "#64748B"
C_GREEN       = "#22C55E"
C_YELLOW      = "#FACC15"
C_BLUE_DOT    = "#3B82F6"
C_PROGRESS_BG = "#1E2540"
C_PROGRESS_FG = "#3B82F6"
C_DIVIDER     = "#1E2A45"
C_BADGE_BG    = "#0A0C14"

IMAGES_DIR       = os.path.join(os.path.dirname(os.path.abspath(__file__)), "images")
IMG_EXTS         = ("*.jpg", "*.jpeg", "*.png", "*.gif", "*.bmp", "*.webp")
MAIL_INTERVAL_MS = 60 * 60 * 1000   # 1 hour
MAX_EPG_ROWS     = 6                 # visible programme rows in EPG at once


# ═════════════════════════════════════════════════════════
class MediaPlayer:
# ═════════════════════════════════════════════════════════

    def __init__(self, root):
        self.root = root
        self.root.title("STB Media Player")
        self.root.attributes("-fullscreen", True)
        self.root.configure(bg="black")

        self.channels            = self._load_channels()
        self.current_channel     = {}
        self.channel_request_id  = 0

        # typed-channel buffer (up to 3 digits)
        self.channel_buffer   = ""
        self.buffer_job       = None

        # left/right channel browse (shows EPG of target, keeps video playing)
        self._browse_num      = None
        self._browse_hide_job = None

        # EPG programme-row navigation
        self._epg_row_index   = 0     # which row is highlighted (0 = currently playing)
        self._epg_items       = []    # [(title, time_str, url_or_none), ...]

        # time-continuity
        self.channel_state: dict = {}

        # next-video preload
        self._preload_result  = None
        self._preload_channel = None

        # mail icon
        self._mail_visible  = False
        self._mail_hide_job = None

        # volume
        self._volume    = 70
        self._vol_hide  = None
        self._epg_tick  = None

        # VLC
        try:
            self.instance = vlc.Instance()
            self.player   = self.instance.media_player_new()
            self.player.audio_set_volume(self._volume)
        except Exception:
            messagebox.showerror("VLC Error", "VLC not detected.")
            sys.exit(1)

        self.video_panel = tk.Frame(root, bg="black")
        self.video_panel.pack(fill=tk.BOTH, expand=True)

        root.bind("<Key>", self.on_keypress)

        self.epg_window = None
        self.hide_job   = None
        self._prepare_epg()
        self._prepare_mail()
        self._schedule_next_mail()
        self._prepare_ch_badge()
        self._prepare_vol_bar()

        self._img_pool    = self._scan_images()
        self._current_img = None

    # ══════════════════════════════════════════════════════
    #  HELPERS
    # ══════════════════════════════════════════════════════

    def _load_channels(self):
        import json
        fn = os.path.join(os.path.dirname(os.path.abspath(__file__)), "channels.json")
        try:
            with open(fn, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return {}
        for num, info in data.items():
            info.setdefault("number", num)
        return data

    def _scan_images(self):
        files = []
        for ext in IMG_EXTS:
            files.extend(glob.glob(os.path.join(IMAGES_DIR, ext)))
        return files

    def _pick_image(self, w, h):
        if not self._img_pool:
            return None
        path = random.choice(self._img_pool)
        try:
            from PIL import Image, ImageTk
            img = Image.open(path)
            # Cover mode: scale so shortest side fills, then centre-crop
            img_w, img_h = img.size
            scale = max(w / img_w, h / img_h)
            new_w = int(img_w * scale)
            new_h = int(img_h * scale)
            img   = img.resize((new_w, new_h), Image.LANCZOS)
            left  = (new_w - w) // 2
            top   = (new_h - h) // 2
            img   = img.crop((left, top, left + w, top + h))
            return ImageTk.PhotoImage(img)
        except Exception:
            pass
        try:
            photo = tk.PhotoImage(file=path)
            # native PhotoImage: just subsample to fit (no crop support)
            sw = max(1, photo.width()  // w)
            sh = max(1, photo.height() // h)
            s  = min(sw, sh)   # use min so image fills (may overflow, but better than letterbox)
            return photo.subsample(s, s) if s > 1 else photo
        except Exception:
            return None

    def _sorted_keys(self):
        return sorted(self.channels.keys(), key=lambda x: int(x))

    # ══════════════════════════════════════════════════════
    #  KEYBOARD
    # ══════════════════════════════════════════════════════

    def on_keypress(self, event=None):
        key = event.keysym

        # digits → channel input badge (max 3)
        if key.isdigit():
            if len(self.channel_buffer) < 3:
                self.channel_buffer += key
            self._show_ch_badge(self.channel_buffer)
            if self.buffer_job:
                self.root.after_cancel(self.buffer_job)
            self.buffer_job = self.root.after(3000, self._auto_confirm_channel)
            return

        if key == "Return":
            if self._browse_num is not None:
                self._confirm_browse()
            elif self.channel_buffer:
                self._confirm_typed_channel()
            elif self._epg_row_index > 0:
                # user navigated down to a future programme row – play it
                self._epg_activate_row()
            return

        # Left/Right → browse channel EPG (video keeps playing)
        if key == "Left":
            self._browse_channel_delta(-1)
            return
        if key == "Right":
            self._browse_channel_delta(+1)
            return

        # Up/Down → scroll EPG programme rows
        if key == "Down":
            self._epg_row_move(+1)
            return
        if key == "Up":
            self._epg_row_move(-1)
            return

        if key in ("i", "I"):
            if self.epg_window.winfo_viewable():
                self.hide_epg()
            else:
                self.show_epg()
            return

        if key in ("m", "M"):
            self._hide_mail() if self._mail_visible else self._show_mail()
            return

        if key in ("XF86AudioRaiseVolume", "plus", "equal"):
            self._change_volume(+5)
            return
        if key in ("XF86AudioLowerVolume", "minus"):
            self._change_volume(-5)
            return

        if key in ("l", "L"):
            self._show_language_picker()
            return

        if key == "Escape":
            # reset row navigation first
            if self._epg_row_index != 0:
                self._epg_row_index = 0
                self._render_epg_rows()
            else:
                self.root.quit()

    # ══════════════════════════════════════════════════════
    #  CHANNEL NUMBER BADGE  — small, top-right, 3 underlined slots
    # ══════════════════════════════════════════════════════

    def _prepare_ch_badge(self):
        self._badge_win = tk.Toplevel(self.root)
        self._badge_win.withdraw()
        self._badge_win.overrideredirect(True)
        self._badge_win.attributes("-topmost", True)
        self._badge_win.attributes("-alpha", 0.93)
        self._badge_win.configure(bg=C_BADGE_BG)

        # thin blue border frame
        border = tk.Frame(self._badge_win, bg=C_PROG_ACTIVE, bd=0)
        border.pack(padx=2, pady=2)

        inner = tk.Frame(border, bg=C_BADGE_BG)
        inner.pack(padx=6, pady=6)

        # 3 digit-slot canvases side by side
        self._digit_canvases = []
        slot_frame = tk.Frame(inner, bg=C_BADGE_BG)
        slot_frame.pack()
        SLOT_W, SLOT_H = 22, 30
        for i in range(3):
            c = tk.Canvas(slot_frame, width=SLOT_W, height=SLOT_H,
                          bg=C_BADGE_BG, highlightthickness=0, bd=0)
            c.pack(side=tk.LEFT, padx=2)
            # underline bar always visible
            c.create_line(0, SLOT_H - 3, SLOT_W, SLOT_H - 3,
                          fill=C_WHITE, width=2)
            # digit text (starts blank)
            tid = c.create_text(SLOT_W // 2, SLOT_H // 2 - 2,
                                text="", fill=C_WHITE,
                                font=("Arial", 16, "bold"), anchor="center")
            self._digit_canvases.append((c, tid))

        # small channel name below
        self._badge_name = tk.Label(inner, text="", fg=C_DIM, bg=C_BADGE_BG,
                                    font=("Arial", 9))
        self._badge_name.pack(pady=(3, 0))

    def _show_ch_badge(self, num_str, name=""):
        # fill digit slots
        for i, (c, tid) in enumerate(self._digit_canvases):
            ch_digit = num_str[i] if i < len(num_str) else ""
            c.itemconfig(tid, text=ch_digit)
        self._badge_name.config(text=name)

        self._badge_win.update_idletasks()
        self.root.update_idletasks()
        rw = self.root.winfo_width()
        rh = self.root.winfo_height()
        rx = self.root.winfo_rootx()
        ry = self.root.winfo_rooty()
        bw = self._badge_win.winfo_reqwidth()
        bh = self._badge_win.winfo_reqheight()
        # top-right: 2/3 from left, 1/6 from top  (≈ "right eye" position)
        x = rx + rw - bw - int(rw * 0.05)
        y = ry + int(rh * 0.12)
        self._badge_win.geometry(f"+{x}+{y}")
        self._badge_win.deiconify()
        self._badge_win.lift()

    def _hide_ch_badge(self):
        self._badge_win.withdraw()

    def _auto_confirm_channel(self):
        if self.channel_buffer:
            self._confirm_typed_channel()

    def _confirm_typed_channel(self):
        num = self.channel_buffer
        self.channel_buffer = ""
        self._hide_ch_badge()
        ch = self.channels.get(num)
        if ch:
            self.switch_channel(ch)
        else:
            messagebox.showinfo("Channel", f"Channel {num} not found")

    # ══════════════════════════════════════════════════════
    #  LEFT/RIGHT CHANNEL BROWSE  —  shows target channel EPG, video unchanged
    # ══════════════════════════════════════════════════════

    def _browse_channel_delta(self, delta: int):
        keys = self._sorted_keys()
        if not keys:
            return
        # start from current browse position, or current channel, or first
        ref = (self._browse_num or
               self.current_channel.get("number") or keys[0])
        try:
            idx = keys.index(ref)
        except ValueError:
            idx = 0
        idx = (idx + delta) % len(keys)
        self._browse_num = keys[idx]

        target_ch = self.channels[self._browse_num]

        # Show EPG bar of the target channel (video stays playing)
        # Build a simple item list from target channel schedule for the EPG rows
        browse_items = []
        for s in target_ch.get("schedule", []):
            time_str = f"{s.get('start','')} – {s.get('end','')}" if s.get("start") else ""
            browse_items.append((s.get("title", ""), time_str, None))
        src = target_ch.get("source", "")
        if not browse_items:
            name = target_ch.get("name", "")
            browse_items = [(name, "", None), ("", "", None)]

        # Temporarily override EPG display for browse preview
        num  = target_ch.get("number", "")
        name = target_ch.get("name", "")
        self.epg_ch_num.config(text=f" {num} ")
        self.epg_ch_name.config(text=name)

        # Row A = first schedule item or channel name  (blue band)
        row_a = browse_items[0] if browse_items else ("", "", None)
        row_b = browse_items[1] if len(browse_items) > 1 else ("", "", None)
        self.epg_active_title.config(text=row_a[0])
        self.epg_active_time.config(text=row_a[1])
        self.epg_next_title.config(text=row_b[0])
        self.epg_next_time.config(text=row_b[1])

        # refresh image and date
        import datetime
        self.date_label.config(
            text=datetime.datetime.now().strftime("%a %d %b  %I:%M %p"))
        photo = self._pick_image(190, 190)
        if photo:
            self._current_img = photo
            self._epg_img_canvas.itemconfig(self._epg_img_id, image=photo)

        # Show EPG bar without auto-hiding (user is browsing)
        self.root.update_idletasks()
        rw = self.root.winfo_width()
        rh = self.root.winfo_height()
        rx = self.root.winfo_rootx()
        ry = self.root.winfo_rooty()
        w  = min(1100, int(rw * 0.94))
        h  = 240
        x  = rx + (rw - w) // 2
        y  = ry + rh - h - 36
        self.epg_window.geometry(f"{w}x{h}+{x}+{y}")
        self.epg_window.deiconify()
        self.epg_window.lift()

        if self.hide_job:
            self.root.after_cancel(self.hide_job)
        self._browse_hide_job and self.root.after_cancel(self._browse_hide_job)
        self._browse_hide_job = self.root.after(5000, self._cancel_browse)

    def _confirm_browse(self):
        if self._browse_hide_job:
            self.root.after_cancel(self._browse_hide_job)
        num = self._browse_num
        self._browse_num = None
        self.hide_epg()
        ch = self.channels.get(num)
        if ch:
            self.switch_channel(ch)

    def _cancel_browse(self):
        self._browse_num = None
        # restore current channel EPG
        if self.current_channel:
            self._update_epg(self.current_channel)

    # ══════════════════════════════════════════════════════
    #  EPG ROW NAVIGATION  (Up/Down arrows scroll programme rows)
    # ══════════════════════════════════════════════════════

    def _build_epg_items(self, ch):
        """Build list of (title, time_str, url_or_None) for this channel."""
        items = []
        src   = ch.get("source", "")
        is_yt = isinstance(src, str) and (src.startswith("yt:") or "youtube.com" in src)

        if is_yt:
            # row 0 = currently playing video title
            cur_title = ch.get("_current_title", "Now Playing")
            items.append((cur_title, "Now Playing", None))

            # preloaded next video (has a real title already resolved)
            if self._preload_channel is ch and self._preload_result:
                pt = self._preload_result[1] or "Next Video"
                items.append((pt, "Up Next", "__preload__"))

            # remaining items from yt_list - use stored titles if available
            yt_titles = ch.get("_yt_titles", {})   # url → title cache
            for url in ch.get("_yt_list", [])[:20]:
                if url == ch.get("_resolved_src"):
                    continue   # skip currently playing
                title = yt_titles.get(url, "")
                if not title:
                    # extract a readable title from the flat playlist entry if available
                    entry_title = ch.get("_yt_entry_titles", {}).get(url, "")
                    title = entry_title if entry_title else url.split("v=")[-1][:11] if "v=" in url else "Video"
                items.append((title, "", url))
        else:
            for s in ch.get("schedule", []):
                t = f"{s.get('start','')} – {s.get('end','')}" if s.get("start") else ""
                items.append((s.get("title", ""), t, None))

        if not items:
            items = [("No programme info", "", None)]
        return items

    def _epg_row_move(self, delta: int):
        ch = self.current_channel
        if not ch:
            return
        if not self._epg_items:
            self._epg_items = self._build_epg_items(ch)

        if not self.epg_window.winfo_viewable():
            # EPG hidden → open it with row 0 selected, don't move yet
            self._epg_row_index = 0
            self._render_epg_rows()
            self.show_epg(auto_hide=0)
            return

        # EPG visible → move the blue selector immediately
        new_idx = self._epg_row_index + delta
        new_idx = max(0, min(new_idx, len(self._epg_items) - 1))
        self._epg_row_index = new_idx
        self._render_epg_rows()
        if self.hide_job:
            self.root.after_cancel(self.hide_job)
        self.hide_job = self.root.after(8000, self.hide_epg)

    def _epg_activate_row(self):
        """Play the currently highlighted non-zero EPG row."""
        if not self._epg_items or self._epg_row_index == 0:
            return
        title, _, url = self._epg_items[self._epg_row_index]
        self._epg_row_index = 0
        ch  = self.current_channel
        rid = self.channel_request_id

        if url == "__preload__" and self._preload_result:
            su, t, h = self._preload_result
            self._preload_result  = None
            self._preload_channel = None
            ch["_current_title"]  = t
            self._play_media_source(ch, su, request_id=rid, title=t, headers=h)

        elif url and url.startswith("http"):
            ch["_current_title"] = title
            self.channel_request_id += 1
            rid2 = self.channel_request_id
            threading.Thread(
                target=self._resolve_and_play,
                args=(ch, url, rid2), daemon=True).start()

    def _render_epg_rows(self):
        """Blue band = currently selected row. Dark band = next row below it."""
        items = self._epg_items
        idx   = self._epg_row_index

        sel      = items[idx]          if idx     < len(items) else ("", "", None)
        sel_next = items[idx + 1]      if idx + 1 < len(items) else ("", "", None)

        self.epg_active_title.config(text=sel[0])
        self.epg_active_time.config(text=sel[1])
        self.epg_next_title.config(text=sel_next[0])
        self.epg_next_time.config(text=sel_next[1])

    # ══════════════════════════════════════════════════════
    #  LANGUAGE PICKER  (L key)
    # ══════════════════════════════════════════════════════

    def _show_language_picker(self):
        ch  = self.current_channel
        src = ch.get("source", "") if ch else ""
        is_yt = isinstance(src, str) and (src.startswith("yt:") or "youtube.com" in src)
        if not is_yt:
            return

        url = ch.get("_resolved_src") or ch.get("source", "")
        if not url:
            return

        # Build popup
        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.attributes("-alpha", 0.95)
        win.configure(bg=C_BG)

        border = tk.Frame(win, bg=C_PROG_ACTIVE, bd=0)
        border.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)
        tk.Frame(border, bg="#3B6FD4", height=3).pack(fill=tk.X)

        inner = tk.Frame(border, bg=C_BG)
        inner.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)

        tk.Label(inner, text="Select Audio Language", fg=C_WHITE, bg=C_BG,
                 font=("Arial", 13, "bold"), pady=10).pack()
        tk.Frame(inner, bg=C_DIVIDER, height=1).pack(fill=tk.X, padx=10)

        loading = tk.Label(inner, text="Fetching available audio tracks…",
                           fg=C_DIM, bg=C_BG, font=("Arial", 11), pady=12)
        loading.pack()

        # close on Escape
        win.bind("<Escape>", lambda e: win.destroy())
        win.bind("<Key>", lambda e: win.destroy() if e.keysym == "Escape" else None)
        win.focus_force()

        # Position centre screen
        self.root.update_idletasks()
        rw = self.root.winfo_width()
        rh = self.root.winfo_height()
        rx = self.root.winfo_rootx()
        ry = self.root.winfo_rooty()
        W, H = 500, 340
        win.geometry(f"{W}x{H}+{rx + (rw-W)//2}+{ry + (rh-H)//2}")

        def _fetch():
            tracks = self._get_audio_tracks(url)
            self.root.after(0, lambda: _populate(tracks))

        def _populate(tracks):
            if not win.winfo_exists():
                return
            loading.destroy()
            if not tracks:
                tk.Label(inner, text="No alternate audio tracks found.",
                         fg=C_DIM, bg=C_BG, font=("Arial", 11), pady=16).pack()
                return

            sel_var = tk.IntVar(value=0)
            for i, (lang, label, fmt_id) in enumerate(tracks):
                row_bg = C_PROG_ACTIVE if i == 0 else C_BG
                row_fg = C_WHITE       if i == 0 else C_LIGHT

                def _make_row(idx, bg, fg, lid, fid, lbl):
                    row = tk.Frame(inner, bg=bg, cursor="hand2")
                    row.pack(fill=tk.X, padx=8, pady=2)
                    prefix = tk.Label(row, text="▶  " if idx == 0 else "    ",
                                      fg=fg, bg=bg, font=("Arial", 12), padx=8, pady=5)
                    prefix.pack(side=tk.LEFT)
                    tk.Label(row, text=lbl, fg=fg, bg=bg,
                             font=("Arial", 12), pady=5, anchor="w").pack(
                             side=tk.LEFT, fill=tk.X, expand=True)

                    def _hover_on(e, r=row, lbl2=prefix):
                        r.config(bg=C_PROG_ACTIVE)
                        for w in r.winfo_children():
                            w.config(bg=C_PROG_ACTIVE, fg=C_WHITE)

                    def _hover_off(e, r=row, oidx=idx):
                        bg2 = C_PROG_ACTIVE if oidx == 0 else C_BG
                        fg2 = C_WHITE       if oidx == 0 else C_LIGHT
                        r.config(bg=bg2)
                        for w in r.winfo_children():
                            w.config(bg=bg2, fg=fg2)

                    def _click(e, fid2=fid, lid2=lid):
                        win.destroy()
                        self._switch_audio_track(fid2, lid2)

                    row.bind("<Enter>",  _hover_on)
                    row.bind("<Leave>",  _hover_off)
                    row.bind("<Button-1>", _click)
                    for w in row.winfo_children():
                        w.bind("<Button-1>", _click)

                _make_row(i, row_bg, row_fg, lang, fmt_id, label)

            tk.Frame(inner, bg=C_DIVIDER, height=1).pack(fill=tk.X, padx=10, pady=(8, 4))
            tk.Label(inner, text="Click to select   ·   Esc to close",
                     fg=C_DIM, bg=C_BG, font=("Arial", 9), pady=4).pack()

        threading.Thread(target=_fetch, daemon=True).start()

    def _get_audio_tracks(self, url):
        """Return list of (lang_code, display_label, format_id) for audio tracks."""
        try:
            import yt_dlp as ydl
        except ImportError:
            return []
        opts = {"quiet": True, "skip_download": True, "noplaylist": True, "no_warnings": True}
        try:
            with ydl.YoutubeDL(opts) as y:
                info = y.extract_info(url, download=False)
        except Exception:
            return []
        if not info:
            return []

        seen   = set()
        tracks = []
        # First add the default/auto track
        tracks.append(("auto", "🌐  Default (Auto)", None))

        for fmt in info.get("formats", []):
            if fmt.get("vcodec", "none") not in ("none", None):
                continue  # video-only or combined – skip for pure audio tracks
            lang = fmt.get("language") or fmt.get("language_preference")
            if not lang:
                continue
            if lang in seen:
                continue
            seen.add(lang)
            acodec = fmt.get("acodec", "")
            abr    = fmt.get("abr") or fmt.get("tbr") or 0
            label  = f"🔊  {lang.upper()}  ({acodec}  {int(abr)}kbps)" if abr else f"🔊  {lang.upper()}  ({acodec})"
            tracks.append((lang, label, fmt.get("format_id")))

        return tracks

    def _switch_audio_track(self, fmt_id, lang):
        """Re-resolve current video with a specific audio format and play it."""
        ch  = self.current_channel
        url = ch.get("_resolved_src") or ch.get("source", "")
        if not url or not url.startswith("http"):
            return

        ch["_current_title"] = ch.get("_current_title", "") + f"  [{lang.upper()}]"
        self.channel_request_id += 1
        rid = self.channel_request_id

        def _work():
            try:
                import yt_dlp as ydl
            except ImportError:
                return
            # If fmt_id is None → default auto track
            opts = {"quiet": True, "skip_download": True,
                    "noplaylist": True, "no_warnings": True}
            if fmt_id:
                opts["format"] = f"bestvideo+{fmt_id}/best"
            try:
                with ydl.YoutubeDL(opts) as y:
                    info = y.extract_info(url, download=False)
            except Exception:
                return
            if not info:
                return
            su      = None
            headers = info.get("http_headers") or {}
            title   = info.get("title", ch.get("_current_title", ""))
            direct  = info.get("url")
            if direct and direct.startswith("http"):
                su = direct
            if not su:
                for fmt in info.get("formats", []):
                    if fmt_id and fmt.get("format_id") == fmt_id:
                        su = fmt.get("url")
                        break
            if not su:
                su, title, headers = self.resolve_youtube_stream(url)
            if su:
                self.root.after(0, lambda: self._play_media_source(
                    ch, su, request_id=rid, title=title, headers=headers))

        threading.Thread(target=_work, daemon=True).start()

    # ══════════════════════════════════════════════════════
    #  VOLUME BAR
    # ══════════════════════════════════════════════════════

    def _prepare_vol_bar(self):
        self._vol_win = tk.Toplevel(self.root)
        self._vol_win.withdraw()
        self._vol_win.overrideredirect(True)
        self._vol_win.attributes("-topmost", True)
        self._vol_win.attributes("-alpha", 0.93)
        self._vol_win.configure(bg=C_BG)

        border = tk.Frame(self._vol_win, bg=C_PROG_ACTIVE, bd=0)
        border.pack(padx=2, pady=2)
        inner = tk.Frame(border, bg=C_BG)
        inner.pack(padx=6, pady=(10, 6))

        BARS  = 20
        BAR_W = 38
        BAR_H = 8
        GAP   = 5
        canvas_h = BARS * (BAR_H + GAP)

        self._vol_canvas = tk.Canvas(inner, bg=C_BG, highlightthickness=0,
                                     width=BAR_W, height=canvas_h)
        self._vol_canvas.pack()

        self._bar_ids = []
        for i in range(BARS):
            y1 = canvas_h - (i + 1) * (BAR_H + GAP) + GAP
            y2 = y1 + BAR_H
            bid = self._vol_canvas.create_rectangle(
                0, y1, BAR_W, y2, fill=C_PROGRESS_BG, outline="")
            self._bar_ids.append(bid)

        self._vol_pct = tk.Label(inner, text="70%", fg=C_WHITE, bg=C_BG,
                                 font=("Arial", 10, "bold"))
        self._vol_pct.pack(pady=(6, 0))

        # fixed window size so geometry always works before first show
        self._VOL_W = BAR_W + 28   # border + padding
        self._VOL_H = canvas_h + 60

    def _change_volume(self, delta: int):
        self._volume = max(0, min(100, self._volume + delta))
        self.player.audio_set_volume(self._volume)
        self._show_vol_bar()

    def _show_vol_bar(self):
        filled = round(self._volume / 100 * len(self._bar_ids))
        for i, bid in enumerate(self._bar_ids):
            self._vol_canvas.itemconfig(
                bid, fill=C_PROGRESS_FG if i < filled else C_PROGRESS_BG)
        self._vol_pct.config(text=f"{self._volume}%")

        self.root.update_idletasks()
        rw = self.root.winfo_width()
        rh = self.root.winfo_height()
        rx = self.root.winfo_rootx()
        ry = self.root.winfo_rooty()
        x  = rx + rw - self._VOL_W - int(rw * 0.025)
        y  = ry + (rh - self._VOL_H) // 2
        self._vol_win.geometry(f"{self._VOL_W}x{self._VOL_H}+{x}+{y}")
        self._vol_win.deiconify()
        self._vol_win.lift()
        if self._vol_hide:
            self.root.after_cancel(self._vol_hide)
        self._vol_hide = self.root.after(2500, self._hide_vol_bar)

    def _hide_vol_bar(self):
        self._vol_win.withdraw()

    # ══════════════════════════════════════════════════════
    #  MAIL ICON  (small envelope, top-centre)
    # ══════════════════════════════════════════════════════

    def _prepare_mail(self):
        self._mail_win = tk.Toplevel(self.root)
        self._mail_win.withdraw()
        self._mail_win.overrideredirect(True)
        self._mail_win.attributes("-topmost", True)
        self._mail_win.attributes("-alpha", 0.92)
        self._mail_win.configure(bg=C_BADGE_BG)

        border = tk.Frame(self._mail_win, bg=C_PROG_ACTIVE, bd=0)
        border.pack(padx=2, pady=2)
        tk.Label(border, text="✉", fg=C_YELLOW, bg=C_BADGE_BG,
                 font=("Arial", 22), padx=12, pady=6).pack()

    def _show_mail(self):
        self._mail_visible = True
        self.root.update_idletasks()
        self._mail_win.update_idletasks()
        rw = self.root.winfo_width()
        rx = self.root.winfo_rootx()
        ry = self.root.winfo_rooty()
        mw = self._mail_win.winfo_reqwidth()
        x  = rx + (rw - mw) // 2
        y  = ry + 28
        self._mail_win.geometry(f"+{x}+{y}")
        self._mail_win.deiconify()
        self._mail_win.lift()
        if self._mail_hide_job:
            self.root.after_cancel(self._mail_hide_job)
        self._mail_hide_job = self.root.after(6000, self._hide_mail)

    def _hide_mail(self):
        self._mail_visible = False
        self._mail_win.withdraw()

    def _schedule_next_mail(self):
        jitter = random.randint(-600_000, 600_000)
        delay  = max(10_000, MAIL_INTERVAL_MS + jitter)
        self.root.after(delay, self._trigger_mail)

    def _trigger_mail(self):
        self._show_mail()
        self._schedule_next_mail()

    # ══════════════════════════════════════════════════════
    #  TIME-CONTINUITY
    # ══════════════════════════════════════════════════════

    def _snapshot(self):
        prev = self.current_channel
        if not prev:
            return
        num = prev.get("number")
        if not num:
            return
        pos = self.player.get_time()
        if pos < 0:
            pos = 0
        s = self.channel_state.setdefault(num, {})
        s["position_ms"] = pos
        s["left_at"]     = _t.time()
        if "_resolved_src" in prev:
            s["source"] = prev["_resolved_src"]

    def _resume_ms(self, ch) -> int:
        s = self.channel_state.get(ch.get("number"))
        if not s:
            return 0
        return s.get("position_ms", 0) + int((_t.time() - s.get("left_at", 0)) * 1000)

    # ══════════════════════════════════════════════════════
    #  CHANNEL SWITCHING
    # ══════════════════════════════════════════════════════

    def switch_channel(self, channel: dict):
        self._snapshot()
        self.current_channel    = channel
        self.channel_request_id += 1
        self._epg_row_index     = 0
        self._epg_items         = []
        rid = self.channel_request_id
        src = channel.get("source", "")

        if isinstance(src, str) and (src.startswith("yt:") or "youtube.com" in src):
            saved     = self.channel_state.get(channel.get("number", ""), {})
            saved_src = saved.get("source")

            if self._preload_channel is channel and self._preload_result:
                su, t, h = self._preload_result
                self._preload_result  = None
                self._preload_channel = None
                channel["_current_title"] = t or ""
                self._play_media_source(channel, su, request_id=rid, title=t, headers=h)

            elif saved_src:
                self._play_media_source(channel, saved_src, request_id=rid,
                                        title=channel.get("_current_title", ""))
            else:
                channel["_current_title"] = "Loading…"
                self.show_epg()
                threading.Thread(
                    target=self._load_yt_channel,
                    args=(channel, src, rid), daemon=True).start()
            return

        if src and os.path.isdir(src):
            files = []
            for ext in ("*.mp4", "*.mkv", "*.avi", "*.mov", "*.wmv"):
                files.extend(glob.glob(os.path.join(src, ext)))
            if files:
                src = files[0]

        self._play_media_source(channel, src, request_id=rid)

    def _load_yt_channel(self, ch, src, rid):
        try:
            if "_yt_list" not in ch or not ch["_yt_list"]:
                yt_list, title_map = self.fetch_youtube_videos(src)
                ch["_yt_list"]         = yt_list
                ch["_yt_entry_titles"] = title_map
            yt = ch.get("_yt_list", [])
            if not yt:
                self.root.after(0, lambda: self._show_channel_error(
                    rid, f"No videos found for channel {ch.get('number','')}"))
                return
            random.shuffle(yt)
            su = t = h = None
            for url in yt[:15]:
                su, t, h = self.resolve_youtube_stream(url)
                if su:
                    break
            if not su:
                self.root.after(0, lambda: self._show_channel_error(
                    rid, f"Cannot resolve stream for {ch.get('name','')}"))
                return
            self.root.after(0, lambda: self._play_media_source(
                ch, su, request_id=rid, title=t, headers=h))
        except Exception as e:
            msg = str(e)
            self.root.after(0, lambda: self._show_channel_error(rid, msg))

    # ══════════════════════════════════════════════════════
    #  NEXT-VIDEO PRELOAD
    # ══════════════════════════════════════════════════════

    def _preload_next(self, ch):
        src = ch.get("source", "")
        if not (isinstance(src, str) and (src.startswith("yt:") or "youtube.com" in src)):
            return

        def _work():
            yt  = ch.get("_yt_list", [])
            if not yt:
                yt_list, title_map = self.fetch_youtube_videos(src)
                ch["_yt_list"]         = yt_list
                ch["_yt_entry_titles"] = title_map
                yt = yt_list
            if not yt:
                return
            cur  = ch.get("_resolved_src", "")
            pool = [u for u in yt if u != cur] or yt[:]
            random.shuffle(pool)
            for url in pool[:15]:
                su, t, h = self.resolve_youtube_stream(url)
                if su:
                    self._preload_result  = (su, t, h)
                    self._preload_channel = ch
                    self.root.after(0, self._on_preload_ready)
                    return

        threading.Thread(target=_work, daemon=True).start()

    def _on_preload_ready(self):
        """Called on main thread when preload finishes – refresh EPG next row."""
        if self._preload_result and self._preload_channel is self.current_channel:
            t = self._preload_result[1] or ""
            if self._epg_items and len(self._epg_items) > 1:
                # update or prepend preload entry
                if self._epg_items[1][2] != "__preload__":
                    self._epg_items.insert(1, (t, "", "__preload__"))
                else:
                    self._epg_items[1] = (t, "", "__preload__")
            self._render_epg_rows()

    # ══════════════════════════════════════════════════════
    #  MEDIA PLAYBACK
    # ══════════════════════════════════════════════════════

    def _apply_headers(self, media, headers):
        if not headers:
            return
        ua  = headers.get("User-Agent") or headers.get("user-agent")
        ref = headers.get("Referer")    or headers.get("referer")
        if ua:  media.add_option(f":http-user-agent={ua}")
        if ref: media.add_option(f":http-referrer={ref}")
        media.add_option(":network-caching=1500")

    def _play_media_source(self, ch, src, request_id=None, title=None, headers=None):
        if request_id is not None and request_id != self.channel_request_id:
            return
        if title:
            ch["_current_title"] = title
        ch["_resolved_src"] = src

        # rebuild EPG items for this channel
        self._epg_items     = self._build_epg_items(ch)
        self._epg_row_index = 0

        self.show_epg()
        self.stop()

        if src:
            m = self.instance.media_new(src)
            self._apply_headers(m, headers)
            self.player.set_media(m)
            self._set_video_window()
            self.player.audio_set_volume(self._volume)
            self.player.play()

            seek = self._resume_ms(ch)
            if seek > 0:
                self._seek_when_ready(seek, request_id)

            self._preload_next(ch)
            self._start_tick(request_id)
        else:
            self._show_channel_error(
                request_id,
                f"Channel {ch.get('number','')} has no playable source")

    def _resolve_and_play(self, ch, url, rid):
        su, t, h = self.resolve_youtube_stream(url)
        if su:
            self.root.after(0, lambda: self._play_media_source(
                ch, su, request_id=rid, title=t, headers=h))
        else:
            self.root.after(0, lambda: self._show_channel_error(
                rid, "Could not resolve stream for selected video"))

    def _seek_when_ready(self, ms, rid, attempt=0):
        if rid != self.channel_request_id or attempt > 30:
            return
        if self.player.get_state() == vlc.State.Playing:
            dur = self.player.get_length()
            if dur > 0:
                ms = min(ms, dur - 2000)
            if ms > 0:
                self.player.set_time(ms)
        else:
            self.root.after(100, lambda: self._seek_when_ready(ms, rid, attempt + 1))

    def _show_channel_error(self, rid, msg):
        if rid is not None and rid != self.channel_request_id:
            return
        messagebox.showerror("Channel", msg)

    # progress ticker
    def _start_tick(self, rid):
        if self._epg_tick:
            self.root.after_cancel(self._epg_tick)
        self._do_tick(rid)

    def _do_tick(self, rid):
        if rid != self.channel_request_id:
            return
        try:
            dur = self.player.get_length()
            pos = self.player.get_time()
            if dur > 0 and pos >= 0:
                fw = max(2, min(219, int(220 * pos / dur)))
                self.epg_progress.coords(self.progress_fill, 0, 2, fw, 8)
        except Exception:
            pass
        self._epg_tick = self.root.after(500, lambda: self._do_tick(rid))

    # ══════════════════════════════════════════════════════
    #  VLC
    # ══════════════════════════════════════════════════════

    def _set_video_window(self):
        self.root.update_idletasks()
        h = self.video_panel.winfo_id()
        if sys.platform.startswith("win"):
            self.player.set_hwnd(h)
        elif sys.platform.startswith("linux"):
            self.player.set_xwindow(h)
        elif sys.platform == "darwin":
            self.player.set_nsobject(h)

    def open_file(self):
        f = filedialog.askopenfilename(
            filetypes=[("Video Files", "*.mp4 *.mkv *.avi *.mov *.wmv")])
        if not f:
            return
        m = self.instance.media_new(f)
        self.player.set_media(m)
        self._set_video_window()
        self.player.play()

    def play(self):  self.player.play()
    def pause(self): self.player.pause()
    def stop(self):  self.player.stop()

    # ══════════════════════════════════════════════════════
    #  YOUTUBE
    # ══════════════════════════════════════════════════════

    def fetch_youtube_videos(self, src) -> list:
        try:
            import yt_dlp as ydl
        except ImportError:
            try:
                import youtube_dl as ydl
            except ImportError:
                messagebox.showerror("Dependency", "yt-dlp required. pip install yt-dlp")
                return []
        opts = {"quiet": True, "ignoreerrors": True, "extract_flat": True,
                "skip_download": True, "no_warnings": True, "playlistend": 50}
        ext = src
        if src.startswith("yt:"):
            ext = f"https://www.youtube.com/channel/{src[3:]}/videos"
        elif "youtube.com/@" in src and not src.rstrip("/").endswith("/videos"):
            ext = f"{src.rstrip('/')}/videos"
        elif "youtube.com/channel/" in src and not src.rstrip("/").endswith("/videos"):
            ext = f"{src.rstrip('/')}/videos"
        elif "youtube.com/c/" in src and not src.rstrip("/").endswith("/videos"):
            ext = f"{src.rstrip('/')}/videos"
        elif "youtube.com/user/" in src and not src.rstrip("/").endswith("/videos"):
            ext = f"{src.rstrip('/')}/videos"
        try:
            with ydl.YoutubeDL(opts) as y:
                info = y.extract_info(ext, download=False)
        except Exception:
            return []
        if not info:
            return []
        out        = []
        title_map  = {}   # url → title from flat playlist metadata
        if "entries" in info:
            for e in info["entries"]:
                if not e:
                    continue
                u = e.get("webpage_url") or e.get("url")
                if u and not u.startswith("http"):
                    u = f"https://www.youtube.com/watch?v={u}"
                if u:
                    out.append(u)
                    t = e.get("title") or e.get("fulltitle") or ""
                    if t:
                        title_map[u] = t
        else:
            u = info.get("url") or info.get("webpage_url")
            if u and not u.startswith("http"):
                u = f"https://www.youtube.com/watch?v={u}"
            if u:
                out.append(u)
        return out, title_map

    def resolve_youtube_stream(self, url):
        try:
            import yt_dlp as ydl
        except ImportError:
            try:
                import youtube_dl as ydl
            except ImportError:
                return None, None, None
        opts = {"quiet": True, "ignoreerrors": True, "skip_download": True,
                "noplaylist": True, "no_warnings": True}
        try:
            with ydl.YoutubeDL(opts) as y:
                info = y.extract_info(url, download=False)
        except Exception:
            return None, None, None
        if not info:
            return None, None, None
        title   = info.get("title") or "YouTube Video"
        headers = info.get("http_headers") or {}
        direct  = info.get("url")
        if direct and direct.startswith("http"):
            return direct, title, headers
        best, best_s = None, -1
        for fmt in info.get("formats", []):
            u = fmt.get("url")
            if not u:
                continue
            if fmt.get("vcodec") == "none" or fmt.get("acodec") == "none":
                continue
            s = (fmt.get("height") or 0)
            if fmt.get("ext") == "mp4":    s += 10000
            if fmt.get("protocol","").startswith("http"): s += 1000
            if s > best_s:
                best_s, best = s, fmt
        if best:
            return best.get("url"), title, headers
        for fmt in reversed(info.get("formats", [])):
            if fmt.get("url"):
                return fmt.get("url"), title, headers
        return None, title, headers

    # ══════════════════════════════════════════════════════
    #  EPG OVERLAY BUILD
    # ══════════════════════════════════════════════════════

    def _prepare_epg(self):
        self.epg_window = tk.Toplevel(self.root)
        self.epg_window.withdraw()
        self.epg_window.overrideredirect(True)
        self.epg_window.attributes("-topmost", True)
        self.epg_window.attributes("-alpha", 0.92)
        self.epg_window.configure(bg=C_BG)

        outer = tk.Frame(self.epg_window, bg=C_BORDER)
        outer.pack(fill=tk.BOTH, expand=True)
        tk.Frame(outer, bg="#3B6FD4", height=3).pack(fill=tk.X)

        body = tk.Frame(outer, bg=C_BG)
        body.pack(fill=tk.BOTH, expand=True, padx=1, pady=(0, 1))

        layout = tk.Frame(body, bg=C_BG)
        layout.pack(fill=tk.BOTH, expand=True)

        # ── LEFT: pure square image ────────────────────────
        IMG_SZ = 190
        img_frame = tk.Frame(layout, bg=C_BG,
                             highlightbackground="#3B6FD4",
                             highlightthickness=2, bd=0)
        img_frame.pack(side=tk.LEFT, padx=(10, 8), pady=8)
        self._epg_img_canvas = tk.Canvas(img_frame,
                                         width=IMG_SZ, height=IMG_SZ,
                                         bg=C_PROG_NEXT,
                                         highlightthickness=0, bd=0)
        self._epg_img_canvas.pack()
        self._epg_img_id = self._epg_img_canvas.create_image(
            IMG_SZ // 2, IMG_SZ // 2, anchor="center")

        # ── RIGHT: info panel ──────────────────────────────
        info = tk.Frame(layout, bg=C_BG)
        info.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 8))

        # Row 1 – channel nav + date
        row1 = tk.Frame(info, bg=C_BG)
        row1.pack(fill=tk.X, pady=(10, 4))
        nav = tk.Frame(row1, bg=C_BG)
        nav.pack(side=tk.LEFT)
        tk.Label(nav, text="◄", fg="#7BA4E0", bg=C_BG,
                 font=("Arial", 13, "bold")).pack(side=tk.LEFT, padx=(0, 6))
        self.epg_ch_num = tk.Label(nav, text="", fg=C_WHITE, bg=C_PROG_ACTIVE,
                                   font=("Arial", 14, "bold"), padx=8, pady=2)
        self.epg_ch_num.pack(side=tk.LEFT, padx=(0, 8))
        self.epg_ch_name = tk.Label(nav, text="", fg=C_WHITE, bg=C_BG,
                                    font=("Arial", 15, "bold"), anchor="w")
        self.epg_ch_name.pack(side=tk.LEFT)
        tk.Label(nav, text="►", fg="#7BA4E0", bg=C_BG,
                 font=("Arial", 13, "bold")).pack(side=tk.LEFT, padx=(8, 0))
        date_chip = tk.Frame(row1, bg="#1A2340", padx=12, pady=4)
        date_chip.pack(side=tk.RIGHT, padx=(0, 4))
        self.date_label = tk.Label(date_chip, text="", fg=C_WHITE,
                                   bg="#1A2340", font=("Arial", 12))
        self.date_label.pack()

        tk.Frame(info, bg=C_DIVIDER, height=1).pack(fill=tk.X, padx=4)

        # Row 2 – highlighted/active programme  (BLUE band – same style as screenshot)
        ra = tk.Frame(info, bg=C_PROG_ACTIVE)
        ra.pack(fill=tk.X, padx=4, pady=(5, 2))
        self.epg_active_title = tk.Label(ra, text="", fg=C_WHITE,
                                         bg=C_PROG_ACTIVE, anchor="w",
                                         font=("Arial", 14, "bold"),
                                         padx=12, pady=6)
        self.epg_active_title.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.epg_active_time = tk.Label(ra, text="", fg="#BDD7FF",
                                        bg=C_PROG_ACTIVE,
                                        font=("Arial", 11), padx=12, pady=6)
        self.epg_active_time.pack(side=tk.RIGHT)

        # Row 3 – next programme  (darker bg – same style as screenshot)
        rn = tk.Frame(info, bg=C_PROG_NEXT)
        rn.pack(fill=tk.X, padx=4, pady=(0, 5))
        self.epg_next_title = tk.Label(rn, text="", fg=C_LIGHT,
                                       bg=C_PROG_NEXT, anchor="w",
                                       font=("Arial", 12), padx=12, pady=4)
        self.epg_next_title.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.epg_next_time = tk.Label(rn, text="", fg=C_DIM,
                                      bg=C_PROG_NEXT, font=("Arial", 10),
                                      padx=12, pady=4)
        self.epg_next_time.pack(side=tk.RIGHT)

        tk.Frame(info, bg=C_DIVIDER, height=1).pack(fill=tk.X, padx=4)

        # Row 4 – progress bar + buttons
        footer = tk.Frame(info, bg=C_BG)
        footer.pack(fill=tk.X, pady=(6, 8))
        prog_area = tk.Frame(footer, bg=C_BG)
        prog_area.pack(side=tk.LEFT, fill=tk.Y, anchor="w", padx=(4, 0))
        self.epg_progress = tk.Canvas(prog_area, width=220, height=10,
                                      bg=C_BG, highlightthickness=0, bd=0)
        self.epg_progress.pack(anchor="w")
        self.epg_progress.create_rectangle(0, 2, 220, 8, fill=C_PROGRESS_BG, outline="")
        self.progress_fill = self.epg_progress.create_rectangle(
            0, 2, 0, 8, fill=C_PROGRESS_FG, outline="")

        btn_frame = tk.Frame(footer, bg=C_BG)
        btn_frame.pack(side=tk.RIGHT, anchor="e", padx=(0, 8))
        for color, label in (
            ("#3B82F6", "🔍 Search"),
            (C_GREEN,   "● Genres"),
            (C_YELLOW,  "● Alerts"),
            (C_BLUE_DOT,"● Language"),
        ):
            item = tk.Frame(btn_frame, bg=C_BG)
            item.pack(side=tk.LEFT, padx=(0, 14))
            tk.Label(item, text=label, fg=color, bg=C_BG,
                     font=("Arial", 9, "bold")).pack()

    # ══════════════════════════════════════════════════════
    #  EPG UPDATE
    # ══════════════════════════════════════════════════════

    def _update_epg(self, ch, browsing=False):
        num  = ch.get("number", "")
        name = ch.get("name", "")
        self.epg_ch_num.config(text=f" {num} ")
        self.epg_ch_name.config(text=name)

        # square image
        photo = self._pick_image(190, 190)
        if photo:
            self._current_img = photo
            self._epg_img_canvas.itemconfig(self._epg_img_id, image=photo)

        # rebuild item list if needed
        if not browsing:
            self._epg_items = self._build_epg_items(ch)
        else:
            # for browse preview just show channel schedule/name
            items = []
            for s in ch.get("schedule", []):
                t = f"{s.get('start','')} – {s.get('end','')}" if s.get("start") else ""
                items.append((s.get("title", ""), t, None))
            if not items:
                items = [(ch.get("name", ""), "", None), ("", "", None)]
            self._epg_items = items

        self._epg_row_index = 0
        self._render_epg_rows()

        # date
        now = datetime.datetime.now()
        self.date_label.config(text=now.strftime("%a %d %b  %I:%M %p"))

        # progress bar (VLC-driven, only for current channel)
        if not browsing:
            dur = self.player.get_length()
            pos = self.player.get_time()
            if dur > 0 and pos >= 0:
                fw = max(2, min(219, int(220 * pos / dur)))
                self.epg_progress.coords(self.progress_fill, 0, 2, fw, 8)
            else:
                self.epg_progress.coords(self.progress_fill, 0, 2, 0, 8)

    # ══════════════════════════════════════════════════════
    #  EPG SHOW / HIDE
    # ══════════════════════════════════════════════════════

    def show_epg(self, auto_hide=5000):
        if self.current_channel:
            self._update_epg(self.current_channel)

        self.root.update_idletasks()
        rw = self.root.winfo_width()
        rh = self.root.winfo_height()
        rx = self.root.winfo_rootx()
        ry = self.root.winfo_rooty()
        w  = min(1100, int(rw * 0.94))
        h  = 240
        x  = rx + (rw - w) // 2
        y  = ry + rh - h - 36
        self.epg_window.geometry(f"{w}x{h}+{x}+{y}")
        self.epg_window.deiconify()
        self.epg_window.lift()

        if self.hide_job:
            self.root.after_cancel(self.hide_job)
        if auto_hide:
            self.hide_job = self.root.after(auto_hide, self.hide_epg)

    def hide_epg(self):
        self.epg_window.withdraw()
        # reset browse state when EPG closes
        if self._browse_num is not None:
            self._browse_num = None
            if self.current_channel:
                self._update_epg(self.current_channel)


# ══════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════

if __name__ == "__main__":
    root = tk.Tk()
    app  = MediaPlayer(root)
    root.mainloop()