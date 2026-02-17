import os
import re
import subprocess
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import vlc
import srt
from faster_whisper import WhisperModel


# ---------- yordamchi funksiyalar ----------
def sec_to_hhmmss(sec: float) -> str:
    sec = max(0.0, float(sec))
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def run_ffmpeg_extract_audio(video_path: str, wav_path: str):
    """
    Videodan WAV audio chiqarish (16kHz mono) - transkripsiya uchun qulay.
    """
    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-vn",
        "-ac", "1",
        "-ar", "16000",
        "-c:a", "pcm_s16le",
        wav_path
    ]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        raise RuntimeError("FFmpeg xatolik:\n" + (p.stderr[-2000:] if p.stderr else "Unknown error"))


def make_srt_from_segments(segments, srt_path: str):
    subs = []
    i = 1
    for seg in segments:
        start = float(seg.start)
        end = float(seg.end)
        text = (seg.text or "").strip()
        if not text:
            continue
        subs.append(
            srt.Subtitle(
                index=i,
                start=srt.timedelta(seconds=start),
                end=srt.timedelta(seconds=end),
                content=text
            )
        )
        i += 1
    data = srt.compose(subs)
    with open(srt_path, "w", encoding="utf-8") as f:
        f.write(data)


def load_srt_items(srt_path: str):
    with open(srt_path, "r", encoding="utf-8", errors="ignore") as f:
        data = f.read()
    subs = list(srt.parse(data))
    items = []
    for sub in subs:
        start_sec = sub.start.total_seconds()
        end_sec = sub.end.total_seconds()
        txt = sub.content.replace("\n", " ").strip()
        items.append((start_sec, end_sec, txt))
    return items


def normalize_text(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"\s+", " ", s)
    return s


