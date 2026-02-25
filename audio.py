import os
import re
import subprocess
import threading
import time
import srt
import yt_dlp
import requests
import json
import traceback
from datetime import timedelta
from kivy.properties import StringProperty, ListProperty, BooleanProperty, NumericProperty
from faster_whisper import WhisperModel
from google import genai
from dotenv import load_dotenv

# .env faylini yuklash
load_dotenv()


# ---------- yordamchi funksiyalar ----------
def sec_to_hhmmss(sec: float) -> str:
    sec = max(0.0, float(sec))
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s_ = int(sec % 60)
    return f"{h:02d}:{m:02d}:{s_:02d}"


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
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace')
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
                start=timedelta(seconds=start),
                end=timedelta(seconds=end),
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
    s = (s or "").lower().strip()
    s = re.sub(r"\s+", " ", s)
    return s


# ---------- Asosiy App ----------
# ---------- Asosiy App (Kivy Version) ----------

KV = """
#:import Window kivy.core.window.Window
#:import math math

<HoverButton@Button>:
    background_normal: ''
    background_color: (0,0,0,0)
    color: [1,1,1,1]
    font_name: 'Roboto'
    font_size: '14sp'
    bold: True
    canvas.before:
        Color:
            rgba: app.accent_color if self.state == 'normal' else [c*1.2 for c in app.accent_color[:3]] + [0.8]
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [15]
        Color:
            rgba: [1, 1, 1, 0.1]
        Line:
            rounded_rectangle: (self.x, self.y, self.width, self.height, 15)
            width: 1.1

<GlassPanel@BoxLayout>:
    canvas.before:
        Color:
            rgba: [1, 1, 1, 0.05] if app.theme_mode == 'dark' else [0, 0, 0, 0.05]
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [20]
        Color:
            rgba: [1, 1, 1, 0.1]
        Line:
            rounded_rectangle: (self.x, self.y, self.width, self.height, 20)
            width: 1.2

<ShazamCircle@ButtonBehavior+Label>:
    pulse_val: 1.0
    size_hint: None, None
    size: '180dp', '180dp'
    pos_hint: {'center_x': .5, 'center_y': .5}
    text: 'IVSP'
    font_size: '32sp'
    bold: True
    color: [1, 1, 1, 1]
    canvas.before:
        # Outer glow/pulse
        Color:
            rgba: app.accent_color[:3] + [0.3 * root.pulse_val]
        Ellipse:
            pos: self.x - (20 * root.pulse_val), self.y - (20 * root.pulse_val)
            size: self.width + (40 * root.pulse_val), self.height + (40 * root.pulse_val)
        # Main circle
        Color:
            rgba: app.accent_color
        Ellipse:
            pos: self.pos
            size: self.size
        # Glass shine
        Color:
            rgba: [1, 1, 1, 0.2]
        Ellipse:
            pos: self.x + 10, self.y + self.height - 60
            size: 40, 20

<StyledTextInput@TextInput>:
    background_color: [0,0,0,0]
    foreground_color: app.fg_color
    cursor_color: app.fg_color
    hint_text_color: [c*0.6 for c in app.fg_color[:3]] + [0.7]
    font_size: '15sp'
    padding: [15, 15]
    multiline: False
    canvas.before:
        Color:
            rgba: [1, 1, 1, 0.08] if app.theme_mode == 'dark' else [0, 0, 0, 0.05]
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [15]
    canvas.after:
        Color:
            rgba: app.accent_color if self.focus else (0, 0, 0, 0)
        Line:
            rounded_rectangle: (self.x + 1, self.y + 1, self.width - 2, self.height - 2, 15)
            width: 1.5

<ModelSpinner@Spinner>:
    background_normal: ''
    background_color: (0,0,0,0)
    color: app.fg_color
    font_size: '14sp'
    sync_height: True
    canvas.before:
        Color:
            rgba: [1, 1, 1, 0.1]
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [10]
        Color:
            rgba: app.accent_color[:3] + [0.5]
        Line:
            rounded_rectangle: (self.x, self.y, self.width, self.height, 10)
            width: 1

<SearchModeBtn@ToggleButton>:
    background_normal: ''
    background_down: ''
    background_color: (0,0,0,0)
    color: app.fg_color
    font_size: '12sp'
    group: 'search_mode'
    canvas.before:
        Color:
            rgba: app.accent_color if self.state == 'down' else [1,1,1,0.05]
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [8]

<MainLayout>:
    id: main_layout
    orientation: 'vertical'
    pulse_val: 1.0
    canvas.before:
        # Background Gradient - Deep Navy to Darker Navy
        Color:
            rgba: [0.08, 0.08, 0.12, 1] if app.theme_mode == 'dark' else [0.95, 0.96, 1, 1]
        Rectangle:
            pos: self.pos
            size: self.size
        Color:
            rgba: [0.15, 0.25, 0.45, 0.6] if app.theme_mode == 'dark' else [0.8, 0.85, 1, 0.4]
        Rectangle:
            pos: self.pos
            size: self.width, self.height * 0.5
            pos: self.x, self.y + self.height * 0.5

    # Top Bar
    BoxLayout:
        size_hint_y: None
        height: '80dp'
        padding: [20, 10]
        spacing: '15dp'
        
        Label:
            text: 'IVSP.'
            font_size: '28sp'
            bold: True
            size_hint_x: None
            width: '100dp'
            color: app.accent_color
            halign: 'left'

        Widget: # Spacer

        BoxLayout:
            size_hint_x: None
            width: '160dp'
            spacing: '10dp'
            HoverButton:
                text: '☀' if app.theme_mode == "dark" else '🌙'
                on_release: root.toggle_theme()
            HoverButton:
                text: ('AUTO' if root.current_lang == 'auto' else 'UZB')
                on_release: root.toggle_language_refined()

    # Central Shazam Visual
    AnchorLayout:
        size_hint_y: 0.4
        ShazamCircle:
            id: shazam_btn
            pulse_val: root.pulse_val
            on_release: root.make_subtitles_thread()

    # Input/Control Panel
    BoxLayout:
        orientation: 'vertical'
        padding: '20dp'
        spacing: '20dp'
        
        GlassPanel:
            orientation: 'vertical' if main_layout.width < 800 else 'horizontal'
            size_hint_y: None
            height: '220dp' if main_layout.width < 800 else '85dp'
            padding: '12dp'
            spacing: '15dp'
            
            BoxLayout:
                orientation: 'vertical'
                spacing: '5dp'
                StyledTextInput:
                    id: url_input
                    hint_text: 'Paste YouTube URL here...'
                    size_hint_y: None
                    height: '45dp'
            
            BoxLayout:
                size_hint_x: None if main_layout.width > 800 else 1
                width: '320dp' if main_layout.width > 800 else 0
                spacing: '10dp'
                
                BoxLayout:
                    orientation: 'vertical'
                    spacing: '2dp'
                    size_hint_x: 0.8
                    Label:
                        text: 'Whisper Model'
                        font_size: '11sp'
                        color: [c*0.8 for c in app.fg_color[:3]] + [1]
                    ModelSpinner:
                        id: model_spinner
                        text: 'small'
                        values: ['tiny', 'base', 'small', 'medium', 'large-v3']
                        on_text: root.whisper_model = self.text

                HoverButton:
                    text: 'YUKLASH'
                    on_release: root.download_and_process(url_input.text)
                HoverButton:
                    text: 'FAYL'
                    on_release: root.open_file_dialog()

        # Video & Results Section
        BoxLayout:
            orientation: 'vertical' if main_layout.width < 1000 else 'horizontal'
            spacing: '20dp'
            opacity: 0.8
            
    
            # Video + Local Controls
            BoxLayout:
                orientation: 'vertical'
                size_hint_x: 1 if main_layout.width < 1000 else 0.6
                spacing: '15dp'
                
                Video:
                    id: video_player
                    allow_stretch: True
                    canvas.before:
                        Color:
                            rgba: [0, 0, 0, 1]
                        RoundedRectangle:
                            pos: self.pos
                            size: self.size
                            radius: [20]
                
                Label:
                    id: subtitle_label
                    text: ''
                    size_hint_y: None
                    height: '40dp'
                    font_size: '18sp'
                    bold: True
                    color: app.accent_color
                    halign: 'center'
                    valign: 'middle'
                    text_size: self.width, None
                
                GridLayout:
                    rows: 1
                    size_hint_y: None
                    height: '50dp'
                    spacing: '10dp'
                    HoverButton:
                        text: 'PLAY' if video_player.state != 'play' else 'PAUSE'
                        on_release: video_player.state = 'pause' if video_player.state == 'play' else 'play'
                    HoverButton:
                        text: 'STOP'
                        on_release: video_player.state = 'stop'
                    HoverButton:
                        text: 'SUMMARY'
                        on_release: root.summarize_with_gemini()
                    
                    BoxLayout:
                        orientation: 'vertical'
                        size_hint_x: 1.5
                        Label:
                            id: time_label
                            text: '00:00 / 00:00'
                            font_size: '12sp'
                            color: app.fg_color
                        ProgressBar:
                            id: mini_progress
                            max: 100
                            value: (video_player.position / video_player.duration * 100) if video_player.duration > 0 else 0

            # Search & List
            GlassPanel:
                orientation: 'vertical'
                size_hint_x: 1 if main_layout.width < 1000 else 0.4
                padding: '15dp'
                spacing: '10dp'
                
                BoxLayout:
                    size_hint_y: None
                    height: '40dp'
                    spacing: '10dp'
                    Label:
                        text: 'Match Mode:'
                        font_size: '13sp'
                        size_hint_x: None
                        width: '90dp'
                    SearchModeBtn:
                        text: 'Contains'
                        state: 'down'
                        on_release: root.match_mode = 'contains'
                    SearchModeBtn:
                        text: 'Exact'
                        on_release: root.match_mode = 'exact'

                BoxLayout:
                    size_hint_y: None
                    height: '45dp'
                    spacing: '8dp'
                    StyledTextInput:
                        id: search_input
                        hint_text: 'Search keywords...'
                        on_text_validate: root.search_now()
                    HoverButton:
                        text: 'IZLASH'
                        size_hint_x: None
                        width: '80dp'
                        on_release: root.search_now()
                
                BoxLayout:
                    size_hint_y: None
                    height: '35dp'
                    spacing: '10dp'
                    HoverButton:
                        text: 'FULL LIST'
                        font_size: '11sp'
                        on_release: root._load_srt_items_into_ui()
                    HoverButton:
                        text: 'CLEAR SEARCH'
                        font_size: '11sp'
                        on_release: search_input.text = ''; root._load_srt_items_into_ui()

                ScrollView:
                    bar_width: '4dp'
                    scroll_type: ['bars', 'content']
                    GridLayout:
                        id: results_container
                        cols: 1
                        size_hint_y: None
                        height: self.minimum_height
                        spacing: '8dp'
                        padding: [0, 5]


"""