# ---------- Asosiy App ----------
class VideoSearchApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Video -> Subtitle -> Qidirish -> O'sha joydan qo'yish")
        self.geometry("1200x720")

        self.video_path = ""
        self.audio_path = ""
        self.srt_path = ""
        self.srt_items = []  # (start_sec, end_sec, text)

        # VLC player
        self.instance = vlc.Instance()
        self.player = self.instance.media_player_new()

        self._build_ui()

    def _build_ui(self):
        top = ttk.Frame(self)
        top.pack(side=tk.TOP, fill=tk.X, padx=10, pady=10)

        self.btn_load = ttk.Button(top, text="1) Video yuklash", command=self.load_video)
        self.btn_load.pack(side=tk.LEFT, padx=5)

        self.btn_make = ttk.Button(top, text="2) Audio + SRT yaratish", command=self.make_subtitles_thread)
        self.btn_make.pack(side=tk.LEFT, padx=5)

        ttk.Label(top, text="Til:").pack(side=tk.LEFT, padx=(20, 5))
        self.lang_var = tk.StringVar(value="uz")  # "uz" yoki "auto"
        self.lang_combo = ttk.Combobox(top, width=8, textvariable=self.lang_var, values=["uz", "auto", "ru", "en"])
        self.lang_combo.pack(side=tk.LEFT)

        ttk.Label(top, text="Model:").pack(side=tk.LEFT, padx=(20, 5))
        self.model_var = tk.StringVar(value="small")
        self.model_combo = ttk.Combobox(top, width=10, textvariable=self.model_var,
                                        values=["tiny", "base", "small", "medium", "large-v3"])
        self.model_combo.pack(side=tk.LEFT)

        self.progress = ttk.Label(top, text="Holat: tayyor")
        self.progress.pack(side=tk.LEFT, padx=20)

        mid = ttk.Frame(self)
        mid.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=10, pady=10)

        # Chap: VLC video panel
        left = ttk.Frame(mid)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.video_panel = tk.Frame(left, bg="black", width=700, height=400)
        self.video_panel.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        controls = ttk.Frame(left)
        controls.pack(side=tk.TOP, fill=tk.X, pady=8)

        self.btn_play = ttk.Button(controls, text="Play", command=self.play_video)
        self.btn_play.pack(side=tk.LEFT, padx=4)

        self.btn_pause = ttk.Button(controls, text="Pause", command=self.pause_video)
        self.btn_pause.pack(side=tk.LEFT, padx=4)

        self.btn_stop = ttk.Button(controls, text="Stop", command=self.stop_video)
        self.btn_stop.pack(side=tk.LEFT, padx=4)

        ttk.Label(controls, text="Sekund:").pack(side=tk.LEFT, padx=(20, 5))
        self.seek_entry = ttk.Entry(controls, width=10)
        self.seek_entry.pack(side=tk.LEFT)
        ttk.Button(controls, text="Go", command=self.seek_manual).pack(side=tk.LEFT, padx=4)

        # O‘ng: qidiruv + natijalar + segmentlar
        right = ttk.Frame(mid)
        right.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=(10, 0))

        search_box = ttk.LabelFrame(right, text="Qidirish")
        search_box.pack(side=tk.TOP, fill=tk.X)

        self.query_var = tk.StringVar()
        self.query_entry = ttk.Entry(search_box, textvariable=self.query_var)
        self.query_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8, pady=8)
        self.query_entry.bind("<Return>", lambda e: self.search_now())

        self.btn_search = ttk.Button(search_box, text="Qidir", command=self.search_now)
        self.btn_search.pack(side=tk.LEFT, padx=8, pady=8)

        self.match_mode = tk.StringVar(value="contains")  # contains/exact
        ttk.Radiobutton(search_box, text="Ichida bor", variable=self.match_mode, value="contains").pack(side=tk.LEFT)
        ttk.Radiobutton(search_box, text="To'liq mos", variable=self.match_mode, value="exact").pack(side=tk.LEFT)

        results_box = ttk.LabelFrame(right, text="Natijalar (bosilganda video o‘sha joydan ketadi)")
        results_box.pack(side=tk.TOP, fill=tk.BOTH, expand=True, pady=(8, 8))

        self.results_list = tk.Listbox(results_box, height=12)
        self.results_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=8, pady=8)
        self.results_list.bind("<<ListboxSelect>>", self.on_select_result)

        res_scroll = ttk.Scrollbar(results_box, orient="vertical", command=self.results_list.yview)
        res_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.results_list.config(yscrollcommand=res_scroll.set)

        segments_box = ttk.LabelFrame(right, text="Barcha segmentlar (SRT)")
        segments_box.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        self.segments_text = tk.Text(segments_box, wrap="word", height=12)
        self.segments_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=8, pady=8)

        seg_scroll = ttk.Scrollbar(segments_box, orient="vertical", command=self.segments_text.yview)
        seg_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.segments_text.config(yscrollcommand=seg_scroll.set)

        # VLC panelni playerga ulash
        self.update_idletasks()
        handle = self.video_panel.winfo_id()
        # Windows
        self.player.set_hwnd(handle)

    # ---------- video ----------
    def load_video(self):
        path = filedialog.askopenfilename(
            title="Video tanlang",
            filetypes=[("Video files", "*.mp4 *.mkv *.avi *.mov *.webm"), ("All files", "*.*")]
        )
        if not path:
            return
        self.video_path = path
        self.audio_path = os.path.splitext(path)[0] + "_audio.wav"
        self.srt_path = os.path.splitext(path)[0] + ".srt"

        media = self.instance.media_new(self.video_path)
        self.player.set_media(media)

        self.progress.config(text=f"Holat: video yuklandi -> {os.path.basename(path)}")
        self._try_load_existing_srt()

    def play_video(self):
        if not self.video_path:
            messagebox.showwarning("Xabar", "Avval video yuklang.")
            return
        self.player.play()

    def pause_video(self):
        if self.player:
            self.player.pause()

    def stop_video(self):
        if self.player:
            self.player.stop()

    def seek_to(self, seconds: float):
        if not self.video_path:
            return
        # VLC millisekund
        ms = int(max(0, seconds) * 1000)
        self.player.play()
        time.sleep(0.05)
        self.player.set_time(ms)

    def seek_manual(self):
        try:
            sec = float(self.seek_entry.get().strip())
            self.seek_to(sec)
        except:
            messagebox.showerror("Xato", "Sekundni to‘g‘ri kiriting. Masalan: 12.5")

    # ---------- subtitle yaratish ----------
    def make_subtitles_thread(self):
        if not self.video_path:
            messagebox.showwarning("Xabar", "Avval video yuklang.")
            return

        t = threading.Thread(target=self.make_subtitles, daemon=True)
        t.start()

    def make_subtitles(self):
        try:
            self._set_status("Holat: audio qirqilmoqda (FFmpeg)...")
            run_ffmpeg_extract_audio(self.video_path, self.audio_path)

            self._set_status("Holat: transkripsiya (subtitle) qilinmoqda...")
            model_name = self.model_var.get().strip()
            # CPU uchun: compute_type="int8" tezroq, yengilroq
            model = WhisperModel(model_name, device="cpu", compute_type="int8")

            lang = self.lang_var.get().strip().lower()
            language = None if lang == "auto" else lang

            segments, info = model.transcribe(
                self.audio_path,
                language=language,
                beam_size=5,
                vad_filter=True
            )

            seg_list = list(segments)
            make_srt_from_segments(seg_list, self.srt_path)

            self._set_status("Holat: SRT tayyor. Yuklanmoqda...")
            self._load_srt_into_ui()

            self._set_status(f"Holat: tayyor ✅  ({os.path.basename(self.srt_path)})")
        except Exception as e:
            self._set_status("Holat: xatolik ❌")
            messagebox.showerror("Xatolik", str(e))

    def _set_status(self, text: str):
        self.progress.after(0, lambda: self.progress.config(text=text))

    def _try_load_existing_srt(self):
        if self.srt_path and os.path.exists(self.srt_path):
            self._load_srt_into_ui()

    def _load_srt_into_ui(self):
        try:
            self.srt_items = load_srt_items(self.srt_path)
        except Exception as e:
            messagebox.showerror("SRT o‘qish xatosi", str(e))
            return

        # Segmentlarni text oynaga chiqarish
        def fill():
            self.segments_text.delete("1.0", tk.END)
            for (st, en, txt) in self.srt_items:
                self.segments_text.insert(tk.END, f"[{sec_to_hhmmss(st)} - {sec_to_hhmmss(en)}] {txt}\n")
            self.results_list.delete(0, tk.END)

        self.segments_text.after(0, fill)

    # ---------- qidirish ----------
    def search_now(self):
        if not self.srt_items:
            messagebox.showwarning("Xabar", "Avval SRT bo‘lishi kerak. 'Audio + SRT yaratish' ni bosing.")
            return

        q = normalize_text(self.query_var.get())
        if not q:
            return

        mode = self.match_mode.get()
        matches = []

        for idx, (st, en, txt) in enumerate(self.srt_items):
            tnorm = normalize_text(txt)
            ok = False
            if mode == "exact":
                ok = (tnorm == q)
            else:
                ok = (q in tnorm)

            if ok:
                matches.append((st, en, txt))

        self.results_list.delete(0, tk.END)

        if not matches:
            self.results_list.insert(tk.END, "Hech narsa topilmadi.")
            return

        for (st, en, txt) in matches:
            self.results_list.insert(tk.END, f"{sec_to_hhmmss(st)}  |  {txt}")

        # natijalar ro‘yxatini keyingi bosishda ham ishlatish uchun saqlab qo‘yamiz
        self._last_matches = matches

    def on_select_result(self, event=None):
        if not hasattr(self, "_last_matches"):
            return
        sel = self.results_list.curselection()
        if not sel:
            return
        i = sel[0]
        if i >= len(self._last_matches):
            return

        st, en, txt = self._last_matches[i]
        self.seek_to(st)


if __name__ == "__main__":
    app = VideoSearchApp()
    app.mainloop()