from kivy.app import App
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.gridlayout import GridLayout
from kivy.uix.label import Label
from kivy.uix.button import Button
from kivy.uix.textinput import TextInput
from kivy.uix.scrollview import ScrollView
from kivy.uix.video import Video
from kivy.uix.popup import Popup
from kivy.uix.progressbar import ProgressBar
from kivy.uix.spinner import Spinner
from kivy.clock import Clock
from kivy.graphics import Color, Rectangle, RoundedRectangle, Line, Ellipse
from kivy.core.window import Window
from kivy.lang import Builder
from kivy.properties import StringProperty, ListProperty, BooleanProperty

class MainLayout(BoxLayout):
    status_text = StringProperty("Holat: tayyor")
    stt_provider = StringProperty("whisper")
    auto_summarize = BooleanProperty(True)
    match_mode = StringProperty("contains")
    whisper_model = StringProperty("small")
    current_lang = StringProperty("auto")
    pulse_val = NumericProperty(1.0)
    
    def __init__(self, **kwargs):
        super(MainLayout, self).__init__(**kwargs)
        self.video_path = ""
        self.audio_path = ""
        self.srt_path = ""
        self.srt_items = []
        self._last_matches = []
        Clock.schedule_interval(self.update_video_time, 0.5)
        Clock.schedule_interval(self.animate_pulse, 0.05)

    def animate_pulse(self, dt):
        import math
        # Pulsating effect for the Shazam circle
        self.pulse_val = 1.0 + 0.08 * math.sin(time.time() * 3)

    def toggle_language_refined(self):
        if self.current_lang == "auto":
            self.current_lang = "uz"
        else:
            self.current_lang = "auto"
        self.set_status(f"Til o'zgardi: {self.current_lang.upper()}")

    def toggle_theme(self):
        app = App.get_running_app()
        if app.theme_mode == "dark":
            app.theme_mode = "light"
            app.bg_color = [0.95, 0.95, 0.98, 1]
            app.fg_color = [0.1, 0.1, 0.15, 1]
            app.accent_color = [0.2, 0.4, 0.8, 1]
            app.secondary_bg = [0.85, 0.87, 0.92, 0.9]
        else:
            app.theme_mode = "dark"
            app.bg_color = [0.12, 0.12, 0.18, 1]
            app.fg_color = [0.8, 0.84, 0.96, 1]
            app.accent_color = [0.53, 0.7, 0.98, 1]
            app.secondary_bg = [0.19, 0.2, 0.27, 0.9]

    def toggle_provider(self):
        self.stt_provider = "muxlisa" if self.stt_provider == "whisper" else "whisper"
        self.set_status(f"Holat: STT provayder - {self.stt_provider.capitalize()}")

    def set_status(self, text):
        def _set(dt):
            self.status_text = text
        Clock.schedule_once(_set)

    def open_file_dialog(self):
        from kivy.uix.filechooser import FileChooserIconView
        content = BoxLayout(orientation='vertical')
        fc = FileChooserIconView(path='.', filters=['*.mp4', '*.mkv', '*.avi', '*.mov', '*.webm'])
        content.add_widget(fc)
        btn_layout = BoxLayout(size_hint_y=None, height='50dp', spacing='10dp', padding='5dp')
        popup = Popup(title='Video tanlang', content=content, size_hint=(0.9, 0.9))
        def on_select(instance):
            if fc.selection:
                self.video_path = fc.selection[0]
                self.audio_path = os.path.splitext(self.video_path)[0] + "_audio.wav"
                self.srt_path = os.path.splitext(self.video_path)[0] + ".srt"
                self.ids.video_player.source = self.video_path
                self.ids.video_player.state = 'play'
                self.set_status(f"Holat: video yuklandi -> {os.path.basename(self.video_path)}")
                popup.dismiss()
                self._try_load_existing_srt()
        btn_select = Button(text='Tanlash')
        btn_select.bind(on_release=on_select)
        btn_close = Button(text='Yopish', on_release=popup.dismiss)
        btn_layout.add_widget(btn_select)
        btn_layout.add_widget(btn_close)
        content.add_widget(btn_layout)
        popup.open()

    def update_video_time(self, dt):
        vp = self.ids.video_player
        if vp.duration > 0:
            self.ids.time_label.text = f"{sec_to_hhmmss(vp.position)} / {sec_to_hhmmss(vp.duration)}"
            
            # Subtitlerlarni yangilash
            current_pos = vp.position
            sub_text = ""
            for st, en, txt in self.srt_items:
                if st <= current_pos <= en:
                    sub_text = txt
                    break
            self.ids.subtitle_label.text = sub_text

    def download_and_process(self, url):
        url = url.strip()
        if not url:
            self.set_status("Xato: URL kiriting")
            return
        if not os.path.exists("downloads"):
            os.makedirs("downloads")
        def task():
            try:
                self.set_status("Holat: Video yuklanmoqda...")
                ydl_opts = {
                    'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
                    'outtmpl': 'downloads/%(title)s.%(ext)s',
                }
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    video_path = ydl.prepare_filename(info)
                self.video_path = video_path
                self.audio_path = os.path.splitext(video_path)[0] + "_audio.wav"
                self.srt_path = os.path.splitext(video_path)[0] + ".srt"
                def _load_video(dt):
                    self.ids.video_player.source = self.video_path
                    self.ids.video_player.state = 'play'
                Clock.schedule_once(_load_video)
                self.make_subtitles_thread()
            except Exception as e:
                self.set_status(f"Xato: {str(e)}")
        threading.Thread(target=task, daemon=True).start()

    def make_subtitles_thread(self):
        threading.Thread(target=self.make_subtitles, daemon=True).start()

    def make_subtitles(self):
        self.set_status("Holat: Subtitre ajratish jarayoni ishga tushdi...")
        threading.Thread(target=self._actual_transcription, daemon=True).start()

    def _actual_transcription(self):
        try:
            self.set_status("Holat: Audio tayyorlanmoqda (FFmpeg)...")
            run_ffmpeg_extract_audio(self.video_path, self.audio_path)
            
            self.set_status("Holat: Matnga o'girish jarayoni (AI)...")
            provider = self.stt_provider
            subs = []
            
            if provider == "muxlisa":
                file_size = os.path.getsize(self.audio_path)
                if file_size > 5 * 1024 * 1024:
                    subs = self.transcribe_with_muxlisa_async()
                else:
                    subs = self.transcribe_with_muxlisa_api()
            else:
                self.set_status(f"Holat: Whisper ({self.whisper_model}) tahlil...")
                model_name = self.whisper_model
                model = WhisperModel(model_name, device="cpu", compute_type="int8")
                lang = self.current_lang
                language = None if lang == "auto" else lang
                segments, info = model.transcribe(self.audio_path, language=language, beam_size=5, vad_filter=True)
                for i, seg in enumerate(segments, 1):
                    subs.append(srt.Subtitle(
                        index=i, 
                        start=timedelta(seconds=seg.start), 
                        end=timedelta(seconds=seg.end), 
                        content=seg.text.strip()
                    ))

                # AI REFINEMENT
                subs = self.refine_subtitles_with_gemini(subs)

            if subs:
                with open(self.srt_path, "w", encoding="utf-8") as f:
                    f.write(srt.compose(subs))
                
                # AUTOMATICALLY LOAD INTO UI
                self._load_srt_into_ui()
                self.set_status(f"Holat: tayyor ✅ ({os.path.basename(self.srt_path)})")
                
        except Exception as e:
            import traceback
            print(traceback.format_exc())
            self.set_status(f"Xato: {str(e)}")

    def refine_subtitles_with_gemini(self, subs):
        try:
            self.set_status("Holat: AI tahlil...")
            # Limit to 50 segments to avoid 400 error/too long prompt
            chunk = subs[:50]
            raw_text = "\n".join([f"{i}|{s.content}" for i, s in enumerate(chunk)])
            
            client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
            prompt = (
                "Quyidagi qatorlarni grammatik tuzatib ber. "
                "Faqat 'index|matn' formatida qaytar. Segmentlarni o'zgartirma.\n\n" + raw_text
            )
            
            response = client.models.generate_content(
                model='gemini-2.0-flash', 
                contents=prompt
            )
            
            if response and response.text:
                lines = response.text.strip().split("\n")
                for line in lines:
                    if "|" in line:
                        parts = line.split("|", 1)
                        if len(parts) == 2:
                            try:
                                idx = int(parts[0].strip())
                                if 0 <= idx < len(subs):
                                    subs[idx].content = parts[1].strip()
                            except: pass
            self.set_status("Holat: AI tahlili yakunlandi")
        except Exception as e:
            print(f"DEBUG: Gemini refinement error: {e}")
            self.set_status(f"Faqat original matn qoldi (Gemini xatosi)")
        return subs

    def transcribe_with_muxlisa_api(self):
        url = "https://service.muxlisa.uz/api/v2/stt"
        api_key = os.getenv("MUXLISA_API_KEY")
        headers = {"x-api-key": api_key}
        with open(self.audio_path, "rb") as f:
            files = {"audio": (os.path.basename(self.audio_path), f, "audio/wav")}
            response = requests.post(url, headers=headers, files=files)
        if response.status_code == 200:
            data = response.json()
            segments = data.get("result", {}).get("segments", [])
            subs = []
            for i, segment in enumerate(segments, 1):
                subs.append(srt.Subtitle(
                    index=i,
                    start=timedelta(seconds=float(segment["start"])),
                    end=timedelta(seconds=float(segment["end"])),
                    content=segment["text"].strip()
                ))
            return subs
        else:
            raise Exception(f"Muxlisa API xatosi: {response.status_code}")

    def transcribe_with_muxlisa_async(self):
        upload_url = "https://service.muxlisa.uz/api/v1/async/stt"
        api_key = os.getenv("MUXLISA_API_KEY")
        headers = {"x-api-key": api_key}
        with open(self.audio_path, "rb") as f:
            files = {"audio": (os.path.basename(self.audio_path), f, "audio/wav")}
            response = requests.post(upload_url, headers=headers, files=files)
        if response.status_code != 200:
            raise Exception(f"Muxlisa Async Upload xatosi: {response.status_code}")
        data = response.json()
        task_id = data.get("task_id") or data.get("id")
        status_url = f"https://service.muxlisa.uz/api/v1/async/stt/status/{task_id}"
        for i in range(300):
            self.set_status(f"Holat: Muxlisa tahlil ({i+1})...")
            try:
                status_resp = requests.get(status_url, headers=headers)
                if status_resp.status_code == 200:
                    status_data = status_resp.json()
                    if status_data.get("status") == "completed":
                        result = status_data.get("result", {})
                        segments = result.get("segments", [])
                        if not segments:
                            segments = [{"start": 0, "end": 10, "text": result.get("text", "")}]
                        subs = []
                        for idx, segment in enumerate(segments, 1):
                            subs.append(srt.Subtitle(
                                index=idx,
                                start=timedelta(seconds=float(segment.get("start", 0))),
                                end=timedelta(seconds=float(segment.get("end", 5))),
                                content=segment.get("text", "").strip()
                            ))
                        return subs
                    elif status_data.get("status") == "failed":
                        raise Exception("Muxlisa Async tahlili muvaffaqiyatsiz.")
            except: pass
            time.sleep(5)
        raise Exception("Muxlisa Async kutish muddati tugadi.")

    def _try_load_existing_srt(self):
        if self.srt_path and os.path.exists(self.srt_path):
            self._load_srt_into_ui()

    def _load_srt_into_ui(self):
        if not self.srt_path or not os.path.exists(self.srt_path):
            return
        try:
            self.srt_items = load_srt_items(self.srt_path)
            def fill(dt):
                self.ids.results_container.clear_widgets()
                for (st, en, txt) in self.srt_items:
                    btn = Button(text=f"[{sec_to_hhmmss(st)}] {txt[:70]}...", size_hint_y=None, height=45,
                                background_normal='', background_color=App.get_running_app().secondary_bg[:3] + [0.8], color=App.get_running_app().fg_color)
                    self.ids.results_container.add_widget(btn)
            Clock.schedule_once(fill)
        except Exception as e:
            self.set_status(f"Xato: SRT o'qishda xatolik")

    def search_now(self):
        query_text = self.ids.search_input.text.strip()
        query = normalize_text(query_text)
        
        if not query or not self.srt_items:
            self._load_srt_items_into_ui() # Full list
            return
        
        matches = []
        for (st, en, txt) in self.srt_items:
            if query in normalize_text(txt):
                matches.append((st, en, txt))
                
        self.ids.results_container.clear_widgets()
        for (st, en, txt) in matches:
            btn = Button(
                text=f"[{sec_to_hhmmss(st)}] {txt}", 
                size_hint_y=None, 
                height=60,
                background_normal='', 
                background_color=App.get_running_app().accent_color[:3] + [0.4], 
                color=App.get_running_app().fg_color,
                halign='left', valign='middle', padding=(10, 5)
            )
            btn.bind(width=lambda inst, val: setattr(inst, 'text_size', (val * 0.9, None)))
            self.ids.results_container.add_widget(btn)
            
        if matches:
            self.set_status(f"Topildi: {len(matches)} ta natija")
            # Birinchi topilgan joyga sakrab o'tish
            Clock.schedule_once(lambda dt: self.seek_to(matches[0][0]), 0.5)
        else:
            self.set_status(f"'{query_text}' topilmadi")

    def seek_to(self, seconds):
        vp = self.ids.video_player
        dur = vp.duration
        print(f"DEBUG: Seek request: {seconds}s, Duration: {dur}s")
        
        if dur > 0:
            target = float(seconds) / float(dur)
            if target < 0: target = 0
            if target > 1: target = 1
            
            # Use a robust sequence: pause -> seek -> play
            vp.state = 'pause'
            def _do_seek(dt):
                try:
                    vp.seek(target)
                except: pass
                def _do_play(dt2):
                    vp.state = 'play'
                Clock.schedule_once(_do_play, 0.2)
            Clock.schedule_once(_do_seek, 0.1)
            self.set_status(f"O'tildi: {sec_to_hhmmss(seconds)}")
        else:
            # If duration not ready, wait and retry
            print("DEBUG: Duration not ready, retrying...")
            Clock.schedule_once(lambda dt: self.seek_to(seconds), 0.5)

    def _load_srt_items_into_ui(self):
        # Helper to load all items (similar to _load_srt_into_ui but without file check)
        self.ids.results_container.clear_widgets()
        for (st, en, txt) in self.srt_items:
            btn = Button(text=f"[{sec_to_hhmmss(st)}] {txt[:100]}", size_hint_y=None, height=50,
                        background_normal='', background_color=App.get_running_app().secondary_bg[:3] + [0.7], 
                        color=App.get_running_app().fg_color)
            self.ids.results_container.add_widget(btn)

    def summarize_with_gemini(self):
        if not self.srt_items:
            return
        full_text = " ".join([item[2] for item in self.srt_items])
        def task():
            try:
                self.set_status("Holat: Gemini xulosa...")
                client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
                response = client.models.generate_content(
                    model='gemini-2.0-flash',
                    contents=f"Quyidagi matnni o'zbek tilida qisqacha xulosa qilib ber:\n\n{full_text}"
                )
                content = BoxLayout(orientation='vertical', padding=10, spacing=10)
                app = App.get_running_app()
                txt = TextInput(text=response.text, readonly=True, background_color=app.bg_color, foreground_color=app.fg_color)
                content.add_widget(txt)
                btn = Button(text="Yopish", size_hint_y=None, height=40, background_color=app.accent_color)
                popup = Popup(title='Gemini Xulosasi', content=content, size_hint=(0.8, 0.8),
                              title_color=app.fg_color, separator_color=app.accent_color)
                btn.bind(on_release=popup.dismiss)
                content.add_widget(btn)
                def _show(dt): popup.open()
                Clock.schedule_once(_show)
                self.set_status("Holat: Gemini tayyor ✅")
            except Exception as e:
                self.set_status(f"Gemini xato: {str(e)}")
        threading.Thread(target=task, daemon=True).start()

class IVSPApp(App):
    theme_mode = StringProperty("dark")
    bg_color = ListProperty([0.12, 0.12, 0.18, 1])
    fg_color = ListProperty([0.8, 0.84, 0.96, 1])
    accent_color = ListProperty([0.53, 0.7, 0.98, 1])
    secondary_bg = ListProperty([0.19, 0.2, 0.27, 0.9])

    def build(self):
        Builder.load_string(KV)
        return MainLayout()

if __name__ == "__main__":
    app = IVSPApp()
    app.run()
