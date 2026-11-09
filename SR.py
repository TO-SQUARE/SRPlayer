import os
import random
import sys
import time
import json
import queue
import threading
import shutil
import array
import wave
import unicodedata
from difflib import get_close_matches
from typing import List, Optional
import urllib.request

import av
from PyQt6.QtCore import (
    QEasingCurve,
    QEvent,
    QObject,
    QMimeData,
    QPropertyAnimation,
    QRect,
    QSize,
    Qt,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QAction,
    QFont,
    QFontDatabase,
    QIcon,
    QKeySequence,
    QColor,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPixmap,
    QMouseEvent,
    QDropEvent,
    QWheelEvent,
)
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QComboBox,
    QCheckBox,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QMenu,
    QPushButton,
    QSlider,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QAbstractButton,
    QAbstractSlider,
    QAbstractScrollArea,
    QStyledItemDelegate,
    QStyle,
    QSystemTrayIcon,
)
from PyQt6.QtMultimedia import QAudio, QAudioFormat, QAudioSink, QMediaDevices


IS_FROZEN = getattr(sys, "frozen", False)
if IS_FROZEN:
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
    RESOURCE_DIR = getattr(sys, "_MEIPASS", BASE_DIR)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    RESOURCE_DIR = BASE_DIR
DATA_DIR = os.path.join(RESOURCE_DIR, "Data")
SUPPORTED_EXTENSIONS = (".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac")
SAVE_DIR = os.path.join(BASE_DIR, "SAVE")
RECORDINGS_DIR = os.path.join(BASE_DIR, "Recordings")
RECORDING_FORMATS = {
    "wav": ("WAV", ".wav"),
    "mp3": ("MP3", ".mp3"),
    "aac": ("AAC (M4A)", ".m4a"),
}
AUDIO_SAMPLE_RATE = 44100
AUDIO_CHANNELS = 2
AUDIO_BYTES_PER_FRAME = AUDIO_CHANNELS * 2
PLAYLIST_FILL_ROLE = Qt.ItemDataRole.UserRole + 1


def read_data_asset(relative_path: str) -> bytes:
    path = os.path.join(DATA_DIR, relative_path)
    with open(path, "rb") as handle:
        return handle.read()


def load_data_pixmap(relative_path: str) -> QPixmap:
    pixmap = QPixmap()
    if not pixmap.loadFromData(read_data_asset(relative_path)):
        raise FileNotFoundError(f"Could not load app resource: {relative_path}")
    return pixmap


def load_data_icon(relative_path: str) -> QIcon:
    return QIcon(os.path.join(DATA_DIR, relative_path))


class StreamDecoder:
    def __init__(
        self,
        url: str,
        is_stream: bool = True,
        start_position: float = 0.0,
    ) -> None:
        self.url = url
        self.is_stream = is_stream
        self.start_position = max(0.0, start_position)
        self.audio_queue: queue.Queue[bytes] = queue.Queue(maxsize=64)
        self.error_queue: queue.Queue[str] = queue.Queue(maxsize=10)
        self.metadata_queue: queue.Queue[str] = queue.Queue(maxsize=20)
        self.container = None
        self.metadata_response = None
        self.running = False
        self.thread: Optional[threading.Thread] = None
        self.metadata_thread: Optional[threading.Thread] = None
        self.finished = False
        self.settings_lock = threading.Lock()
        self.audio_processing_enabled = False
        self.equalizer_values: dict[str, int] = {}
        self.effects_values: dict[str, int] = {}
        self.filter_graph = None
        self.filter_source = None
        self.filter_sink = None
        self.filter_signature = None

    def start(self) -> None:
        self.running = True
        self.finished = False
        self.thread = threading.Thread(target=self._decode, daemon=True)
        self.thread.start()
        if self.is_stream:
            self.metadata_thread = threading.Thread(target=self._read_metadata, daemon=True)
            self.metadata_thread.start()

    def stop(self) -> None:
        self.running = False
        if self.metadata_response is not None:
            try:
                self.metadata_response.close()
            except Exception:
                pass
        for worker in (self.thread, self.metadata_thread):
            if worker is not None and worker is not threading.current_thread():
                worker.join(timeout=1.0)

    def set_audio_settings(
        self,
        equalizer_values: dict[str, int],
        effects_values: dict[str, int],
        enabled: bool,
    ) -> None:
        with self.settings_lock:
            self.equalizer_values = dict(equalizer_values)
            self.effects_values = dict(effects_values)
            self.audio_processing_enabled = enabled

    def _audio_filter_signature(self) -> tuple:
        with self.settings_lock:
            return (
                self.audio_processing_enabled,
                tuple(sorted(self.equalizer_values.items())),
                tuple(sorted(self.effects_values.items())),
            )

    def _build_audio_filter_graph(self, frame, signature: tuple) -> None:
        enabled, equalizer, effects = signature
        if not enabled or not any(value for _, value in equalizer + effects):
            self.filter_graph = None
            self.filter_source = None
            self.filter_sink = None
            self.filter_signature = signature
            return

        equalizer_values = dict(equalizer)
        effects_values = dict(effects)
        graph = av.filter.Graph()
        source = graph.add_abuffer(
            sample_rate=frame.sample_rate,
            format=frame.format.name,
            layout=frame.layout.name,
        )
        previous = source

        def append_filter(name: str, args: str) -> None:
            nonlocal previous
            current = graph.add(name, args=args)
            previous.link_to(current)
            previous = current

        for band, frequency in (
            ("60", 60),
            ("170", 170),
            ("350", 350),
            ("1000", 1000),
            ("3500", 3500),
            ("10000", 10000),
            ("16000", 16000),
        ):
            gain = equalizer_values.get(band, 0)
            if gain:
                append_filter(
                    "equalizer",
                    f"frequency={frequency}:width_type=o:width=1:g={gain}",
                )

        bass = effects_values.get("bass", 0)
        if bass:
            append_filter("bass", f"g={bass}:f=100")
        treble = effects_values.get("treble", 0)
        if treble:
            append_filter("treble", f"g={treble}:f=3000")
        clarity = effects_values.get("clarity", 0)
        if clarity:
            append_filter(
                "equalizer",
                f"frequency=4000:width_type=o:width=1:g={clarity}",
            )

        voice = effects_values.get("voice", 0)
        voice_factors = {
            1: 0.78,
            2: 1.08,
            3: 1.18,
            4: 1.38,
            5: 1.24,
            6: 0.84,
            7: 1.65,
        }
        pitch_factor = voice_factors.get(voice)
        if pitch_factor:
            target_rate = round(frame.sample_rate * pitch_factor)
            append_filter("asetrate", f"sample_rate={target_rate}")
            append_filter("aresample", str(frame.sample_rate))
            append_filter("atempo", f"tempo={1.0 / pitch_factor:.6f}")
            if voice in (5, 7):
                append_filter(
                    "vibrato",
                    f"f={5 if voice == 5 else 8}:d={0.12 if voice == 5 else 0.28}",
                )

        underwater = effects_values.get("underwater", 0)
        if underwater:
            intensity = underwater / 100.0
            highpass_frequency = round(25 + 225 * intensity)
            lowpass_frequency = round(18000 - 17400 * intensity)
            append_filter("highpass", f"f={highpass_frequency}")
            append_filter("lowpass", f"f={lowpass_frequency}")
            append_filter(
                "equalizer",
                "frequency=1200:width_type=o:width=1:"
                f"g={-12 * intensity:.2f}",
            )

        echo = effects_values.get("echo", 0)
        reverb = effects_values.get("reverb", 0)
        if echo or reverb:
            delays = []
            decays = []
            if echo:
                delays.append(str(120 + echo * 3))
                decays.append(f"{echo / 160.0:.3f}")
            if reverb:
                for delay in (35, 75, 120):
                    delays.append(str(delay))
                    decays.append(f"{reverb / 400.0:.3f}")
            append_filter(
                "aecho",
                "in_gain=0.8:out_gain=0.9:"
                f"delays={'|'.join(delays)}:decays={'|'.join(decays)}",
            )

        pan = effects_values.get("pan", 0)
        if pan:
            append_filter("stereotools", f"balance_in={pan / 100.0:.2f}")

        gain_db = equalizer_values.get("preamp", 0) + effects_values.get("gain", 0)
        if gain_db:
            append_filter("volume", f"volume={gain_db}dB")

        append_filter(
            "aformat",
            f"sample_fmts=s16:sample_rates={AUDIO_SAMPLE_RATE}:channel_layouts=stereo",
        )
        sink = graph.add("abuffersink")
        previous.link_to(sink)
        graph.configure()
        self.filter_graph = graph
        self.filter_source = source
        self.filter_sink = sink
        self.filter_signature = signature

    def _filter_audio_frame(self, frame) -> list:
        signature = self._audio_filter_signature()
        if signature != self.filter_signature:
            self._build_audio_filter_graph(frame, signature)
        if self.filter_source is None or self.filter_sink is None:
            return [frame]

        self.filter_source.push(frame)
        filtered = []
        while True:
            try:
                filtered.append(self.filter_sink.pull())
            except av.error.BlockingIOError:
                break
        return filtered

    def _flush_audio_filter(self) -> list:
        if self.filter_source is None or self.filter_sink is None:
            return []
        self.filter_source.push(None)
        filtered = []
        while True:
            try:
                filtered.append(self.filter_sink.pull())
            except (av.error.BlockingIOError, av.error.EOFError):
                break
        self.filter_graph = None
        self.filter_source = None
        self.filter_sink = None
        self.filter_signature = None
        return filtered

    def _queue_audio_chunk(self, chunk: bytes) -> bool:
        while self.running:
            try:
                self.audio_queue.put(chunk, timeout=0.5)
                return True
            except queue.Full:
                continue
        return False

    def _read_metadata(self) -> None:
        response = None
        try:
            if not self.running:
                return
            request = urllib.request.Request(
                self.url,
                headers={
                    "Icy-MetaData": "1",
                    "User-Agent": "AIMP-Frequency/1.0",
                },
            )
            response = urllib.request.urlopen(request, timeout=8)
            self.metadata_response = response
            headers = response.headers
            meta_int = int(headers.get("icy-metaint", "0") or 0)

            station_name = headers.get("icy-name", "").strip()
            if station_name:
                try:
                    self.metadata_queue.put_nowait(station_name)
                except queue.Full:
                    pass

            if meta_int <= 0:
                return

            while self.running:
                audio_block = response.read(meta_int)
                if len(audio_block) != meta_int:
                    break

                length_byte = response.read(1)
                if not length_byte:
                    break
                metadata_length = length_byte[0] * 16
                if metadata_length <= 0:
                    continue

                metadata = response.read(metadata_length)
                if len(metadata) != metadata_length:
                    break
                text = metadata.rstrip(b"\x00").decode("utf-8", errors="replace").strip()

                for field in text.split(";"):
                    field = field.strip()
                    if field.startswith("StreamTitle="):
                        title = field[len("StreamTitle="):].strip().strip("'\"")
                        if title:
                            try:
                                self.metadata_queue.put_nowait(title)
                            except queue.Full:
                                pass
                        break
        except Exception:
            return
        finally:
            if response is not None:
                response.close()
            self.metadata_response = None

    def _decode(self) -> None:
        while self.running:
            try:
                options = {}
                if self.is_stream:
                    options = {
                        "user_agent": "AIMP-Frequency/1.0",
                        "rw_timeout": "10000000",
                        "reconnect": "1",
                        "reconnect_at_eof": "1",
                        "reconnect_on_network_error": "1",
                        "reconnect_on_http_error": "5xx,4xx",
                        "reconnect_streamed": "1",
                        "reconnect_delay_max": "5",
                        "probesize": "32768",
                        "analyzeduration": "1000000",
                    }
                with av.open(
                    self.url,
                    mode="r",
                    options=options,
                    timeout=(10.0, 10.0) if self.is_stream else None,
                ) as container:
                    self.container = container
                    audio_stream = next(
                        (stream for stream in container.streams if stream.type == "audio"),
                        None,
                    )
                    if audio_stream is None:
                        raise ValueError("The source contains no audio stream")
                    if not self.is_stream and self.start_position > 0:
                        container.seek(int(self.start_position * av.time_base))

                    output_rate = AUDIO_SAMPLE_RATE
                    resampler = av.AudioResampler(
                        format="s16",
                        layout="stereo",
                        rate=output_rate,
                    )

                    def queue_processed_frame(frame) -> None:
                        for processed_frame in self._filter_audio_frame(frame):
                            chunk = bytes(processed_frame.planes[0])[
                                :processed_frame.samples * AUDIO_BYTES_PER_FRAME
                            ]
                            if chunk:
                                self._queue_audio_chunk(chunk)

                    for decoded_frame in container.decode(audio_stream):
                        if not self.running:
                            break
                        for output_frame in resampler.resample(decoded_frame):
                            queue_processed_frame(output_frame)
                    if self.running:
                        for output_frame in resampler.resample(None):
                            queue_processed_frame(output_frame)
                        for output_frame in self._flush_audio_filter():
                            chunk = bytes(output_frame.planes[0])[
                                :output_frame.samples * AUDIO_BYTES_PER_FRAME
                            ]
                            if chunk:
                                self._queue_audio_chunk(chunk)
                self.container = None
                if self.running and not self.is_stream:
                    self.finished = True
                    break
                if self.running and self.is_stream:
                    time.sleep(0.75)
            except Exception as exc:
                self.container = None
                if self.running:
                    try:
                        self.error_queue.put_nowait(str(exc))
                    except queue.Full:
                        pass
                    if not self.is_stream:
                        self.finished = True
                        break
                    time.sleep(0.75)


class AudioRecorder:
    def __init__(self, output_path: str, recording_format: str = "wav") -> None:
        self.output_path = output_path
        self.recording_format = recording_format
        self.writer = None
        self.container = None
        self.stream = None
        if recording_format == "wav":
            self.writer = wave.open(output_path, "wb")
            self.writer.setnchannels(AUDIO_CHANNELS)
            self.writer.setsampwidth(2)
            self.writer.setframerate(AUDIO_SAMPLE_RATE)
        else:
            codec = {"mp3": "libmp3lame", "aac": "aac"}[recording_format]
            self.container = av.open(output_path, "w")
            try:
                self.stream = self.container.add_stream(
                    codec,
                    rate=AUDIO_SAMPLE_RATE,
                )
                self.stream.layout = "stereo"
                self.stream.bit_rate = 192000
            except Exception:
                self.container.close()
                raise
        self.audio_queue: queue.Queue[tuple[bytes, float]] = queue.Queue(maxsize=1024)
        self.frames_written = 0
        self.frames_lock = threading.Lock()
        self.is_recording = True
        self.is_paused = False
        self.error: Optional[str] = None
        self.worker = threading.Thread(target=self._write_audio, daemon=True)
        self.worker.start()

    @property
    def duration_seconds(self) -> float:
        with self.frames_lock:
            return self.frames_written / AUDIO_SAMPLE_RATE

    def enqueue(self, chunk: bytes, volume: float) -> None:
        if not self.is_recording or self.is_paused or not chunk:
            return
        try:
            self.audio_queue.put_nowait((bytes(chunk), volume))
        except queue.Full:
            self.error = "The recording buffer is full."
            self.is_recording = False

    def _write_audio(self) -> None:
        try:
            while self.is_recording or not self.audio_queue.empty():
                try:
                    chunk, volume = self.audio_queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                if volume != 1.0:
                    samples = array.array("h")
                    samples.frombytes(chunk)
                    for index, sample in enumerate(samples):
                        samples[index] = max(
                            -32768,
                            min(32767, round(sample * volume)),
                        )
                    chunk = samples.tobytes()
                if self.writer is not None:
                    self.writer.writeframesraw(chunk)
                else:
                    samples = array.array("h")
                    samples.frombytes(chunk)
                    frame_count = len(samples) // AUDIO_CHANNELS
                    frame = av.AudioFrame(
                        format="fltp",
                        layout="stereo",
                        samples=frame_count,
                    )
                    frame.sample_rate = AUDIO_SAMPLE_RATE
                    for channel in range(AUDIO_CHANNELS):
                        plane = array.array(
                            "f",
                            (
                                samples[index] / 32768.0
                                for index in range(channel, len(samples), AUDIO_CHANNELS)
                            ),
                        )
                        frame.planes[channel].update(plane.tobytes())
                    for packet in self.stream.encode(frame):
                        self.container.mux(packet)
                with self.frames_lock:
                    self.frames_written += len(chunk) // AUDIO_BYTES_PER_FRAME
        except Exception as exc:
            self.error = str(exc)
            self.is_recording = False
        finally:
            if self.writer is not None:
                self.writer.close()
            if self.container is not None:
                try:
                    for packet in self.stream.encode(None):
                        self.container.mux(packet)
                except Exception as exc:
                    if self.error is None:
                        self.error = str(exc)
                finally:
                    self.container.close()

    def stop(self) -> None:
        self.is_recording = False
        self.worker.join()


class RadioDialog(QDialog):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add Internet Radio")
        self.resize(500, 150)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.url = QLineEdit()
        self.url.setPlaceholderText("https://example.com/radio.mp3")
        self.name = QLineEdit()
        self.name.setPlaceholderText("Station name")
        form.addRow("Stream URL:", self.url)
        form.addRow("Station name:", self.name)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self) -> tuple[str, str]:
        return self.url.text().strip(), self.name.text().strip()


class StreamCustomizeDialog(QDialog):
    def __init__(
        self,
        url: str,
        label: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Customize Radio Stream")
        self.resize(520, 180)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit()
        self.url = QLineEdit()
        self.name.setText(label.removeprefix("[Radio] ").strip())
        self.url.setText(url)
        form.addRow("Display name:", self.name)
        form.addRow("Stream URL:", self.url)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self) -> tuple[str, str]:
        return self.name.text().strip(), self.url.text().strip()


class PlaylistDialog(QDialog):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Create Playlist")
        self.resize(420, 250)
        self.selected_image = ""

        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit()
        self.name.setPlaceholderText("Playlist name")
        form.addRow("Name:", self.name)

        self.preview = QLabel("No logo selected")
        self.preview.setFixedSize(120, 120)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setObjectName("playlistLogoPreview")

        image_row = QHBoxLayout()
        browse_button = QPushButton("Choose logo")
        clear_button = QPushButton("Clear")
        browse_button.clicked.connect(self.choose_image)
        clear_button.clicked.connect(self.clear_image)
        image_row.addWidget(browse_button)
        image_row.addWidget(clear_button)
        image_row.addStretch()

        layout.addLayout(form)
        layout.addWidget(self.preview)
        layout.addLayout(image_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok_button.setEnabled(False)
        self.name.textChanged.connect(
            lambda text: ok_button.setEnabled(bool(text.strip()))
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def choose_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Choose Playlist Logo",
            "",
            "Images (*.png *.jpg *.jpeg *.bmp *.webp *.gif *.svg)",
            options=QFileDialog.Option.DontUseNativeDialog,
        )
        if not path:
            return
        pixmap = QPixmap(path)
        if pixmap.isNull():
            QMessageBox.warning(self, "Invalid image", "The selected logo could not be loaded.")
            return
        self.selected_image = path
        self.preview.setPixmap(
            pixmap.scaled(
                self.preview.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def clear_image(self) -> None:
        self.selected_image = ""
        self.preview.clear()
        self.preview.setText("No logo selected")

    def values(self) -> tuple[str, str]:
        return self.name.text().strip(), self.selected_image


class AudioEqualizerDialog(QWidget):
    BAND_ORDER = ["60", "170", "350", "1000", "3500", "10000", "16000"]
    PRESETS = {
        "Flat": {"preamp": 0, "60": 0, "170": 0, "350": 0, "1000": 0, "3500": 0, "10000": 0, "16000": 0},
        "Rock": {"preamp": 2, "60": 3, "170": 2, "350": 1, "1000": 0, "3500": 2, "10000": 3, "16000": 4},
        "Pop": {"preamp": 1, "60": 1, "170": 2, "350": 3, "1000": 3, "3500": 2, "10000": 1, "16000": 1},
        "Techno": {"preamp": 3, "60": 4, "170": 3, "350": 2, "1000": 2, "3500": 4, "10000": 4, "16000": 3},
        "Classical": {"preamp": 0, "60": 0, "170": 1, "350": 2, "1000": 3, "3500": 3, "10000": 2, "16000": 2},
        "Vocal": {"preamp": 1, "60": -1, "170": 0, "350": 2, "1000": 4, "3500": 3, "10000": 1, "16000": 0},
        "Jazz": {"preamp": 1, "60": 3, "170": 2, "350": 1, "1000": 1, "3500": 2, "10000": 2, "16000": 3},
    }

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.sliders: dict[str, QSlider] = {}
        self.value_labels: dict[str, QLabel] = {}
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.preset_combo = QComboBox()
        self.preset_combo.addItems(["Flat", "Rock", "Pop", "Techno", "Classical", "Vocal", "Jazz"])
        self.preset_combo.currentTextChanged.connect(self.apply_preset)
        top.addWidget(QLabel("Preset:"))
        top.addWidget(self.preset_combo)
        top.addStretch()
        layout.addLayout(top)

        band_box = QWidget()
        band_layout = QHBoxLayout(band_box)
        band_layout.setContentsMargins(10, 10, 10, 10)

        for band in self.BAND_ORDER:
            column = QVBoxLayout()
            label = QLabel(f"{band}Hz")
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            slider = QSlider(Qt.Orientation.Vertical)
            slider.setRange(-12, 12)
            slider.setValue(0)
            slider.setFixedHeight(180)
            slider.valueChanged.connect(lambda value, band_name=band: self._update_band_label(band_name, value))
            value_label = QLabel("0 dB")
            value_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            value_label.setObjectName("equalizerValue")
            self.sliders[band] = slider
            self.value_labels[band] = value_label
            column.addWidget(label)
            column.addWidget(slider)
            column.addWidget(value_label)
            band_layout.addLayout(column)

        self.preamp_slider = QSlider(Qt.Orientation.Horizontal)
        self.preamp_slider.setRange(-12, 12)
        self.preamp_slider.setValue(0)
        self.preamp_label = QLabel("Preamp: 0 dB")
        self.preamp_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preamp_slider.valueChanged.connect(lambda value: self.preamp_label.setText(f"Preamp: {value} dB"))

        layout.addWidget(band_box)
        layout.addWidget(self.preamp_label)
        layout.addWidget(self.preamp_slider)

    def _update_band_label(self, band: str, value: int) -> None:
        if band in self.value_labels:
            self.value_labels[band].setText(f"{value} dB")

    def set_values(self, values: dict[str, int]) -> None:
        self.preamp_slider.setValue(int(values.get("preamp", 0)))
        for band in self.BAND_ORDER:
            self.sliders[band].setValue(int(values.get(band, 0)))

    def values(self) -> dict[str, int]:
        payload = {"preamp": self.preamp_slider.value()}
        for band in self.BAND_ORDER:
            payload[band] = self.sliders[band].value()
        return payload

    def apply_preset(self, name: str) -> None:
        preset = self.PRESETS.get(name, self.PRESETS["Flat"])
        self.set_values(preset)


class AudioEffectsDialog(QWidget):
    PRESETS = {
        "Flat": {"bass": 0, "treble": 0, "clarity": 0, "echo": 0, "reverb": 0, "pan": 0, "gain": 0, "underwater": 0, "voice": 0},
        "Vocal": {"bass": -2, "treble": 3, "clarity": 6, "echo": 10, "reverb": 15, "pan": 0, "gain": 2, "underwater": 0, "voice": 0},
        "Bass Boost": {"bass": 10, "treble": 1, "clarity": 2, "echo": 8, "reverb": 12, "pan": 0, "gain": 3, "underwater": 0, "voice": 0},
        "Warm": {"bass": 6, "treble": -2, "clarity": 1, "echo": 15, "reverb": 20, "pan": 10, "gain": 2, "underwater": 0, "voice": 0},
        "Club": {"bass": 5, "treble": 5, "clarity": 4, "echo": 20, "reverb": 18, "pan": 16, "gain": 4, "underwater": 0, "voice": 0},
    }

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.sliders: dict[str, QSlider] = {}
        self.value_labels: dict[str, QLabel] = {}
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.preset_combo = QComboBox()
        self.preset_combo.addItems(["Flat", "Vocal", "Bass Boost", "Warm", "Club"])
        self.preset_combo.currentTextChanged.connect(self.apply_preset)
        top.addWidget(QLabel("Preset:"))
        top.addWidget(self.preset_combo)
        top.addStretch()
        layout.addLayout(top)

        rows = [
            ("bass", "Bass", -12, 12),
            ("treble", "Treble", -12, 12),
            ("clarity", "Clarity", -10, 10),
            ("echo", "Echo", 0, 100),
            ("reverb", "Reverb", 0, 100),
            ("underwater", "Underwater", 0, 100),
            ("pan", "Stereo Pan", -100, 100),
            ("gain", "Gain", -6, 6),
        ]
        for key, label_text, minimum, maximum in rows:
            row = QHBoxLayout()
            row_label = QLabel(f"{label_text}:")
            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(minimum, maximum)
            slider.setValue(0)
            slider.valueChanged.connect(lambda value, name=key: self._update_value_label(name, value))
            value_label = QLabel("0")
            self.sliders[key] = slider
            self.value_labels[key] = value_label
            row.addWidget(row_label)
            row.addWidget(slider, 1)
            row.addWidget(value_label)
            layout.addLayout(row)

        voice_row = QHBoxLayout()
        voice_row.addWidget(QLabel("Voice type:"))
        self.voice_combo = QComboBox()
        self.voice_combo.addItems(
            [
                "Normal",
                "Deep male",
                "Young male",
                "Female",
                "Child",
                "Grandmother",
                "Grandfather",
                "Squirrel",
            ]
        )
        voice_row.addWidget(self.voice_combo, 1)
        layout.addLayout(voice_row)

    def _update_value_label(self, key: str, value: int) -> None:
        if key in self.value_labels:
            self.value_labels[key].setText(str(value))

    def set_values(self, values: dict[str, int]) -> None:
        for key, slider in self.sliders.items():
            slider.setValue(int(values.get(key, 0)))
        self.voice_combo.setCurrentIndex(
            max(0, min(self.voice_combo.count() - 1, int(values.get("voice", 0))))
        )

    def values(self) -> dict[str, int]:
        payload = {}
        for key, slider in self.sliders.items():
            payload[key] = slider.value()
        payload["voice"] = self.voice_combo.currentIndex()
        return payload

    def apply_preset(self, name: str) -> None:
        preset = self.PRESETS.get(name, self.PRESETS["Flat"])
        self.set_values(preset)


class AudioSettingsDialog(QDialog):
    settingsChanged = pyqtSignal(dict, dict, bool)
    SOUND_PRESETS = {
        "Studio": (True, {"preamp": -1, "1000": 1, "3500": 2}, {}),
        "Podcast": (True, {"preamp": -2, "170": -2, "1000": 2, "3500": 4, "10000": 2}, {"clarity": 5}),
        "Deep male": (True, {}, {"voice": 1}),
        "Young male": (True, {}, {"voice": 2}),
        "Female": (True, {}, {"voice": 3}),
        "Child": (True, {}, {"voice": 4}),
        "Grandmother": (True, {}, {"voice": 5}),
        "Grandfather": (True, {}, {"voice": 6}),
        "Squirrel": (True, {}, {"voice": 7}),
        "Underwater": (True, {}, {"underwater": 85}),
        "Echo chamber": (True, {}, {"echo": 65, "reverb": 35}),
    }

    def __init__(
        self,
        equalizer_values: dict[str, int],
        effects_values: dict[str, int],
        processing_enabled: bool,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Audio Settings")
        self.resize(640, 590)
        self.setMinimumSize(560, 500)
        self._applying_preset = False

        layout = QVBoxLayout(self)
        self.equalizer = AudioEqualizerDialog(self)
        self.effects = AudioEffectsDialog(self)
        self.equalizer.set_values(equalizer_values)
        self.effects.set_values(effects_values)

        preset_row = QHBoxLayout()
        preset_row.addWidget(QLabel("Sound profile:"))
        self.preset_combo = QComboBox()
        self.preset_combo.addItems(
            ["Custom", "Normal", *self.SOUND_PRESETS.keys()]
        )
        is_normal = not processing_enabled
        self.preset_combo.setCurrentText("Normal" if is_normal else "Custom")
        self.preset_combo.currentTextChanged.connect(self.apply_sound_preset)
        preset_row.addWidget(self.preset_combo, 1)
        layout.addLayout(preset_row)

        self.processing_enabled_checkbox = QCheckBox("Enable audio adjustments")
        self.processing_enabled_checkbox.setObjectName("audioProcessingToggle")
        self.processing_enabled_checkbox.setChecked(processing_enabled)
        self.processing_enabled_checkbox.toggled.connect(self.emit_settings_changed)
        layout.addWidget(self.processing_enabled_checkbox)

        tabs = QTabWidget()
        tabs.addTab(self.equalizer, "Equalizer")
        tabs.addTab(self.effects, "Effects")
        layout.addWidget(tabs)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        for slider in (
            *self.equalizer.sliders.values(),
            self.equalizer.preamp_slider,
            *self.effects.sliders.values(),
        ):
            slider.valueChanged.connect(self.emit_settings_changed)
        self.effects.voice_combo.currentIndexChanged.connect(self.emit_settings_changed)

    def apply_sound_preset(self, name: str) -> None:
        if name == "Custom":
            return
        if name == "Normal":
            enabled, equalizer, effects = False, {}, {}
        else:
            profile = self.SOUND_PRESETS.get(name)
            if profile is None:
                return
            enabled, equalizer, effects = profile

        equalizer_values = {
            key: 0
            for key in ("preamp", *AudioEqualizerDialog.BAND_ORDER)
        }
        equalizer_values.update(equalizer)
        effects_values = {key: 0 for key in self.effects.sliders}
        effects_values["voice"] = 0
        effects_values.update(effects)

        self._applying_preset = True
        self.processing_enabled_checkbox.blockSignals(True)
        self.processing_enabled_checkbox.setChecked(enabled)
        self.processing_enabled_checkbox.blockSignals(False)
        self.equalizer.set_values(equalizer_values)
        self.effects.set_values(effects_values)
        self._applying_preset = False
        self.settingsChanged.emit(
            self.equalizer.values(),
            self.effects.values(),
            enabled,
        )

    def emit_settings_changed(self, *_args) -> None:
        if self._applying_preset:
            return
        if self.preset_combo.currentText() != "Custom":
            self.preset_combo.blockSignals(True)
            self.preset_combo.setCurrentText("Custom")
            self.preset_combo.blockSignals(False)
        self.settingsChanged.emit(
            self.equalizer.values(),
            self.effects.values(),
            self.processing_enabled_checkbox.isChecked(),
        )


class TimelineSlider(QSlider):
    wheelChanged = pyqtSignal(int)
    wheel_step_ms = 0

    def setTimelineWheelStep(self, step: int) -> None:
        self.wheel_step_ms = max(0, int(step))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.orientation() == Qt.Orientation.Horizontal:
            if self.width() <= 0:
                event.accept()
                return
            self.set_value_at_position(event.position().x())
            self.sliderPressed.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if event.buttons() == Qt.MouseButton.LeftButton and self.orientation() == Qt.Orientation.Horizontal:
            self.set_value_at_position(event.position().x())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.orientation() == Qt.Orientation.Horizontal:
            self.sliderReleased.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def set_value_at_position(self, x: float) -> None:
        ratio = max(0.0, min(1.0, x / self.width()))
        value = self.minimum() + ratio * (self.maximum() - self.minimum())
        self.setValue(round(value))

    def wheelEvent(self, event: QWheelEvent) -> None:
        delta = event.angleDelta().y()
        if delta == 0:
            return

        if self.wheel_step_ms > 0:
            step = self.wheel_step_ms
        elif self.maximum() >= 30000:
            step = 500
        else:
            step = max(1, self.maximum() // 100)

        notches = delta / 120.0
        new_value = self.value() + int(step * notches)
        new_value = max(self.minimum(), min(self.maximum(), new_value))
        self.setValue(new_value)
        self.wheelChanged.emit(new_value)
        event.accept()


class PlaylistWidget(QListWidget):
    enterPressed = pyqtSignal(QListWidgetItem)
    arrowMoved = pyqtSignal()
    selectionModeEscape = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self.selection_mode = False

    def keyPressEvent(self, event) -> None:
        key = event.key()

        if self.selection_mode and key == Qt.Key.Key_Escape:
            self.selectionModeEscape.emit()
            event.accept()
            return

        if (
            self.selection_mode
            and key == Qt.Key.Key_A
            and event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            for row in range(self.count()):
                item = self.item(row)
                if item is not None:
                    item.setSelected(not item.isHidden())
            event.accept()
            return

        if key == Qt.Key.Key_Return or key == Qt.Key.Key_Enter:
            if self.selection_mode:
                event.accept()
                return
            item = self.currentItem()
            if item is not None:
                self.enterPressed.emit(item)
                event.accept()
                return

        if self.count() > 0 and self.currentRow() < 0:
            if key == Qt.Key.Key_Up:
                self.setCurrentRow(self.count() - 1)
                self.arrowMoved.emit()
                event.accept()
                return
            if key == Qt.Key.Key_Down:
                self.setCurrentRow(0)
                self.arrowMoved.emit()
                event.accept()
                return

        super().keyPressEvent(event)
        if key == Qt.Key.Key_Up or key == Qt.Key.Key_Down:
            self.arrowMoved.emit()


class TopMiniPlayer(QWidget):
    def __init__(self, player: "AudioPlayer") -> None:
        super().__init__(
            None,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        self.player = player
        self.expanded = False
        self.mini_mode = False
        self.panel_height = 110
        self.panel_geometry = QRect(0, 0, 760, self.panel_height)
        self.resizing_enabled = False
        self.moving_enabled = False
        self._interaction = None
        self._interaction_start = None
        self._interaction_geometry = None
        self.overlay_screen = None
        self.setObjectName("miniPlayerOverlay")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle("SR Player Mini Player")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.panel = QWidget()
        self.panel.setObjectName("miniPlayerPanel")
        self.panel.setMinimumHeight(102)
        panel_layout = QVBoxLayout(self.panel)
        panel_layout.setContentsMargins(16, 10, 16, 12)
        panel_layout.setSpacing(8)

        info_row = QHBoxLayout()
        info_row.setSpacing(12)
        self.logo_label = QLabel()
        self.logo_label.setObjectName("miniPlayerLogo")
        self.logo_label.setFixedSize(40, 40)
        self.logo_label.setScaledContents(True)
        logo = load_data_pixmap("SR.PNG")
        if not logo.isNull():
            self.logo_label.setPixmap(
                logo.scaled(
                    self.logo_label.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        info_row.addWidget(self.logo_label)

        title_column = QVBoxLayout()
        title_column.setSpacing(2)
        self.title_label = QLabel("No track loaded")
        self.title_label.setObjectName("miniPlayerTitle")
        self.title_label.setMinimumWidth(120)
        self.details_label = QLabel("Add audio files to start listening")
        self.details_label.setObjectName("miniPlayerDetails")
        self.details_label.setMinimumWidth(120)
        title_column.addWidget(self.title_label)
        title_column.addWidget(self.details_label)
        info_row.addLayout(title_column, 1)

        self.live_badge = QLabel("READY")
        self.live_badge.setObjectName("miniPlayerBadge")
        self.live_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        info_row.addWidget(self.live_badge)
        self.open_button = QToolButton()
        self.open_button.setObjectName("miniPlayerOpenButton")
        self.open_button.setText("Open player")
        self.open_button.setToolTip("Restore the main player")
        self.open_button.clicked.connect(player.restore_from_mini_player)
        info_row.addWidget(self.open_button)
        self.internet_button = self._icon_button(
            "internet.svg",
            f"Internet access: {'on' if player.internet_enabled else 'off'}",
        )
        self.internet_button.setObjectName("internetAccessButton")
        self.internet_button.setCheckable(True)
        self.internet_button.setChecked(player.internet_enabled)
        self.internet_button.setProperty("active", player.internet_enabled)
        self.internet_button.clicked.connect(player.toggle_internet_access)
        info_row.addWidget(self.internet_button)
        self.collapse_button = QToolButton()
        self.collapse_button.setObjectName("miniPlayerCollapseButton")
        self.collapse_button.setText("▲")
        self.collapse_button.setToolTip("Hide mini player")
        self.collapse_button.clicked.connect(self.collapse_panel)
        info_row.addWidget(self.collapse_button)
        panel_layout.addLayout(info_row)

        controls_row = QHBoxLayout()
        controls_row.setSpacing(7)
        self.previous_button = self._icon_button("previous.svg", "Previous track")
        self.play_button = self._icon_button("play.svg", "Play")
        self.stop_button = self._icon_button("stop.svg", "Stop")
        self.next_button = self._icon_button("next.svg", "Next track")
        self.previous_button.clicked.connect(player.previous_track)
        self.play_button.clicked.connect(player.toggle_play_pause)
        self.stop_button.clicked.connect(player.stop)
        self.next_button.clicked.connect(player.next_track)
        controls_row.addWidget(self.previous_button)
        controls_row.addWidget(self.play_button)
        controls_row.addWidget(self.stop_button)
        controls_row.addWidget(self.next_button)

        self.elapsed_label = QLabel("00:00")
        self.elapsed_label.setObjectName("miniPlayerTime")
        self.position_slider = TimelineSlider(Qt.Orientation.Horizontal)
        self.position_slider.setObjectName("miniPlayerProgress")
        self.position_slider.setRange(0, 1000)
        self.position_slider.sliderPressed.connect(player.position_pressed)
        self.position_slider.valueChanged.connect(self._timeline_changed)
        self.position_slider.sliderReleased.connect(player.seek_position)
        self.position_slider.wheelChanged.connect(player.timeline_wheel_changed)
        self.duration_label = QLabel("00:00")
        self.duration_label.setObjectName("miniPlayerTime")
        controls_row.addWidget(self.elapsed_label)
        controls_row.addWidget(self.position_slider, 1)
        controls_row.addWidget(self.duration_label)

        self.volume_label = QLabel("75%")
        self.volume_label.setObjectName("miniPlayerVolume")
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setObjectName("miniPlayerVolumeSlider")
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(75)
        self.volume_slider.setFixedWidth(92)
        self.volume_slider.valueChanged.connect(player.set_volume)
        controls_row.addWidget(self.volume_label)
        controls_row.addWidget(self.volume_slider)
        panel_layout.addLayout(controls_row)
        root.addWidget(self.panel)

        self.animation = QPropertyAnimation(self, b"geometry", self)
        self.animation.setDuration(240)
        self.animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.animation.finished.connect(self._animation_finished)
        self.panel.hide()
        self.setMinimumWidth(560)
        self.resize(760, 0)
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    def _icon_button(self, icon_name: str, tooltip: str) -> QToolButton:
        button = QToolButton()
        button.setObjectName("miniPlayerTransport")
        button.setIcon(self.player.svg_icon(icon_name))
        button.setIconSize(QSize(18, 18))
        button.setFixedSize(34, 32)
        button.setToolTip(tooltip)
        return button

    def _timeline_changed(self, value: int) -> None:
        self.player.position_slider.setValue(value)
        self.player.timeline_value_changed(value)
        source = self.player.active_playback_source
        if source is not None and not source.startswith(("http://", "https://")):
            duration = self.player.get_duration()
            if duration > 0:
                position = duration * value / max(1, self.player.position_slider.maximum())
                self.elapsed_label.setText(self.player.format_time(position))
                self.duration_label.setText(self.player.format_time(duration))
                return
        self.elapsed_label.setText(self.player.elapsed_label.text())
        self.duration_label.setText(self.player.duration_label.text())

    def collapse_panel(self) -> None:
        if self.expanded:
            self.toggle_panel()

    def apply_settings(self, settings: dict) -> None:
        self.resizing_enabled = bool(settings.get("resizable", False))
        self.moving_enabled = bool(settings.get("movable", False))
        geometry_loaded = False
        geometry = settings.get("geometry")
        if isinstance(geometry, list) and len(geometry) == 4:
            try:
                x, y, width, height = (int(value) for value in geometry)
                candidate = QRect(x, y, width, height)
                visible = any(
                    candidate.intersected(screen.availableGeometry()).width() >= 100
                    and candidate.intersected(screen.availableGeometry()).height() >= 60
                    for screen in QApplication.screens()
                )
                if width >= 560 and height >= 105 and visible:
                    self.panel_geometry = candidate
                    self.panel_height = height
                    geometry_loaded = True
            except (TypeError, ValueError, OverflowError):
                pass
        if not geometry_loaded:
            screen = QApplication.primaryScreen()
            if screen is not None:
                available = screen.availableGeometry()
                self.panel_geometry = QRect(
                    available.center().x() - self.panel_geometry.width() // 2,
                    screen.geometry().top(),
                    self.panel_geometry.width(),
                    self.panel_height,
                )
        self.resize(self.panel_geometry.width(), 0)
        self.move(self.panel_geometry.topLeft())

    def settings_payload(self) -> dict:
        geometry = self.panel_geometry
        return {
            "resizable": self.resizing_enabled,
            "movable": self.moving_enabled,
            "geometry": [geometry.x(), geometry.y(), geometry.width(), geometry.height()],
        }

    def show_settings_menu(self, global_position) -> None:
        menu = QMenu(self)
        resize_action = QAction("Enable resizing", menu)
        resize_action.setCheckable(True)
        resize_action.setChecked(self.resizing_enabled)
        resize_action.toggled.connect(self.set_resizing_enabled)
        menu.addAction(resize_action)
        move_action = QAction("Enable moving", menu)
        move_action.setCheckable(True)
        move_action.setChecked(self.moving_enabled)
        move_action.toggled.connect(self.set_moving_enabled)
        menu.addAction(move_action)
        menu.addSeparator()
        default_action = QAction("Restore default shape", menu)
        default_action.triggered.connect(self.restore_default_shape)
        menu.addAction(default_action)
        menu.exec(global_position)

    def restore_default_shape(self) -> None:
        width = 760
        self.panel_height = 110
        screen = QApplication.primaryScreen()
        if screen is None:
            x, y = 0, 0
        else:
            available = screen.availableGeometry()
            x = available.center().x() - width // 2
            y = screen.geometry().top()
            self.overlay_screen = screen
        self.panel_geometry = QRect(x, y, width, self.panel_height)
        self.setGeometry(
            x,
            y,
            width,
            self.panel_height if self.expanded else 0,
        )
        self.player.save_window_settings()

    def restore_default_settings(self) -> None:
        self.resizing_enabled = False
        self.moving_enabled = False
        self.restore_default_shape()

    def set_resizing_enabled(self, enabled: bool) -> None:
        self.resizing_enabled = enabled
        self.player.save_window_settings()

    def set_moving_enabled(self, enabled: bool) -> None:
        self.moving_enabled = enabled
        self.player.save_window_settings()

    def _resize_edges_at(self, global_position) -> Qt.Edge:
        if not self.resizing_enabled or not self.expanded:
            return Qt.Edge(0)
        local_x = global_position.x() - self.geometry().x()
        local_y = global_position.y() - self.geometry().y()
        border = 8
        edges = Qt.Edge(0)
        if local_x <= border:
            edges |= Qt.Edge.LeftEdge
        elif local_x >= self.width() - border:
            edges |= Qt.Edge.RightEdge
        if local_y >= self.height() - border:
            edges |= Qt.Edge.BottomEdge
        return edges

    def _update_resize_cursor(self, watched: QWidget, edges: Qt.Edge) -> None:
        if edges & Qt.Edge.BottomEdge:
            if edges & Qt.Edge.LeftEdge:
                cursor = Qt.CursorShape.SizeBDiagCursor
            elif edges & Qt.Edge.RightEdge:
                cursor = Qt.CursorShape.SizeFDiagCursor
            else:
                cursor = Qt.CursorShape.SizeVerCursor
        elif edges & (Qt.Edge.LeftEdge | Qt.Edge.RightEdge):
            cursor = Qt.CursorShape.SizeHorCursor
        else:
            watched.unsetCursor()
            return
        watched.setCursor(cursor)

    def _apply_panel_resize(self, global_position) -> None:
        start = self._interaction_geometry
        origin = self._interaction_start
        if start is None or origin is None:
            return
        delta = global_position - origin
        x, y, width, height = start.x(), start.y(), start.width(), start.height()
        edges = self._interaction
        minimum_width = 560
        minimum_height = 105
        if edges & Qt.Edge.LeftEdge:
            width -= delta.x()
            x += delta.x()
            if width < minimum_width:
                x -= minimum_width - width
                width = minimum_width
        elif edges & Qt.Edge.RightEdge:
            width = max(minimum_width, width + delta.x())
        if edges & Qt.Edge.BottomEdge:
            height = max(minimum_height, height + delta.y())
        self.setGeometry(x, y, width, height)
        self.panel_height = height
        self.panel_geometry = self.geometry()

    def _is_panel_drag_target(self, watched: QWidget) -> bool:
        return not isinstance(watched, (QAbstractButton, QAbstractSlider))

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if not isinstance(watched, QWidget) or watched.window() is not self:
            return False
        if event.type() == QEvent.Type.MouseButtonPress and isinstance(event, QMouseEvent):
            position = event.globalPosition().toPoint()
            if event.button() == Qt.MouseButton.RightButton:
                self.show_settings_menu(position)
                return True
            if event.button() == Qt.MouseButton.LeftButton:
                edges = self._resize_edges_at(position)
                if edges:
                    self._interaction = edges
                    self._interaction_start = position
                    self._interaction_geometry = self.geometry()
                    self.grabMouse()
                    return True
                if self.moving_enabled and self._is_panel_drag_target(watched):
                    self._interaction = "move"
                    self._interaction_start = position - self.frameGeometry().topLeft()
                    self.grabMouse()
                    return True
        elif event.type() == QEvent.Type.MouseMove and isinstance(event, QMouseEvent):
            position = event.globalPosition().toPoint()
            if self._interaction == "move" and event.buttons() & Qt.MouseButton.LeftButton:
                self.move(position - self._interaction_start)
                return True
            if isinstance(self._interaction, Qt.Edge) and event.buttons() & Qt.MouseButton.LeftButton:
                self._apply_panel_resize(position)
                return True
            self._update_resize_cursor(watched, self._resize_edges_at(position))
        elif event.type() == QEvent.Type.MouseButtonRelease and isinstance(event, QMouseEvent):
            if event.button() == Qt.MouseButton.LeftButton and self._interaction is not None:
                if self._interaction == "move" or isinstance(self._interaction, Qt.Edge):
                    self.panel_geometry = QRect(
                        self.geometry().x(),
                        self.geometry().y(),
                        self.geometry().width(),
                        self.panel_height,
                    )
                    self.player.save_window_settings()
                self._interaction = None
                self._interaction_start = None
                self._interaction_geometry = None
                self.releaseMouse()
                watched.unsetCursor()
                return True
        return False

    def toggle_panel(self) -> None:
        if not self.mini_mode:
            return
        self.animation.stop()
        self.expanded = not self.expanded
        if self.expanded:
            self.panel.show()
            self.collapse_button.setText("▲")
            self._position_overlay()
            self.show()
            self.raise_()
        else:
            self.collapse_button.setText("▼")
        start = self.geometry()
        end = QRect(
            start.x(),
            self.panel_geometry.y(),
            self.panel_geometry.width(),
            self.panel_height if self.expanded else 0,
        )
        self.animation.setStartValue(start)
        self.animation.setEndValue(end)
        self.animation.start()

    def _animation_finished(self) -> None:
        if not self.expanded:
            self.panel.hide()
            self.hide()
        self.setGeometry(
            self.panel_geometry.x(),
            self.panel_geometry.y(),
            self.panel_geometry.width(),
            self.panel_height if self.expanded else 0,
        )
        self._position_overlay(self.overlay_screen)

    def _position_overlay(self, screen=None) -> None:
        if screen is None:
            screen = self.overlay_screen
        if screen is None:
            screen = QApplication.primaryScreen()
        if screen is None:
            return
        center = self.panel_geometry.center()
        self.overlay_screen = QApplication.screenAt(center) or screen

    def enter_mini_mode(self) -> None:
        self.animation.stop()
        self.expanded = False
        self.mini_mode = True
        self.panel.hide()
        self.setGeometry(
            self.panel_geometry.x(),
            self.panel_geometry.y(),
            self.panel_geometry.width(),
            0,
        )
        self._position_overlay()
        self.hide()

    def hide_overlay(self) -> None:
        self.mini_mode = False
        self.animation.stop()
        self.hide()

    @staticmethod
    def _metadata_display_text(text: str) -> str:
        value = text.strip()
        return "" if value in {".", "·", "•", "∙"} else text

    def sync_from_player(self) -> None:
        player = self.player
        title = self._metadata_display_text(player.track_title.text())
        details = self._metadata_display_text(player.track_details.text())
        self.title_label.setText(title)
        self.title_label.setToolTip(title)
        self.details_label.setText(details)
        self.details_label.setToolTip(details)
        self.elapsed_label.setText(player.elapsed_label.text())
        self.duration_label.setText(player.duration_label.text())

        slider = player.position_slider
        if not player.dragging_position:
            self.position_slider.blockSignals(True)
            self.position_slider.setRange(slider.minimum(), slider.maximum())
            self.position_slider.setValue(slider.value())
            self.position_slider.setTimelineWheelStep(slider.wheel_step_ms)
            self.position_slider.setEnabled(
                player.active_playback_source is not None
            )
            self.position_slider.blockSignals(False)

        self.volume_slider.blockSignals(True)
        self.volume_slider.setValue(player.volume_slider.value())
        self.volume_slider.blockSignals(False)
        self.volume_label.setText(f"{player.volume_slider.value()}%")
        self.internet_button.setChecked(player.internet_enabled)
        self.internet_button.setProperty("active", player.internet_enabled)
        self.internet_button.setToolTip(
            f"Internet access: {'on' if player.internet_enabled else 'off'}"
        )
        self.internet_button.style().unpolish(self.internet_button)
        self.internet_button.style().polish(self.internet_button)
        self.play_button.setIcon(
            player.svg_icon("pause.svg" if player.is_playing and not player.is_paused else "play.svg")
        )
        self.play_button.setToolTip(
            "Pause" if player.is_playing and not player.is_paused else "Play"
        )
        source = player.active_playback_source
        active = source is not None and player.is_playing and not player.is_paused
        is_stream = source is not None and source.startswith(("http://", "https://"))
        self.live_badge.setText(
            "LIVE" if is_stream and active else "ON AIR" if active else "PAUSED" if player.is_paused else "READY"
        )
        self.live_badge.setProperty("active", active)
        self.live_badge.setProperty("live", is_stream and active)
        self.live_badge.style().unpolish(self.live_badge)
        self.live_badge.style().polish(self.live_badge)
        has_source = source is not None
        self.stop_button.setEnabled(has_source)
        self.previous_button.setEnabled(bool(player.playlist))
        self.next_button.setEnabled(bool(player.playlist))


class VolumeSlider(QSlider):
    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        width = self.width()
        height = self.height()
        track_width = 10
        track_x = (width - track_width) / 2.0
        track_top = 7
        track_bottom = height - 7
        usable_height = track_bottom - track_top

        painter.setPen(QColor("#6e9fc5"))
        painter.setBrush(QColor("#07111f"))
        painter.drawRoundedRect(
            int(track_x),
            track_top,
            track_width,
            usable_height,
            5,
            5,
        )

        ratio = max(0.0, min(1.0, (self.value() - self.minimum()) / max(1, self.maximum() - self.minimum())))
        filled_top = track_bottom - usable_height * ratio

        gradient = QLinearGradient(0, track_top, 0, track_bottom)
        gradient.setColorAt(0.0, QColor("#9d4cff"))
        gradient.setColorAt(0.55, QColor("#4fb8ff"))
        gradient.setColorAt(1.0, QColor("#19d4c8"))
        painter.setPen(QColor("#6e9fc5"))
        painter.setBrush(gradient)
        painter.drawRoundedRect(
            int(track_x),
            int(filled_top),
            track_width,
            max(0, int(track_bottom - filled_top)),
            5,
            5,
        )

        handle_y = track_bottom - usable_height * ratio
        handle_radius = 7
        painter.setPen(QColor("#72c1ff"))
        painter.setBrush(QColor("#f8fbff"))
        painter.drawEllipse(
            int(width / 2.0 - handle_radius),
            int(handle_y - handle_radius),
            handle_radius * 2,
            handle_radius * 2,
        )
        painter.end()


class PlaylistCardDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index) -> None:
        if not index.data(PLAYLIST_FILL_ROLE):
            super().paint(painter, option, index)
            return

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = option.rect.adjusted(3, 3, -3, -3)
        icon = index.data(Qt.ItemDataRole.DecorationRole)
        path = QPainterPath()
        path.addRoundedRect(
            rect.x(),
            rect.y(),
            rect.width(),
            rect.height(),
            5,
            5,
        )
        painter.setClipPath(path)

        if isinstance(icon, QIcon) and not icon.isNull():
            requested_size = QSize(rect.width() * 2, rect.height() * 2)
            source = icon.pixmap(requested_size)
            scaled = source.scaled(
                rect.size(),
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            source_rect = QRect(
                (scaled.width() - rect.width()) // 2,
                (scaled.height() - rect.height()) // 2,
                rect.width(),
                rect.height(),
            )
            painter.drawPixmap(rect, scaled, source_rect)
        else:
            painter.fillRect(rect, QColor("#091827"))

        text_height = 22
        text_rect = QRect(
            rect.left(),
            rect.bottom() - text_height + 1,
            rect.width(),
            text_height,
        )
        painter.fillRect(text_rect, QColor(5, 15, 27, 210))
        label = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        label = painter.fontMetrics().elidedText(
            label,
            Qt.TextElideMode.ElideRight,
            max(0, text_rect.width() - 12),
        )
        painter.setPen(QColor("#ffffff"))
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, label)
        painter.setClipping(False)

        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        border = "#8fc3e7" if selected else "#69b8e3" if hovered else "#315a78"
        painter.setPen(QColor(border))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 5, 5)
        painter.restore()


class AudioPlayer(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("SRPlayer")
        self.setWindowIcon(load_data_icon("SR.PNG"))
        self.resize(1180, 710)
        self.setMinimumSize(960, 620)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setAcceptDrops(True)
        self.window_settings_file = os.path.join(SAVE_DIR, "window_settings.json")
        self.default_window_size = QSize(1180, 710)
        self.window_resizable = True
        self.window_position_pinned = False
        self.window_stays_on_top = False
        self.hide_main_window_on_minimize = False
        self._resize_edges = Qt.Edge(0)
        self._resize_start_position = None
        self._resize_start_geometry = None
        self._mini_restore_maximized = False
        self._in_tray_mode = False
        self._mini_player_settings: dict = {}

        self.audio_format = QAudioFormat()
        self.audio_format.setSampleRate(AUDIO_SAMPLE_RATE)
        self.audio_format.setChannelCount(AUDIO_CHANNELS)
        self.audio_format.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        self.audio_sink = QAudioSink(
            QMediaDevices.defaultAudioOutput(),
            self.audio_format,
            self,
        )
        self.audio_sink.setBufferSize(AUDIO_SAMPLE_RATE * AUDIO_BYTES_PER_FRAME // 4)
        self.audio_output_device = None
        self.internet_enabled = False
        self.stream_decoder: Optional[StreamDecoder] = None
        self.stream_jitter_buffer = bytearray()
        self.stream_buffer_ready = False
        self.stream_buffer_ms = 3000
        self.stream_history = bytearray()
        self.stream_history_max_ms = 30000
        self.stream_rewind_mode = False
        self.stream_rewind_buffer = bytearray()
        self.stream_rewind_cursor_ms = 0

        self.playlist: List[str] = []
        self.playlist_labels: List[str] = []
        self.stream_buffer_ms_by_item: List[int] = []
        self.playlists: dict[str, dict[str, List[str]]] = {}
        self.audio_metadata_cache: dict[str, tuple[dict[str, str], float]] = {}
        self.current_playlist_name = ""
        self.playlists_file = os.path.join(SAVE_DIR, "playlists.json")
        self.load_playlists()
        self.current_index = -1
        self.active_playback_source: Optional[str] = None
        self.active_playlist_name = ""
        self.audio_recorder: Optional[AudioRecorder] = None
        self.recording_format = "wav"
        self.recording_directory = RECORDINGS_DIR
        self.recording_saved_path = ""
        self.recording_hide_timer = QTimer(self)
        self.recording_hide_timer.setSingleShot(True)
        self.recording_hide_timer.timeout.connect(self.hide_recording_status)
        self.shuffle_enabled = False
        self.repeat_enabled = False
        self.paused_position = 0.0
        self.playback_started_at = 0.0
        self.is_paused = False
        self.is_playing = False
        self.dragging_position = False
        self._drag_position = None
        self.load_window_settings()
        self.playlist_navigation_timer = QTimer(self)
        self.playlist_navigation_timer.setSingleShot(True)
        self.playlist_navigation_timer.setInterval(3000)
        self.playlist_navigation_timer.timeout.connect(self.restore_current_playlist_selection)

        self.build_ui()
        self.apply_theme()
        self.setup_system_tray()
        self.build_shortcuts()
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
        self.refresh_toggle(self.shuffle_button)
        self.refresh_toggle(self.repeat_button)

        self.equalizer_values = {
            "preamp": 0,
            "60": 0,
            "170": 0,
            "350": 0,
            "1000": 0,
            "3500": 0,
            "10000": 0,
            "16000": 0,
        }
        self.effects_values = {
            "bass": 0,
            "treble": 0,
            "clarity": 0,
            "echo": 0,
            "reverb": 0,
            "pan": 0,
            "gain": 0,
            "underwater": 0,
            "voice": 0,
        }
        self.audio_settings_file = os.path.join(SAVE_DIR, "audio_settings.json")
        self.audio_processing_enabled = False
        self.load_audio_settings()
        self.update_record_button_tooltip()
        self.audio_settings_dialog: Optional[AudioSettingsDialog] = None
        self.audio_feed_timer = QTimer(self)
        self.audio_feed_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.audio_feed_timer.setInterval(20)
        self.audio_feed_timer.timeout.connect(self.feed_stream_audio)
        self.audio_feed_timer.start()
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.update_progress)
        self.timer.start()
        self.set_volume(75)
        self.apply_equalizer_settings()
        self.apply_effects_settings()

    def svg_icon(self, name: str) -> QIcon:
        icon = load_data_icon(name)
        if icon.isNull():
            raise FileNotFoundError(os.path.join(DATA_DIR, name))
        return icon

    def setup_system_tray(self) -> None:
        self.tray_available = QSystemTrayIcon.isSystemTrayAvailable()
        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setIcon(self.windowIcon())
        self.tray_icon.setToolTip("SR Player")
        self.tray_menu = QMenu(self)
        show_mini_action = self.tray_menu.addAction("Show mini player")
        show_mini_action.triggered.connect(self.toggle_mini_player)
        restore_action = self.tray_menu.addAction("Open SR Player")
        restore_action.triggered.connect(self.restore_from_mini_player)
        self.tray_menu.addSeparator()
        exit_action = self.tray_menu.addAction("Exit SR Player")
        exit_action.triggered.connect(self.close)
        self.tray_icon.setContextMenu(self.tray_menu)
        self.tray_icon.activated.connect(self.handle_tray_activation)

    def handle_tray_activation(
        self,
        reason: QSystemTrayIcon.ActivationReason,
    ) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.toggle_mini_player()
        elif reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.restore_from_mini_player()

    def toggle_mini_player(self) -> None:
        if not self._in_tray_mode:
            return
        self.mini_player.sync_from_player()
        self.mini_player.toggle_panel()

    def load_playlists(self) -> None:
        default = {
            "My Playlist": {
                "sources": [],
                "labels": [],
                "stream_buffer_ms": [],
                "cover": "",
            },
        }
        try:
            os.makedirs(SAVE_DIR, exist_ok=True)
            old_playlists_file = os.path.join(DATA_DIR, "playlists.json")
            if not os.path.isfile(self.playlists_file) and os.path.isfile(old_playlists_file):
                shutil.copy2(old_playlists_file, self.playlists_file)
                os.remove(old_playlists_file)
            elif os.path.isfile(old_playlists_file):
                os.remove(old_playlists_file)
            if os.path.isfile(self.playlists_file):
                with open(self.playlists_file, "r", encoding="utf-8") as handle:
                    saved = json.load(handle)
                if isinstance(saved, dict) and saved:
                    default = saved
                    self.migrate_saved_covers(default)
        except Exception as exc:
            print(f"Could not load playlists: {exc}")
        self.playlists = default
        self.current_playlist_name = next(iter(self.playlists), "")
        if self.current_playlist_name:
            data = self.playlists[self.current_playlist_name]
            self.playlist = list(data.get("sources", []))
            self.playlist_labels = list(data.get("labels", []))
            self.stream_buffer_ms_by_item = list(
                data.get("stream_buffer_ms", [])
            )

    def save_playlists(self) -> None:
        try:
            os.makedirs(SAVE_DIR, exist_ok=True)
            with open(self.playlists_file, "w", encoding="utf-8") as handle:
                json.dump(self.playlists, handle, ensure_ascii=False, indent=2)
        except Exception as exc:
            print(f"Could not save playlists: {exc}")

    def load_audio_settings(self) -> None:
        try:
            with open(self.audio_settings_file, "r", encoding="utf-8") as handle:
                settings = json.load(handle)
            if not isinstance(settings, dict):
                return
            equalizer = settings.get("equalizer", {})
            effects = settings.get("effects", {})
            if isinstance(equalizer, dict):
                for key in self.equalizer_values:
                    value = equalizer.get(key)
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        self.equalizer_values[key] = max(-12, min(12, int(value)))
            if isinstance(effects, dict):
                for key in self.effects_values:
                    value = effects.get(key)
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        lower, upper = {
                            "bass": (-12, 12),
                            "treble": (-12, 12),
                            "clarity": (-10, 10),
                            "echo": (0, 100),
                            "reverb": (0, 100),
                            "pan": (-100, 100),
                            "gain": (-6, 6),
                            "underwater": (0, 100),
                            "voice": (0, 7),
                        }[key]
                        self.effects_values[key] = max(lower, min(upper, int(value)))
            enabled = settings.get("enabled", False)
            self.audio_processing_enabled = enabled if isinstance(enabled, bool) else False
            recording_format = settings.get("recording_format", "wav")
            if recording_format in RECORDING_FORMATS:
                self.recording_format = recording_format
            recording_directory = settings.get("recording_directory")
            if isinstance(recording_directory, str) and recording_directory.strip():
                self.recording_directory = os.path.abspath(recording_directory)
        except FileNotFoundError:
            self.audio_processing_enabled = False
        except (OSError, ValueError, TypeError, OverflowError) as exc:
            print(f"Could not load audio settings: {exc}")
            self.audio_processing_enabled = False

    def save_audio_settings(self) -> None:
        settings = {
            "enabled": self.audio_processing_enabled,
            "equalizer": dict(self.equalizer_values),
            "effects": dict(self.effects_values),
            "recording_format": self.recording_format,
            "recording_directory": self.recording_directory,
        }
        try:
            os.makedirs(SAVE_DIR, exist_ok=True)
            with open(self.audio_settings_file, "w", encoding="utf-8") as handle:
                json.dump(settings, handle, ensure_ascii=False, indent=2)
        except OSError as exc:
            print(f"Could not save audio settings: {exc}")

    def migrate_saved_covers(self, playlists: dict) -> None:
        changed = False
        for data in playlists.values():
            if not isinstance(data, dict):
                continue
            sources = list(data.get("sources", []))
            buffers = list(data.get("stream_buffer_ms", []))
            if len(buffers) != len(sources):
                buffers = [
                    max(500, min(10000, buffer if isinstance(buffer, int) else 3000))
                    for buffer in buffers[:len(sources)]
                ]
                while len(buffers) < len(sources):
                    buffers.append(3000)
                data["stream_buffer_ms"] = buffers
                changed = True
            cover = data.get("cover", "")
            if not cover:
                continue
            if os.path.isabs(cover):
                continue
            if cover.startswith("playlist-covers" + os.sep) or cover.startswith("playlist-covers/"):
                legacy_cover = os.path.normpath(os.path.join(SAVE_DIR, cover))
                if os.path.isfile(legacy_cover):
                    data["cover"] = os.path.abspath(legacy_cover)
                    changed = True
                else:
                    data["cover"] = ""
                    changed = True
                    continue
            else:
                legacy_cover = os.path.normpath(os.path.join(DATA_DIR, cover))
                if os.path.isfile(legacy_cover):
                    data["cover"] = os.path.abspath(legacy_cover)
                    changed = True
        if changed:
            try:
                with open(self.playlists_file, "w", encoding="utf-8") as handle:
                    json.dump(playlists, handle, ensure_ascii=False, indent=2)
            except Exception as exc:
                print(f"Could not migrate playlist cover paths: {exc}")

    def build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("window")
        central.setMouseTracking(True)
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Custom dark title bar
        titlebar = QWidget()
        self.titlebar = titlebar
        titlebar.setObjectName("titlebar")
        titlebar.setFixedHeight(50)
        titlebar.setMouseTracking(True)
        title_layout = QHBoxLayout(titlebar)
        title_layout.setContentsMargins(12, 0, 8, 0)
        title_layout.setSpacing(4)
        brand_logo = QLabel()
        brand_logo.setObjectName("brandLogo")
        brand_logo.setFixedSize(44, 44)
        brand_logo.setScaledContents(True)
        brand_logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        brand_logo_pixmap = load_data_pixmap("SR.PNG")
        if not brand_logo_pixmap.isNull():
            brand_logo.setPixmap(
                brand_logo_pixmap.scaled(
                    brand_logo.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        brand_variant = QLabel("SR player")
        brand_variant.setObjectName("brandVariant")
        title_layout.addWidget(brand_logo)
        title_layout.addWidget(brand_variant)
        title_layout.addStretch()
        self.minimize_button = self.tool_icon("minus.svg", "Minimize")
        self.close_button = self.tool_icon("close.svg", "Close")
        self.maximize_button = self.tool_icon("full_screen.svg", "Maximize", fixed=38)
        self.maximize_button.setObjectName("windowButton")
        self.maximize_button.setFixedSize(38, 38)
        self.maximize_button.setToolTip("Maximize")
        self.minimize_button.setObjectName("titleMinimizeButton")
        self.close_button.setObjectName("titleCloseButton")
        self.minimize_button.setFixedSize(38, 38)
        self.close_button.setFixedSize(38, 38)
        self.minimize_button.clicked.connect(self.minimize_to_mini_player)
        self.maximize_button.clicked.connect(self.toggle_maximized)
        self.close_button.clicked.connect(self.close)
        title_layout.addWidget(self.minimize_button)
        title_layout.addWidget(self.maximize_button)
        title_layout.addWidget(self.close_button)
        root.addWidget(titlebar)

        main = QWidget()
        main.setMouseTracking(True)
        main_layout = QHBoxLayout(main)
        main_layout.setContentsMargins(10, 10, 10, 8)
        main_layout.setSpacing(10)

        # Playlist cards pane
        left_pane = QWidget()
        left_pane.setMaximumWidth(280)
        left_pane.setObjectName("playlistPane")
        left_pane_layout = QVBoxLayout(left_pane)
        left_pane_layout.setContentsMargins(0, 0, 0, 0)
        left_pane_layout.setSpacing(8)
        left_controls = QWidget()
        left_controls.setObjectName("panelTop")
        left_controls_layout = QHBoxLayout(left_controls)
        left_controls_layout.setContentsMargins(10, 5, 10, 5)
        panel_title = QLabel("PLAYLISTS")
        panel_title.setObjectName("panelTitle")
        left_controls_layout.addWidget(panel_title)
        left_controls_layout.addStretch()
        new_button = self.tool_icon("plus.svg", "New playlist", fixed=32)
        delete_button = self.tool_icon("trash.svg", "Delete playlist", fixed=32)
        logo_button = self.tool_icon("logo.svg", "Change playlist logo", fixed=32)
        new_button.clicked.connect(self.new_playlist)
        delete_button.clicked.connect(self.delete_playlist)
        logo_button.clicked.connect(self.change_playlist_logo)
        left_controls_layout.addWidget(new_button)
        left_controls_layout.addWidget(logo_button)
        left_controls_layout.addWidget(delete_button)
        left_pane_layout.addWidget(left_controls)

        self.playlist_cards = QListWidget()
        self.playlist_cards.setObjectName("playlistCards")
        self.playlist_cards.setViewMode(QListWidget.ViewMode.IconMode)
        self.playlist_cards.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.playlist_cards.setMovement(QListWidget.Movement.Snap)
        self.playlist_cards.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self.playlist_cards.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.playlist_cards.setDropIndicatorShown(True)
        self.playlist_cards.setItemDelegate(PlaylistCardDelegate(self.playlist_cards))
        self.playlist_cards.setSpacing(8)
        self.playlist_cards.setIconSize(QSize(48, 48))
        self.playlist_cards.setGridSize(QSize(116, 100))
        self.playlist_cards.model().rowsMoved.connect(self.save_playlist_card_order)
        self.playlist_cards.itemClicked.connect(self.select_playlist_card)
        self.playlist_cards.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.playlist_cards.customContextMenuRequested.connect(self.show_playlist_card_menu)
        left_pane_layout.addWidget(self.playlist_cards, 1)
        main_layout.addWidget(left_pane, 3)

        # Right player and playlist pane
        right_pane = QWidget()
        right_layout = QVBoxLayout(right_pane)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(6)

        top_info = QWidget()
        top_info.setObjectName("trackHeader")
        top_info_layout = QVBoxLayout(top_info)
        top_info_layout.setContentsMargins(12, 8, 12, 8)
        self.track_title = QLabel("No track loaded")
        self.track_title.setObjectName("trackTitle")
        self.track_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.track_details = QLabel("Add audio files to start listening")
        self.track_details.setObjectName("trackDetails")
        self.track_details.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.track_details.setWordWrap(True)
        top_info_layout.addWidget(self.track_title)
        top_info_layout.addWidget(self.track_details)
        right_layout.addWidget(top_info)

        timeline = QHBoxLayout()
        self.elapsed_label = QLabel("00:00")
        self.elapsed_label.setObjectName("timeLabel")
        self.duration_label = QLabel("00:00")
        self.duration_label.setObjectName("timeLabel")
        self.position_slider = TimelineSlider(Qt.Orientation.Horizontal)
        self.position_slider.setRange(0, 1000)
        self.position_slider.sliderPressed.connect(self.position_pressed)
        self.position_slider.sliderReleased.connect(self.seek_position)
        self.position_slider.valueChanged.connect(self.timeline_value_changed)
        self.position_slider.wheelChanged.connect(self.timeline_wheel_changed)
        timeline.addWidget(self.elapsed_label)
        timeline.addWidget(self.position_slider, 1)
        timeline.addWidget(self.duration_label)
        right_layout.addLayout(timeline)

        transport_row = QHBoxLayout()
        self.volume_slider = VolumeSlider(Qt.Orientation.Vertical)
        self.volume_slider.setObjectName("volumeSlider")
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(75)
        self.volume_slider.setFixedHeight(78)
        self.volume_slider.setFixedWidth(28)
        self.volume_slider.valueChanged.connect(self.set_volume)
        self.volume_label = QLabel("75%")
        self.volume_label.setObjectName("volumeLabel")
        self.volume_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        transport_row.addWidget(self.volume_slider)
        transport_row.addWidget(self.volume_label)
        transport_row.addStretch()

        self.previous_button = self.tool_icon("previous.svg", "Previous")
        self.play_button = self.tool_icon("play.svg", "Play")
        self.stop_button = self.tool_icon("stop.svg", "Stop")
        self.record_button = self.tool_icon("record.svg", "Start recording")
        self.record_button.setObjectName("recordButton")
        self.record_button.setProperty("recording", False)
        self.record_button.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.record_button.customContextMenuRequested.connect(
            self.show_recording_format_menu
        )
        self.audio_settings_button = self.tool_icon(
            "audio.svg",
            "Audio effects and voice presets",
        )
        self.next_button = self.tool_icon("next.svg", "Next")
        self.shuffle_button = self.tool_icon("shuffle.svg", "Shuffle")
        self.repeat_button = self.tool_icon("repeat.svg", "Repeat")

        for button in (
            self.previous_button,
            self.play_button,
            self.stop_button,
            self.next_button,
            self.shuffle_button,
            self.repeat_button,
            self.audio_settings_button,
            self.record_button,
        ):
            transport_row.addWidget(button)

        self.previous_button.clicked.connect(self.previous_track)
        self.play_button.clicked.connect(self.toggle_play_pause)
        self.stop_button.clicked.connect(self.stop)
        self.record_button.clicked.connect(self.toggle_recording)
        self.audio_settings_button.clicked.connect(self.open_audio_settings_dialog)
        self.next_button.clicked.connect(self.next_track)
        self.shuffle_button.clicked.connect(self.toggle_shuffle)
        self.repeat_button.clicked.connect(self.toggle_repeat)
        right_layout.addLayout(transport_row)

        self.current_playlist_label = QLabel(self.current_playlist_name)
        self.current_playlist_label.setObjectName("appTitle")
        right_layout.addWidget(self.current_playlist_label)

        self.selection_toolbar = QWidget()
        self.selection_toolbar.setObjectName("selectionToolbar")
        selection_layout = QHBoxLayout(self.selection_toolbar)
        selection_layout.setContentsMargins(6, 3, 6, 3)
        selection_layout.setSpacing(5)
        self.selection_count_label = QLabel("0 selected")
        self.selection_count_label.setObjectName("selectionCount")
        selection_layout.addWidget(self.selection_count_label)
        selection_layout.addStretch()
        self.selection_all_button = QPushButton("Select all")
        self.selection_all_button.setToolTip("Select all visible items (Ctrl+A)")
        self.selection_clear_button = QPushButton("Clear")
        self.selection_clear_button.setToolTip("Clear the current selection")
        self.selection_move_button = QPushButton("Move to...")
        self.selection_move_button.setToolTip("Move selected items to another playlist")
        self.selection_delete_button = QPushButton("Delete")
        self.selection_delete_button.setObjectName("selectionDeleteButton")
        self.selection_delete_button.setToolTip("Delete selected items from this playlist")
        self.selection_done_button = QPushButton("Done")
        self.selection_done_button.setToolTip("Exit selection mode (Esc)")
        selection_layout.addWidget(self.selection_all_button)
        selection_layout.addWidget(self.selection_clear_button)
        selection_layout.addWidget(self.selection_move_button)
        selection_layout.addWidget(self.selection_delete_button)
        selection_layout.addWidget(self.selection_done_button)
        self.selection_toolbar.hide()
        right_layout.addWidget(self.selection_toolbar)

        self.playlist_widget = PlaylistWidget()
        self._selection_mode = False
        self.playlist_widget.setDragDropMode(
            QListWidget.DragDropMode.InternalMove
        )
        self.playlist_widget.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.playlist_widget.setDragEnabled(True)
        self.playlist_widget.setAcceptDrops(True)
        self.playlist_widget.setDropIndicatorShown(True)
        self.playlist_widget.setDragDropOverwriteMode(False)
        self.playlist_widget.model().rowsMoved.connect(
            self.handle_playlist_rows_moved
        )
        self.playlist_widget.itemDoubleClicked.connect(self.play_selected)
        self.playlist_widget.itemActivated.connect(self.play_selected)
        self.playlist_widget.enterPressed.connect(self.play_selected)
        self.playlist_widget.arrowMoved.connect(self.restart_playlist_navigation_timer)
        self.playlist_widget.itemSelectionChanged.connect(
            self._update_selection_toolbar
        )
        self.playlist_widget.selectionModeEscape.connect(
            lambda: self.set_selection_mode(False)
        )
        self.selection_all_button.clicked.connect(self.select_all_visible_tracks)
        self.selection_clear_button.clicked.connect(
            self.playlist_widget.clearSelection
        )
        self.selection_move_button.clicked.connect(self.show_move_selected_menu)
        self.selection_delete_button.clicked.connect(self.delete_selected_tracks)
        self.selection_done_button.clicked.connect(
            lambda: self.set_selection_mode(False)
        )
        self.playlist_widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.playlist_widget.customContextMenuRequested.connect(
            self.show_track_context_menu
        )
        right_layout.addWidget(self.playlist_widget, 1)
        for index, source in enumerate(self.playlist):
            label = (
                self.playlist_labels[index]
                if index < len(self.playlist_labels)
                else self.metadata_title(source)
            )
            item = QListWidgetItem(label)
            item.setToolTip(source)
            item.setForeground(QColor("#ff8fa0" if label.startswith("[Radio]") else "#b9dcf5"))
            self.playlist_widget.addItem(item)

        self.refresh_playlist_cards()
        playlist_actions = QHBoxLayout()
        add_button = self.tool_icon("plus.svg", "Add files")
        folder_button = self.tool_icon("folder.svg", "Add folder")
        radio_button = self.tool_icon("volume.svg", "Add radio stream")
        remove_button = self.tool_icon("trash.svg", "Remove selected")
        search_button = self.tool_icon("lens.svg", "Search playlist")
        self.internet_button = self.tool_icon(
            "internet.svg",
            f"Internet access: {'on' if self.internet_enabled else 'off'}",
        )
        self.internet_button.setObjectName("internetAccessButton")
        self.internet_button.setCheckable(True)
        self.internet_button.setChecked(self.internet_enabled)
        self.internet_button.setProperty("active", self.internet_enabled)
        self.internet_button.setToolTip(
            f"Internet access: {'on' if self.internet_enabled else 'off'}"
        )
        self.search_button = search_button
        add_button.clicked.connect(self.add_files)
        folder_button.clicked.connect(self.add_folder)
        radio_button.clicked.connect(self.add_radio)
        remove_button.clicked.connect(self.remove_selected)
        self.search_field = QLineEdit()
        self.search_field.setObjectName("playlistSearchField")
        self.search_field.setPlaceholderText("Search playlist...")
        self.search_field.setFixedHeight(32)
        self.search_field.setFixedWidth(220)
        self.search_field.hide()
        self.search_field.textChanged.connect(self.search_playlist)
        search_button.clicked.connect(self.toggle_playlist_search)
        self.internet_button.clicked.connect(self.toggle_internet_access)
        playlist_actions.addWidget(add_button)
        playlist_actions.addWidget(folder_button)
        playlist_actions.addWidget(radio_button)
        playlist_actions.addWidget(remove_button)
        playlist_actions.addWidget(search_button)
        playlist_actions.addWidget(self.search_field)
        playlist_actions.addStretch()
        playlist_actions.addWidget(self.internet_button)

        right_layout.addLayout(playlist_actions)
        main_layout.addWidget(right_pane, 7)

        footer = QWidget()
        footer.setObjectName("footer")
        footer.setFixedHeight(34)
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(8, 0, 8, 0)
        footer_layout.setSpacing(8)
        self.recording_status_label = QLabel("Ready")
        self.recording_status_label.setObjectName("bottomStatus")
        self.recording_status_label.setProperty("recording", False)
        footer_layout.addWidget(self.recording_status_label)
        footer_layout.addStretch()
        self.recording_timer_label = QLabel("00:00:00")
        self.recording_timer_label.setObjectName("recordingTimer")
        self.recording_timer_label.hide()
        footer_layout.addWidget(self.recording_timer_label)
        self.recording_pause_button = self.tool_icon(
            "pause.svg", "Pause recording", fixed=28
        )
        self.recording_pause_button.setObjectName("recordingPauseButton")
        self.recording_pause_button.clicked.connect(self.toggle_recording_pause)
        self.recording_pause_button.hide()
        footer_layout.addWidget(self.recording_pause_button)
        root.addWidget(main, 1)
        root.addWidget(footer)
        self.mini_player = TopMiniPlayer(self)
        self.mini_player.apply_settings(self._mini_player_settings)

    def tool_icon(
        self,
        file_name: str,
        tooltip: str,
        fixed: int = 38,
    ) -> QToolButton:
        button = QToolButton()
        button.setIcon(self.svg_icon(file_name))
        button.setIconSize(QSize(fixed - 12, fixed - 12))
        button.setFixedSize(fixed, fixed)
        button.setToolTip(tooltip)
        button.setAutoRaise(True)
        button.setObjectName("iconButton")
        return button

    def build_shortcuts(self) -> None:
        action = QAction("Play/Pause", self)
        action.setShortcut(QKeySequence("Space"))
        action.triggered.connect(self.toggle_play_pause)
        self.addAction(action)

    def restart_playlist_navigation_timer(self) -> None:
        self.playlist_navigation_timer.start(3000)

    def restore_current_playlist_selection(self) -> None:
        if 0 <= self.current_index < self.playlist_widget.count():
            self.playlist_widget.setCurrentRow(self.current_index)
            self.playlist_widget.scrollToItem(
                self.playlist_widget.item(self.current_index)
            )

    def sync_current_playlist(self) -> None:
        if not self.current_playlist_name:
            return
        while len(self.playlist_labels) < len(self.playlist):
            self.playlist_labels.append("")
        while len(self.stream_buffer_ms_by_item) < len(self.playlist):
            self.stream_buffer_ms_by_item.append(3000)
        old_data = self.playlists[self.current_playlist_name]
        self.playlists[self.current_playlist_name] = {
            "sources": list(self.playlist),
            "labels": list(self.playlist_labels),
            "stream_buffer_ms": list(self.stream_buffer_ms_by_item),
            "cover": old_data.get("cover", ""),
            "cover_fill": bool(old_data.get("cover_fill", False)),
        }

    def handle_playlist_rows_moved(
        self,
        _source_parent,
        source_start: int,
        source_end: int,
        _destination_parent,
        destination_child: int,
    ) -> None:
        if (
            source_start < 0
            or source_end >= len(self.playlist)
            or source_start > source_end
        ):
            return

        moved_count = source_end - source_start + 1
        destination = destination_child
        if destination_child > source_end:
            destination -= moved_count
        if source_start <= destination <= source_end:
            return

        old_current_index = self.current_index

        def move_rows(values: list) -> list:
            moved = values[source_start : source_end + 1]
            remaining = values[:source_start] + values[source_end + 1 :]
            remaining[destination:destination] = moved
            return remaining

        self.playlist = move_rows(self.playlist)
        self.playlist_labels = move_rows(self.playlist_labels)
        self.stream_buffer_ms_by_item = move_rows(self.stream_buffer_ms_by_item)

        if source_start <= old_current_index <= source_end:
            self.current_index = destination + old_current_index - source_start
        elif old_current_index > source_end:
            self.current_index = old_current_index - moved_count
            if self.current_index >= destination:
                self.current_index += moved_count
        elif old_current_index >= destination:
            self.current_index = old_current_index + moved_count

        self.sync_current_playlist()
        self.save_playlists()
        self._update_selection_toolbar()

    def refresh_playlist_cards(self) -> None:
        self.playlist_cards.clear()
        for name, data in self.playlists.items():
            count = len(data.get("sources", []))
            cover = self.resolve_playlist_cover(data.get("cover", ""))
            if cover and os.path.isfile(cover):
                pixmap = QPixmap(cover)
                icon = QIcon(pixmap) if not pixmap.isNull() else self.svg_icon("logo.svg")
            else:
                icon = self.svg_icon("logo.svg")
            item = QListWidgetItem(icon, name)
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setData(
                PLAYLIST_FILL_ROLE,
                bool(data.get("cover_fill", False)),
            )
            item.setToolTip(f"{name} - {count} items")
            item.setForeground(QColor("#b9dcf5"))
            item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)
            item.setSizeHint(QSize(108, 92))
            self.playlist_cards.addItem(item)
        for row in range(self.playlist_cards.count()):
            item = self.playlist_cards.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == self.current_playlist_name:
                self.playlist_cards.setCurrentItem(item)
                break

    def save_playlist_card_order(self, *_args) -> None:
        ordered_playlists = {}
        for row in range(self.playlist_cards.count()):
            item = self.playlist_cards.item(row)
            name = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(name, str) and name in self.playlists:
                ordered_playlists[name] = self.playlists[name]
        if len(ordered_playlists) != len(self.playlists):
            return
        self.playlists = ordered_playlists
        self.save_playlists()

    def select_playlist_card(self, item: QListWidgetItem) -> None:
        name = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(name, str):
            self.switch_playlist(name)

    def set_selection_mode(self, enabled: bool, initial_row: Optional[int] = None) -> None:
        self._selection_mode = bool(enabled)
        self.playlist_widget.selection_mode = self._selection_mode
        self.playlist_widget.setSelectionMode(
            QListWidget.SelectionMode.ExtendedSelection
            if self._selection_mode
            else QListWidget.SelectionMode.SingleSelection
        )
        if not self._selection_mode:
            self.playlist_widget.clearSelection()
            if 0 <= self.current_index < self.playlist_widget.count():
                self.playlist_widget.setCurrentRow(self.current_index)
        elif initial_row is not None and 0 <= initial_row < self.playlist_widget.count():
            self.playlist_widget.clearSelection()
            self.playlist_widget.item(initial_row).setSelected(True)
            self.playlist_widget.setCurrentRow(initial_row)
        self.selection_toolbar.setVisible(self._selection_mode)
        self._update_selection_toolbar()
        if self._selection_mode:
            self.playlist_widget.setFocus()

    def _selected_playlist_rows(self) -> list[int]:
        return sorted(
            {
                self.playlist_widget.row(item)
                for item in self.playlist_widget.selectedItems()
                if 0 <= self.playlist_widget.row(item) < len(self.playlist)
            }
        )

    def _update_selection_toolbar(self) -> None:
        if not hasattr(self, "selection_toolbar"):
            return
        selected_count = len(self._selected_playlist_rows())
        self.selection_count_label.setText(
            f"{selected_count} selected"
        )
        self.selection_clear_button.setEnabled(selected_count > 0)
        self.selection_delete_button.setEnabled(selected_count > 0)
        self.selection_move_button.setEnabled(
            selected_count > 0
            and any(name != self.current_playlist_name for name in self.playlists)
        )

    def select_all_visible_tracks(self) -> None:
        self.playlist_widget.clearSelection()
        for row in range(self.playlist_widget.count()):
            item = self.playlist_widget.item(row)
            if item is not None:
                item.setSelected(not item.isHidden())

    def show_move_selected_menu(self) -> None:
        rows = self._selected_playlist_rows()
        destinations = [
            name for name in self.playlists if name != self.current_playlist_name
        ]
        if not rows or not destinations:
            return
        menu = QMenu(self.selection_move_button)
        actions = {
            menu.addAction(name): name
            for name in destinations
        }
        selected_action = menu.exec(
            self.selection_move_button.mapToGlobal(
                self.selection_move_button.rect().bottomLeft()
            )
        )
        destination = actions.get(selected_action)
        if destination is not None:
            self.move_selected_tracks(destination)

    def move_selected_tracks(self, destination: str) -> None:
        if destination not in self.playlists or destination == self.current_playlist_name:
            return
        rows = self._selected_playlist_rows()
        if not rows:
            return

        target = self.playlists[destination]
        target_sources = list(target.get("sources", []))
        target_labels = list(target.get("labels", []))
        target_buffers = list(target.get("stream_buffer_ms", []))
        while len(target_labels) < len(target_sources):
            target_labels.append("")
        while len(target_buffers) < len(target_sources):
            target_buffers.append(3000)

        rows_to_move = []
        skipped_duplicates = 0
        for row in rows:
            source = self.playlist[row]
            if source in target_sources:
                skipped_duplicates += 1
                continue
            item = self.playlist_widget.item(row)
            label = (
                self.playlist_labels[row]
                if row < len(self.playlist_labels)
                else (item.text() if item is not None else source)
            )
            target_sources.append(source)
            target_labels.append(label)
            buffer_ms = (
                self.stream_buffer_ms_by_item[row]
                if row < len(self.stream_buffer_ms_by_item)
                else 3000
            )
            target_buffers.append(max(500, min(10000, buffer_ms)))
            rows_to_move.append(row)

        if not rows_to_move:
            QMessageBox.information(
                self,
                "Items already in playlist",
                f"All {skipped_duplicates} selected items already exist in '{destination}'.",
            )
            return

        answer = QMessageBox.question(
            self,
            "Move selected items",
            f"Move {len(rows_to_move)} selected items to '{destination}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        target["sources"] = target_sources
        target["labels"] = target_labels
        target["stream_buffer_ms"] = target_buffers
        self.remove_playlist_rows(rows_to_move)
        self.set_selection_mode(False)
        if skipped_duplicates:
            QMessageBox.information(
                self,
                "Some items were not moved",
                f"{skipped_duplicates} selected items were already in '{destination}' and remain in this playlist.",
            )

    def delete_selected_tracks(self) -> None:
        rows = self._selected_playlist_rows()
        if not rows:
            return
        answer = QMessageBox.question(
            self,
            "Delete selected items",
            f"Delete {len(rows)} selected items from '{self.current_playlist_name}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.remove_playlist_rows(rows)
        self.set_selection_mode(False)

    def show_playlist_card_menu(self, position) -> None:
        item = self.playlist_cards.itemAt(position)
        if item is None:
            return
        name = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(name, str):
            return

        menu = QMenu(self)
        rename_action = menu.addAction("Rename playlist")
        change_action = menu.addAction("Change logo")
        fill_action = menu.addAction("Fill card with logo")
        fill_action.setCheckable(True)
        fill_action.setChecked(
            bool(self.playlists[name].get("cover_fill", False))
        )
        delete_action = menu.addAction("Delete playlist")
        selected_action = menu.exec(
            self.playlist_cards.viewport().mapToGlobal(position)
        )
        if selected_action == change_action:
            self.change_playlist_logo(name)
        elif selected_action == rename_action:
            self.rename_playlist(name)
        elif selected_action == fill_action:
            self.toggle_playlist_cover_fill(name, fill_action.isChecked())
        elif selected_action == delete_action:
            self.delete_playlist(name)

    def toggle_playlist_cover_fill(self, playlist_name: str, enabled: bool) -> None:
        data = self.playlists.get(playlist_name)
        if data is None:
            return
        data["cover_fill"] = enabled
        self.refresh_playlist_cards()
        self.save_playlists()

    def switch_playlist(self, name: str) -> None:
        if not name or name == self.current_playlist_name or name not in self.playlists:
            return
        if self._selection_mode:
            self.set_selection_mode(False)
        keep_playing = self.active_playback_source is not None and (
            self.is_playing or self.is_paused
        )
        if not keep_playing:
            self.stop()
        self.sync_current_playlist()
        self.current_playlist_name = name
        self.current_playlist_label.setText(name)
        data = self.playlists[name]
        self.playlist = list(data.get("sources", []))
        self.playlist_labels = list(data.get("labels", []))
        self.stream_buffer_ms_by_item = list(data.get("stream_buffer_ms", []))
        while len(self.stream_buffer_ms_by_item) < len(self.playlist):
            self.stream_buffer_ms_by_item.append(3000)
        self.current_index = -1
        if (
            keep_playing
            and self.active_playlist_name == name
            and self.active_playback_source in self.playlist
        ):
            self.current_index = self.playlist.index(self.active_playback_source)
        self.playlist_widget.clear()
        for index, source in enumerate(self.playlist):
            label = self.playlist_labels[index] if index < len(self.playlist_labels) else ""
            if not label:
                label = self.metadata_title(source) if not source.startswith(("http://", "https://")) else source
            item = QListWidgetItem(label)
            item.setToolTip(source)
            item.setForeground(QColor("#ff8fa0" if label.startswith("[Radio]") else "#b9dcf5"))
            self.playlist_widget.addItem(item)
        if not keep_playing:
            self.reset_labels()
        self.refresh_playlist_cards()
        self.save_playlists()

    def resolve_playlist_cover(self, cover: str) -> str:
        if not cover:
            return ""
        if os.path.isabs(cover):
            return cover
        return ""

    def resolve_playlist_logo(self, source_path: str) -> str:
        if not source_path or not os.path.isfile(source_path):
            return ""
        extension = os.path.splitext(source_path)[1].lower()
        if extension not in (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif", ".svg"):
            return ""
        return os.path.abspath(source_path)

    def change_playlist_logo(self, playlist_name: Optional[str] = None) -> None:
        name = playlist_name or self.current_playlist_name
        if not name or name not in self.playlists:
            return
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Choose Playlist Logo",
            "",
            "Images (*.png *.jpg *.jpeg *.bmp *.webp *.gif *.svg)",
            options=QFileDialog.Option.DontUseNativeDialog,
        )
        if not path:
            return
        pixmap = QPixmap(path)
        if pixmap.isNull():
            QMessageBox.warning(self, "Invalid image", "The selected logo could not be loaded.")
            return
        cover = self.resolve_playlist_logo(path)
        if not cover:
            QMessageBox.warning(self, "Invalid image", "Choose a supported image file.")
            return
        self.sync_current_playlist()
        self.playlists[name]["cover"] = cover
        self.refresh_playlist_cards()
        self.save_playlists()

    def rename_playlist(self, playlist_name: str) -> None:
        if not playlist_name or playlist_name not in self.playlists:
            return

        new_name, accepted = QInputDialog.getText(
            self,
            "Rename Playlist",
            "New playlist name:",
            text=playlist_name,
        )
        new_name = new_name.strip()
        if not accepted or not new_name or new_name == playlist_name:
            return
        if new_name in self.playlists:
            QMessageBox.warning(
                self,
                "Playlist exists",
                "Choose a unique playlist name.",
            )
            return

        self.sync_current_playlist()
        renamed: dict = {}
        for name, data in self.playlists.items():
            renamed[new_name if name == playlist_name else name] = data
        self.playlists = renamed

        if self.current_playlist_name == playlist_name:
            self.current_playlist_name = new_name
            self.current_playlist_label.setText(new_name)
        if self.active_playlist_name == playlist_name:
            self.active_playlist_name = new_name

        self.refresh_playlist_cards()
        self.save_playlists()

    def new_playlist(self) -> None:
        dialog = PlaylistDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name, image_path = dialog.values()
        if not name:
            return
        if name in self.playlists:
            QMessageBox.warning(self, "Playlist exists", "Choose a unique playlist name.")
            return
        self.sync_current_playlist()
        cover = self.resolve_playlist_logo(image_path) if image_path else ""
        self.playlists[name] = {
            "sources": [],
            "labels": [],
            "stream_buffer_ms": [],
            "cover": cover,
        }
        self.stop()
        self.current_playlist_name = name
        self.current_playlist_label.setText(name)
        self.playlist = []
        self.playlist_labels = []
        self.stream_buffer_ms_by_item = []
        self.current_index = -1
        self.playlist_widget.clear()
        self.reset_labels()
        self.refresh_playlist_cards()
        self.save_playlists()

    def delete_playlist(self, playlist_name: Optional[str] = None) -> None:
        name = playlist_name or self.current_playlist_name
        if not name or name not in self.playlists:
            return
        if len(self.playlists) <= 1:
            QMessageBox.warning(self, "Cannot delete", "Keep at least one playlist.")
            return
        answer = QMessageBox.question(
            self,
            "Delete playlist",
            f"Delete '{name}'?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        del self.playlists[name]
        if name == self.current_playlist_name:
            self.stop()
            self.current_playlist_name = ""
            replacement = next(iter(self.playlists), "")
            if replacement:
                self.switch_playlist(replacement)
            else:
                self.current_playlist_label.setText("")
                self.playlist = []
                self.playlist_labels = []
                self.playlist_widget.clear()
                self.reset_labels()
        self.refresh_playlist_cards()
        self.save_playlists()

    def add_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Open Audio Files",
            "",
            "Audio files (*.mp3 *.wav *.ogg *.flac *.m4a *.aac);;All files (*.*)",
            options=QFileDialog.Option.DontUseNativeDialog,
        )
        for path in files:
            self.append_track(path)

    def audio_files_from_mime_data(self, mime_data: QMimeData) -> list[str]:
        if not mime_data.hasUrls():
            return []
        paths = []
        for url in mime_data.urls():
            if not url.isLocalFile():
                continue
            path = url.toLocalFile()
            if path.lower().endswith(SUPPORTED_EXTENSIONS) and os.path.isfile(path):
                paths.append(path)
        return paths

    def add_dropped_files(self, paths: list[str]) -> None:
        for path in paths:
            self.append_track(path)

    def add_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self,
            "Open Audio Folder",
            "",
            QFileDialog.Option.DontUseNativeDialog,
        )
        if not folder:
            return
        for root, _, filenames in os.walk(folder):
            for filename in sorted(filenames):
                if filename.lower().endswith(SUPPORTED_EXTENSIONS):
                    self.append_track(os.path.join(root, filename))

    def add_radio(self) -> None:
        dialog = RadioDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        url, name = dialog.values()
        if not url:
            return
        if not url.startswith(("http://", "https://")):
            QMessageBox.warning(self, "Invalid URL", "Use an HTTP or HTTPS URL.")
            return
        self.playlist.append(url)
        label = f"[Radio] {name or url}"
        self.playlist_labels.append(label)
        item = QListWidgetItem(label)
        item.setToolTip(url)
        item.setForeground(QColor("#ff8fa0"))
        self.playlist_widget.addItem(item)
        self.sync_current_playlist()
        self.refresh_playlist_cards()
        self.save_playlists()

    def append_track(self, path: str) -> None:
        if path in self.playlist:
            return
        self.playlist.append(path)
        label = self.metadata_title(path)
        self.playlist_labels.append(label)
        item = QListWidgetItem(label)
        item.setToolTip(path)
        item.setForeground(QColor("#b9dcf5"))
        item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.playlist_widget.addItem(item)
        self.sync_current_playlist()
        self.refresh_playlist_cards()
        self.save_playlists()

    def toggle_playlist_search(self) -> None:
        if self.search_field.isHidden():
            self.search_field.show()
            self.search_field.setFocus()
            self.search_field.selectAll()
            self.set_playlist_search_active(True)
        else:
            self.search_field.clear()
            self.search_field.hide()
            self.set_playlist_search_active(False)

    def set_playlist_search_active(self, active: bool) -> None:
        self.search_button.setProperty("active", active)
        self.search_button.style().unpolish(self.search_button)
        self.search_button.style().polish(self.search_button)

    def search_playlist(self, text: str) -> None:
        normalize = lambda value: "".join(
            character
            for character in unicodedata.normalize("NFKC", value).casefold()
            if unicodedata.category(character) != "Mn" and character != "ـ"
        )
        query_terms = [
            "".join(character for character in term if character.isalnum())
            for term in normalize(text).split()
        ]
        query_terms = [term for term in query_terms if term]
        if not query_terms:
            for row in range(self.playlist_widget.count()):
                item = self.playlist_widget.item(row)
                if item is not None:
                    item.setHidden(False)
            self.playlist_widget.clearSelection()
            return

        matches = []
        for row, item in enumerate(
            (self.playlist_widget.item(index) for index in range(self.playlist_widget.count()))
        ):
            if item is None:
                continue
            source = self.playlist[row] if row < len(self.playlist) else ""
            fields = [item.text(), source, os.path.basename(source)]
            if source and not source.startswith(("http://", "https://")):
                metadata, _ = self.audio_metadata(source)
                fields.extend(metadata.values())

            normalized_fields = [normalize(field) for field in fields if field]
            words = {
                "".join(character for character in word if character.isalnum())
                for field in normalized_fields
                for word in field.split()
            }
            words.discard("")
            matched = all(
                any(term in field for field in normalized_fields)
                or (
                    len(term) >= 4
                    and bool(get_close_matches(term, words, n=1, cutoff=0.78))
                )
                for term in query_terms
            )
            item.setHidden(not matched)
            if matched:
                matches.append(item)

        if matches:
            self.playlist_widget.setCurrentItem(matches[0])
            self.playlist_widget.scrollToItem(matches[0])
        else:
            self.playlist_widget.clearSelection()

    def remove_selected(self) -> None:
        if self._selection_mode:
            self.delete_selected_tracks()
            return
        rows = self._selected_playlist_rows()
        if not rows:
            return
        self.remove_playlist_rows(rows)

    def remove_playlist_rows(self, rows: list[int]) -> None:
        rows = sorted(
            {row for row in rows if 0 <= row < len(self.playlist)},
            reverse=True,
        )
        if not rows:
            return
        source_was_selected = self.current_index in rows
        if source_was_selected:
            self.stop()
            self.current_index = -1
        for row in rows:
            self.playlist.pop(row)
            if row < len(self.playlist_labels):
                self.playlist_labels.pop(row)
            if row < len(self.stream_buffer_ms_by_item):
                self.stream_buffer_ms_by_item.pop(row)
            self.playlist_widget.takeItem(row)
        if source_was_selected:
            self.reset_labels()
        else:
            self.current_index -= sum(row < self.current_index for row in rows)
        if not self.playlist:
            self.reset_labels()
        self.sync_current_playlist()
        self.refresh_playlist_cards()
        self.save_playlists()

    def reset_labels(self) -> None:
        self.track_title.setText("No track loaded")
        self.track_details.setText("Add audio files to start listening")
        self.elapsed_label.setText("00:00")
        self.duration_label.setText("00:00")
        self.position_slider.setValue(0)

    def play_selected(self, item: QListWidgetItem) -> None:
        if self._selection_mode:
            return
        self.playlist_navigation_timer.stop()
        row = self.playlist_widget.row(item)
        if 0 <= row < len(self.playlist):
            self.load_track(row)
            self.play()

    def show_track_context_menu(self, position) -> None:
        item = self.playlist_widget.itemAt(position)
        if item is None:
            return
        row = self.playlist_widget.row(item)
        if not 0 <= row < len(self.playlist):
            return

        source = self.playlist[row]
        if self._selection_mode:
            if not item.isSelected():
                self.playlist_widget.clearSelection()
                item.setSelected(True)
            rows = self._selected_playlist_rows()
            menu = QMenu(self)
            summary_action = menu.addAction(f"{len(rows)} selected")
            summary_action.setEnabled(False)
            select_all_action = menu.addAction("Select all visible items")
            clear_action = menu.addAction("Clear selection")
            move_menu = menu.addMenu("Move selected to")
            move_actions = {
                move_menu.addAction(name): name
                for name in self.playlists
                if name != self.current_playlist_name
            }
            move_menu.setEnabled(bool(rows and move_actions))
            delete_action = menu.addAction("Delete selected items")
            delete_action.setEnabled(bool(rows))
            menu.addSeparator()
            customize_action = None
            if source.startswith(("http://", "https://")):
                customize_action = menu.addAction("Customize radio stream")
            exit_action = menu.addAction("Exit selection mode")
            selected_action = menu.exec(
                self.playlist_widget.viewport().mapToGlobal(position)
            )
            if selected_action == select_all_action:
                self.select_all_visible_tracks()
            elif selected_action == clear_action:
                self.playlist_widget.clearSelection()
            elif selected_action in move_actions:
                self.move_selected_tracks(move_actions[selected_action])
            elif selected_action == delete_action:
                self.delete_selected_tracks()
            elif customize_action is not None and selected_action == customize_action:
                self.customize_radio_stream(row)
            elif selected_action == exit_action:
                self.set_selection_mode(False)
            return

        menu = QMenu(self)
        select_action = menu.addAction("Select multiple items")
        menu.addSeparator()
        delete_action = menu.addAction("Delete")

        add_menu = menu.addMenu("Add to playlist")
        other_playlist_actions = {}
        for playlist_name in self.playlists:
            if playlist_name == self.current_playlist_name:
                continue
            action = add_menu.addAction(playlist_name)
            other_playlist_actions[action] = playlist_name

        customize_action = None
        if source.startswith(("http://", "https://")):
            customize_action = menu.addAction("Customize radio stream")

        selected_action = menu.exec(
            self.playlist_widget.viewport().mapToGlobal(position)
        )
        if selected_action == select_action:
            self.set_selection_mode(True, row)
        elif selected_action == delete_action:
            self.remove_track_row(row)
        elif customize_action is not None and selected_action == customize_action:
            self.customize_radio_stream(row)
        elif selected_action in other_playlist_actions:
            self.add_track_to_playlist(
                row,
                other_playlist_actions[selected_action],
            )

    def remove_track_row(self, row: int) -> None:
        self.remove_playlist_rows([row])

    def add_track_to_playlist(self, row: int, destination: str) -> None:
        if not 0 <= row < len(self.playlist):
            return
        if destination not in self.playlists:
            return

        source = self.playlist[row]
        label = (
            self.playlist_labels[row]
            if row < len(self.playlist_labels)
            else ""
        )
        if not label:
            label = (
                source
                if source.startswith(("http://", "https://"))
                else self.metadata_title(source)
            )

        target = self.playlists[destination]
        target_sources = list(target.get("sources", []))
        target_labels = list(target.get("labels", []))
        target_buffers = list(target.get("stream_buffer_ms", []))
        while len(target_labels) < len(target_sources):
            target_labels.append("")
        while len(target_buffers) < len(target_sources):
            target_buffers.append(3000)
        if source in target_sources:
            QMessageBox.information(
                self,
                "Already added",
                f"This item already exists in '{destination}'.",
            )
            return
        target_sources.append(source)
        target_labels.append(label)
        item_buffer_ms = (
            self.stream_buffer_ms_by_item[row]
            if row < len(self.stream_buffer_ms_by_item)
            else 3000
        )
        target_buffers.append(max(500, min(10000, item_buffer_ms)))
        target["sources"] = target_sources
        target["labels"] = target_labels
        target["stream_buffer_ms"] = target_buffers
        self.save_playlists()

    def customize_radio_stream(self, row: int) -> None:
        if not 0 <= row < len(self.playlist):
            return
        old_url = self.playlist[row]
        if not old_url.startswith(("http://", "https://")):
            return
        old_label = (
            self.playlist_labels[row]
            if row < len(self.playlist_labels)
            else ""
        )
        old_buffer_ms = (
            self.stream_buffer_ms_by_item[row]
            if row < len(self.stream_buffer_ms_by_item)
            else 3000
        )
        dialog = StreamCustomizeDialog(
            old_url,
            old_label,
            self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        new_name, new_url = dialog.values()
        if not new_url.startswith(("http://", "https://")):
            QMessageBox.warning(
                self,
                "Invalid URL",
                "Use an HTTP or HTTPS stream URL.",
            )
            return
        if not new_name:
            new_name = new_url
        while len(self.playlist_labels) < len(self.playlist):
            self.playlist_labels.append("")
        while len(self.stream_buffer_ms_by_item) < len(self.playlist):
            self.stream_buffer_ms_by_item.append(3000)
        self.playlist[row] = new_url
        self.playlist_labels[row] = f"[Radio] {new_name}"
        self.playlist_widget.item(row).setText(f"[Radio] {new_name}")
        self.playlist_widget.item(row).setToolTip(new_url)
        if self.current_index == row:
            self.track_title.setText(new_name)
            self.track_details.setText(new_url)
            if old_url != new_url:
                started = self.start_stream(new_url, old_buffer_ms)
                self.set_play_button_state(started)
        self.sync_current_playlist()
        self.save_playlists()

    def load_track(self, index: int) -> None:
        if not 0 <= index < len(self.playlist):
            return
        if index != self.current_index:
            self.stop_stream()
            self.is_paused = False
            self.is_playing = False
        self.current_index = index
        source = self.playlist[index]
        self.active_playback_source = source
        self.active_playlist_name = self.current_playlist_name
        self.playlist_widget.setCurrentRow(index)
        self.paused_position = 0.0
        self.playback_started_at = 0.0
        self.is_paused = False
        self.position_slider.setValue(0)
        self.elapsed_label.setText("00:00")

        if source.startswith(("http://", "https://")):
            self.position_slider.setTimelineWheelStep(500)
            delay_ms = (
                self.stream_buffer_ms_by_item[index]
                if index < len(self.stream_buffer_ms_by_item)
                else self.stream_buffer_ms
            )
            delay_ms = max(0, min(30000, int(delay_ms)))
            item = self.playlist_widget.item(index)
            self.track_title.setText(
                item.text().removeprefix("[Radio] ")
                if item is not None else "Radio"
            )
            self.track_details.setText(source)
            self.position_slider.blockSignals(True)
            self.position_slider.setRange(0, 30000)
            self.position_slider.setSingleStep(500)
            self.position_slider.setPageStep(1000)
            self.position_slider.blockSignals(False)
            self.elapsed_label.setText("LIVE" if delay_ms == 0 else "DELAYED")
            self.duration_label.setText(
                "LIVE NOW" if delay_ms == 0 else f"{delay_ms / 1000.0:.1f}s"
            )
            self.stream_buffer_ms = delay_ms
        else:
            self.position_slider.setTimelineWheelStep(0)
            self.track_title.setText(self.metadata_title(source))
            self.track_details.setText(self.metadata_details(source))
            self.position_slider.blockSignals(True)
            self.position_slider.setRange(0, 1000)
            self.position_slider.blockSignals(False)

        duration = self.get_duration()
        if not source.startswith(("http://", "https://")):
            self.duration_label.setText(self.format_time(duration))

    def audio_metadata(self, path: str) -> tuple[dict[str, str], float]:
        cache_key = os.path.abspath(path)
        cached = self.audio_metadata_cache.get(cache_key)
        if cached is not None:
            return cached

        metadata: dict[str, str] = {}
        duration = 0.0
        try:
            with av.open(path, mode="r") as container:
                metadata = {
                    key.casefold(): str(value)
                    for key, value in container.metadata.items()
                }
                if container.duration:
                    duration = max(0.0, container.duration / av.time_base)
                else:
                    audio_stream = next(
                        (stream for stream in container.streams if stream.type == "audio"),
                        None,
                    )
                    if audio_stream is not None and audio_stream.duration:
                        duration = max(
                            0.0,
                            float(audio_stream.duration * audio_stream.time_base),
                        )
        except (OSError, av.error.FFmpegError):
            pass

        result = (metadata, duration)
        self.audio_metadata_cache[cache_key] = result
        return result

    def metadata_title(self, path: str) -> str:
        metadata, _ = self.audio_metadata(path)
        title = metadata.get("title", "").strip()
        artist = metadata.get("artist", "").strip()
        if title and artist:
            return f"{artist} - {title}"
        if title:
            return title
        return os.path.splitext(os.path.basename(path))[0]

    def metadata_details(self, path: str) -> str:
        metadata, duration = self.audio_metadata(path)
        artist = metadata.get("artist", "Unknown Artist")
        album = metadata.get("album", "Unknown Album")
        return f"{artist}  |  {album}  |  {self.format_time(duration)}"

    def get_duration(self) -> float:
        source = self.active_playback_source
        if source is None:
            return 0.0
        if source.startswith(("http://", "https://")):
            return 0.0
        _, duration = self.audio_metadata(source)
        return duration

    def start_stream(self, url: str, buffer_ms: Optional[int] = None) -> bool:
        if not self.internet_enabled:
            return False
        if buffer_ms is not None:
            self.stream_buffer_ms = max(0, min(30000, int(buffer_ms)))
        self.stop_stream()
        self.active_playback_source = url
        self.stream_jitter_buffer.clear()
        self.stream_buffer_ready = False
        self.stream_history.clear()
        self.stream_rewind_mode = False
        self.stream_rewind_buffer.clear()
        self.stream_rewind_cursor_ms = 0

        try:
            self.stream_decoder = StreamDecoder(url, is_stream=True)
            self.stream_decoder.set_audio_settings(
                self.equalizer_values,
                self.effects_values,
                self.audio_processing_enabled,
            )
            self.stream_decoder.start()
        except Exception as exc:
            self.stream_decoder = None
            self.is_playing = False
            self.is_paused = False
            self.track_details.setText("Stream load failed")
            print(f"Radio stream decoder startup failed: {exc}")
            return False

        self.is_playing = True
        self.is_paused = False
        return True

    def start_file_audio(self, source: str, position: float = 0.0) -> None:
        self.stop_stream()
        self.active_playback_source = source
        self.stream_buffer_ms = 0
        self.stream_history.clear()
        self.stream_rewind_mode = False
        self.stream_rewind_buffer.clear()
        self.stream_jitter_buffer.clear()
        self.stream_buffer_ready = True
        try:
            self.stream_decoder = StreamDecoder(
                source,
                is_stream=False,
                start_position=position,
            )
            self.stream_decoder.set_audio_settings(
                self.equalizer_values,
                self.effects_values,
                self.audio_processing_enabled,
            )
            self.stream_decoder.start()
        except Exception as exc:
            self.stream_decoder = None
            self.is_playing = False
            self.is_paused = False
            raise RuntimeError(f"Could not start audio decoder: {exc}") from exc
        self.paused_position = max(0.0, position)
        self.is_playing = True
        self.is_paused = False

    def stop_stream(self) -> None:
        decoder = self.stream_decoder
        self.stream_decoder = None
        if decoder is not None:
            decoder.stop()
            while True:
                try:
                    decoder.audio_queue.get_nowait()
                except queue.Empty:
                    break
        self.audio_sink.stop()
        self.audio_output_device = None
        self.stream_jitter_buffer.clear()
        self.stream_buffer_ready = False
        self.stream_history.clear()
        self.stream_rewind_mode = False
        self.stream_rewind_buffer.clear()
        self.stream_rewind_cursor_ms = 0

    def write_audio_output(self, chunk: bytes | bytearray) -> int:
        if not chunk or self.audio_sink.state() == QAudio.State.SuspendedState:
            return 0
        if (
            self.audio_output_device is None
            or self.audio_sink.state() == QAudio.State.StoppedState
        ):
            self.audio_output_device = self.audio_sink.start()
        if self.audio_output_device is None:
            raise RuntimeError("The audio output device could not be started.")
        if self.audio_sink.error() != QAudio.Error.NoError:
            raise RuntimeError(f"Audio output error: {self.audio_sink.error().name}")

        available = min(len(chunk), self.audio_sink.bytesFree())
        available -= available % AUDIO_BYTES_PER_FRAME
        if available <= 0:
            return 0
        written = self.audio_output_device.write(chunk[:available])
        if written < 0:
            raise RuntimeError(self.audio_output_device.errorString())
        if written % AUDIO_BYTES_PER_FRAME:
            raise RuntimeError("The audio device wrote an incomplete sample frame.")
        recorder = self.audio_recorder
        if recorder is not None:
            recorder.enqueue(
                bytes(chunk[:written]),
                self.volume_slider.value() / 100.0,
            )
        return written

    def report_audio_output_error(self, error: Exception) -> None:
        self.track_details.setText(f"Audio output error: {error}")
        self.is_playing = False
        self.is_paused = False
        self.set_play_button_state(False)
        self.stop_stream()

    def feed_stream_audio(self) -> None:
        if (
            self.stream_decoder is None
            or self.is_paused
            or not self.is_playing
        ):
            return

        is_local_file = not self.stream_decoder.is_stream
        stream_chunk_size = 32768 if is_local_file else AUDIO_SAMPLE_RATE * AUDIO_BYTES_PER_FRAME // 2
        prebuffer_ms = self.stream_buffer_ms if not is_local_file else 250
        prebuffer_bytes = AUDIO_SAMPLE_RATE * AUDIO_BYTES_PER_FRAME * prebuffer_ms // 1000
        jitter_limit_bytes = prebuffer_bytes + stream_chunk_size
        bytes_to_read = (
            stream_chunk_size
            if self.stream_rewind_mode
            else max(0, jitter_limit_bytes - len(self.stream_jitter_buffer))
        )

        while bytes_to_read > 0:
            try:
                chunk = self.stream_decoder.audio_queue.get_nowait()
                if self.stream_decoder.is_stream:
                    self.stream_history.extend(chunk)
                if not self.stream_rewind_mode:
                    self.stream_jitter_buffer.extend(chunk)
                bytes_to_read -= len(chunk)
            except queue.Empty:
                break

        if self.stream_decoder.is_stream:
            history_limit = (
                AUDIO_SAMPLE_RATE
                * AUDIO_BYTES_PER_FRAME
                * self.stream_history_max_ms
                // 1000
            )
            if len(self.stream_history) > history_limit:
                del self.stream_history[:len(self.stream_history) - history_limit]

        if self.stream_rewind_mode:
            if not self.stream_rewind_buffer:
                return
            try:
                written = self.write_audio_output(self.stream_rewind_buffer)
                if written:
                    del self.stream_rewind_buffer[:written]
                    self.stream_rewind_cursor_ms += (
                        written * 1000
                        // (AUDIO_SAMPLE_RATE * AUDIO_BYTES_PER_FRAME)
                    )
                cache_ms = min(
                    self.stream_history_max_ms,
                    len(self.stream_history)
                    * 1000
                    // (AUDIO_SAMPLE_RATE * AUDIO_BYTES_PER_FRAME),
                )
                if self.stream_rewind_cursor_ms >= cache_ms:
                    self.stream_rewind_mode = False
                    self.stream_rewind_buffer.clear()
                    self.stream_buffer_ready = True
                    self.stream_jitter_buffer.clear()
            except Exception as exc:
                print(f"Stream rewind output error: {exc}")
                self.report_audio_output_error(exc)
            return

        if not self.stream_buffer_ready:
            if (
                len(self.stream_jitter_buffer) < prebuffer_bytes
                and not (is_local_file and self.stream_decoder.finished)
            ):
                return
            self.stream_buffer_ready = True

        if not self.stream_jitter_buffer:
            return

        try:
            written = self.write_audio_output(self.stream_jitter_buffer)
            if written:
                del self.stream_jitter_buffer[:written]
        except Exception as exc:
            print(f"Stream audio output error: {exc}")
            self.report_audio_output_error(exc)

    def play(self) -> None:
        if self.is_paused and self.active_playback_source is not None:
            self.audio_sink.resume()
            self.is_paused = False
            self.is_playing = True
            self.playback_started_at = time.monotonic()
            self.set_play_button_state(True)
            return
        if not self.playlist:
            return
        if self.current_index == -1:
            self.load_track(0)
        elif self.active_playback_source is None:
            self.load_track(self.current_index)
        source = self.active_playback_source
        if source is None:
            return
        if source.startswith(("http://", "https://")) and not self.internet_enabled:
            self.stop_stream()
            self.is_paused = False
            self.is_playing = False
            self.track_details.setText("Internet access is disabled")
            self.set_play_button_state(False)
            return
        try:
            if source.startswith(("http://", "https://")):
                buffer_ms = (
                    self.stream_buffer_ms_by_item[self.current_index]
                    if self.current_index < len(self.stream_buffer_ms_by_item)
                    else 3000
                )
                if not self.start_stream(source, buffer_ms):
                    self.set_play_button_state(False)
                    return
            else:
                self.start_file_audio(source)
            self.paused_position = 0.0
            self.playback_started_at = time.monotonic()
            self.is_playing = True
            self.is_paused = False
            self.set_play_button_state(True)
        except Exception as exc:
            self.is_playing = False
            self.is_paused = False
            self.set_play_button_state(False)
            QMessageBox.critical(
                self,
                "Playback Error",
                f"Could not play this track:\n{source}\n\n{exc}",
            )

    def pause(self) -> None:
        if self.is_playing and not self.is_paused:
            self.paused_position = self.current_position()
            self.audio_sink.suspend()
            self.is_paused = True
            self.set_play_button_state(False)

    def stop(self) -> None:
        self.stop_stream()
        self.is_paused = False
        self.is_playing = False
        self.active_playback_source = None
        self.active_playlist_name = ""
        self.set_play_button_state(False)
        self.paused_position = 0.0
        self.position_slider.setValue(0)
        self.elapsed_label.setText("00:00")

    def toggle_play_pause(self) -> None:
        if self.is_playing and not self.is_paused:
            self.pause()
        else:
            self.play()

    def set_play_button_state(self, playing: bool) -> None:
        if playing and not self.is_paused:
            self.play_button.setIcon(self.svg_icon("pause.svg"))
            self.play_button.setToolTip("Pause")
        else:
            self.play_button.setIcon(self.svg_icon("play.svg"))
            self.play_button.setToolTip("Play")

    def next_track(self) -> None:
        if not self.playlist:
            return
        if self.shuffle_enabled and len(self.playlist) > 1:
            choices = list(range(len(self.playlist)))
            choices.remove(max(0, self.current_index))
            index = random.choice(choices)
        else:
            index = (self.current_index + 1) % len(self.playlist)
        self.load_track(index)
        self.play()

    def previous_track(self) -> None:
        if not self.playlist:
            return
        if self.current_position() > 5:
            self.seek_to(0.0)
            return
        index = (self.current_index - 1) % len(self.playlist)
        self.load_track(index)
        self.play()

    def toggle_shuffle(self) -> None:
        self.shuffle_enabled = not self.shuffle_enabled
        self.refresh_toggle(self.shuffle_button)

    def toggle_repeat(self) -> None:
        self.repeat_enabled = not self.repeat_enabled
        self.refresh_toggle(self.repeat_button)

    def toggle_internet_access(self) -> None:
        self.internet_enabled = not self.internet_enabled
        state = "on" if self.internet_enabled else "off"
        for button in (self.internet_button, self.mini_player.internet_button):
            button.setChecked(self.internet_enabled)
            button.setIcon(self.svg_icon("internet.svg"))
            button.setProperty("active", self.internet_enabled)
            button.style().unpolish(button)
            button.style().polish(button)
            button.setToolTip(f"Internet access: {state}")

        if not self.internet_enabled and self.active_playback_source is not None:
            source = self.active_playback_source
            if source.startswith(("http://", "https://")):
                self.stop_stream()
                self.is_paused = False
                self.is_playing = False
                self.track_details.setText("Internet access is disabled")
                self.set_play_button_state(False)

    def refresh_toggle(self, button: QToolButton) -> None:
        active = (
            button is self.shuffle_button and self.shuffle_enabled
        ) or (
            button is self.repeat_button and self.repeat_enabled
        )
        button.setProperty("active", active)
        button.setToolTip(
            ("Shuffle enabled" if self.shuffle_enabled else "Shuffle")
            if button is self.shuffle_button
            else ("Repeat enabled" if self.repeat_enabled else "Repeat")
        )
        button.style().unpolish(button)
        button.style().polish(button)

    def open_audio_settings_dialog(self) -> None:
        previous_equalizer = dict(self.equalizer_values)
        previous_effects = dict(self.effects_values)
        previous_enabled = self.audio_processing_enabled
        dialog = AudioSettingsDialog(
            previous_equalizer,
            previous_effects,
            previous_enabled,
            self,
        )
        self.audio_settings_dialog = dialog
        dialog.settingsChanged.connect(self.preview_audio_settings)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        if accepted:
            self.equalizer_values = dialog.equalizer.values()
            self.effects_values = dialog.effects.values()
            self.audio_processing_enabled = (
                dialog.processing_enabled_checkbox.isChecked()
            )
            self.save_audio_settings()
        else:
            self.equalizer_values = previous_equalizer
            self.effects_values = previous_effects
            self.audio_processing_enabled = previous_enabled
        self.audio_settings_dialog = None
        self.apply_equalizer_settings()
        self.apply_audio_settings()

    def preview_audio_settings(
        self,
        equalizer_values: dict[str, int],
        effects_values: dict[str, int],
        processing_enabled: bool,
    ) -> None:
        self.equalizer_values = dict(equalizer_values)
        self.effects_values = dict(effects_values)
        self.audio_processing_enabled = processing_enabled
        self.apply_audio_settings()
        self.apply_equalizer_settings()

    def apply_equalizer_settings(self) -> None:
        self.audio_sink.setVolume(self.volume_slider.value() / 100.0)

    def apply_effects_settings(self) -> None:
        self.apply_audio_settings()

    def apply_audio_settings(self) -> None:
        if self.stream_decoder is not None:
            self.stream_decoder.set_audio_settings(
                self.equalizer_values,
                self.effects_values,
                self.audio_processing_enabled,
            )

    def set_volume(self, value: int) -> None:
        value = max(0, min(100, int(value)))
        if self.volume_slider.value() != value:
            self.volume_slider.blockSignals(True)
            self.volume_slider.setValue(value)
            self.volume_slider.blockSignals(False)
        self.volume_label.setText(f"{value}%")
        self.apply_equalizer_settings()

    def update_record_button_tooltip(self) -> None:
        label = RECORDING_FORMATS[self.recording_format][0]
        action = "Stop recording" if self.audio_recorder is not None else "Start recording"
        self.record_button.setToolTip(f"{action} ({label})")

    def show_recording_format_menu(self, position) -> None:
        menu = QMenu(self)
        for recording_format, (label, _) in RECORDING_FORMATS.items():
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(recording_format == self.recording_format)
            action.setData(recording_format)
        menu.addSeparator()
        folder_action = menu.addAction(f"Recording folder: {self.recording_directory}")
        folder_action.setEnabled(False)
        folder_action.setToolTip(self.recording_directory)
        choose_directory_action = menu.addAction("Choose recording folder...")
        selected_action = menu.exec(
            self.record_button.mapToGlobal(position)
        )
        if selected_action is None:
            return
        if selected_action is choose_directory_action:
            directory = QFileDialog.getExistingDirectory(
                self,
                "Choose Recording Folder",
                self.recording_directory,
                QFileDialog.Option.DontUseNativeDialog,
            )
            if directory:
                self.recording_directory = os.path.abspath(directory)
                self.save_audio_settings()
            return
        recording_format = selected_action.data()
        if recording_format not in RECORDING_FORMATS:
            return
        self.recording_format = recording_format
        self.save_audio_settings()
        self.update_record_button_tooltip()

    def toggle_recording(self) -> None:
        if self.audio_recorder is not None:
            self.stop_recording()
            return

        self.recording_hide_timer.stop()
        try:
            os.makedirs(self.recording_directory, exist_ok=True)
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            milliseconds = int(time.time() * 1000) % 1000
            extension = RECORDING_FORMATS[self.recording_format][1]
            filename = f"SRPlayer_{timestamp}_{milliseconds:03d}{extension}"
            output_path = os.path.join(self.recording_directory, filename)
            self.audio_recorder = AudioRecorder(
                output_path,
                self.recording_format,
            )
        except Exception as exc:
            QMessageBox.warning(self, "Recording failed", str(exc))
            return

        self.recording_saved_path = ""
        self.update_record_button_tooltip()
        self.record_button.setProperty("recording", True)
        self.record_button.style().unpolish(self.record_button)
        self.record_button.style().polish(self.record_button)
        self.recording_status_label.setText("Recording")
        self.recording_status_label.setProperty("recording", True)
        self.recording_status_label.style().unpolish(self.recording_status_label)
        self.recording_status_label.style().polish(self.recording_status_label)
        self.recording_timer_label.setText("00:00:00")
        self.recording_timer_label.show()
        self.recording_pause_button.setIcon(self.svg_icon("pause.svg"))
        self.recording_pause_button.setToolTip("Pause recording")
        self.recording_pause_button.show()

    def toggle_recording_pause(self) -> None:
        recorder = self.audio_recorder
        if recorder is None or not recorder.is_recording:
            return
        recorder.is_paused = not recorder.is_paused
        if recorder.is_paused:
            self.recording_status_label.setText("Recording paused")
            self.recording_pause_button.setIcon(self.svg_icon("play.svg"))
            self.recording_pause_button.setToolTip("Resume recording")
        else:
            self.recording_status_label.setText("Recording")
            self.recording_pause_button.setIcon(self.svg_icon("pause.svg"))
            self.recording_pause_button.setToolTip("Pause recording")

    def stop_recording(self) -> None:
        recorder = self.audio_recorder
        if recorder is None:
            return
        self.audio_recorder = None
        recorder.stop()
        self.recording_saved_path = recorder.output_path
        self.update_record_button_tooltip()
        self.record_button.setProperty("recording", False)
        self.record_button.style().unpolish(self.record_button)
        self.record_button.style().polish(self.record_button)
        self.recording_status_label.setProperty("recording", False)
        self.recording_status_label.style().unpolish(self.recording_status_label)
        self.recording_status_label.style().polish(self.recording_status_label)
        self.recording_pause_button.hide()
        self.recording_timer_label.setText(
            self.format_recording_time(recorder.duration_seconds)
        )
        if recorder.error:
            self.recording_status_label.setText(
                f"Recording error: {recorder.error}"
            )
        else:
            try:
                relative_path = os.path.relpath(
                    self.recording_saved_path,
                    BASE_DIR,
                )
            except ValueError:
                relative_path = self.recording_saved_path
            self.recording_status_label.setText(f"Saved: {relative_path}")
        self.recording_hide_timer.start(5000)

    def hide_recording_status(self) -> None:
        if self.audio_recorder is not None:
            return
        self.recording_status_label.clear()
        self.recording_timer_label.hide()

    def update_recording_status(self) -> None:
        recorder = self.audio_recorder
        if recorder is None:
            return
        if recorder.error:
            error = recorder.error
            self.stop_recording()
            self.recording_status_label.setText(f"Recording error: {error}")
            return
        self.recording_timer_label.setText(
            self.format_recording_time(recorder.duration_seconds)
        )

    @staticmethod
    def format_recording_time(seconds: float) -> str:
        total = max(0, int(seconds))
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    def current_position(self) -> float:
        source = self.active_playback_source
        if source is None:
            return 0.0
        if self.is_paused or not self.is_playing:
            return self.paused_position
        if source.startswith(("http://", "https://")):
            return 0.0
        elapsed = time.monotonic() - self.playback_started_at
        return max(0.0, self.paused_position + elapsed)

    def position_pressed(self) -> None:
        source = self.active_playback_source
        if source is not None and source.startswith(("http://", "https://")):
            self.dragging_position = True
            return
        self.dragging_position = True

    def seek_position(self) -> None:
        self.dragging_position = False
        source = self.active_playback_source
        if source is not None and source.startswith(("http://", "https://")):
            self.rewind_stream(self.position_slider.value())
            return
        duration = self.get_duration()
        if duration > 0:
            self.seek_to(duration * self.position_slider.value() / 1000.0)

    def timeline_wheel_changed(self, value: int) -> None:
        source = self.active_playback_source
        if source is None:
            return
        if source.startswith(("http://", "https://")):
            cache_ms = min(
                self.stream_history_max_ms,
                len(self.stream_history)
                * 1000
                // (AUDIO_SAMPLE_RATE * AUDIO_BYTES_PER_FRAME),
            )
            value = max(0, min(cache_ms, value))
            self.rewind_stream(value)
            return

        duration = self.get_duration()
        if duration > 0:
            self.seek_to(duration * value / self.position_slider.maximum())

    def timeline_value_changed(self, delay_ms: int) -> None:
        source = self.active_playback_source
        if source is None or not source.startswith(("http://", "https://")):
            return
        cache_ms = min(
            self.stream_history_max_ms,
            len(self.stream_history)
            * 1000
            // (AUDIO_SAMPLE_RATE * AUDIO_BYTES_PER_FRAME),
        )
        selected_ms = max(0, min(cache_ms, int(delay_ms)))
        if selected_ms >= max(0, cache_ms - 250):
            self.elapsed_label.setText("LIVE")
            self.duration_label.setText("LIVE NOW")
        else:
            behind_live = max(0, cache_ms - selected_ms)
            self.elapsed_label.setText(self.format_time(selected_ms / 1000.0))
            self.duration_label.setText(f"-{behind_live / 1000.0:.1f}s  LIVE")

    def rewind_stream(self, position_ms: int) -> None:
        cache_ms = min(
            self.stream_history_max_ms,
            len(self.stream_history)
            * 1000
            // (AUDIO_SAMPLE_RATE * AUDIO_BYTES_PER_FRAME),
        )
        position_ms = max(0, min(cache_ms, int(position_ms)))
        if cache_ms == 0:
            return
        if position_ms >= cache_ms - 250:
            self.stream_rewind_mode = False
            self.stream_rewind_buffer.clear()
            self.stream_buffer_ready = True
            self.stream_jitter_buffer.clear()
            self.audio_sink.stop()
            self.audio_output_device = None
            return

        byte_position = (
            AUDIO_SAMPLE_RATE
            * AUDIO_BYTES_PER_FRAME
            * position_ms
            // 1000
        )
        byte_position -= byte_position % AUDIO_BYTES_PER_FRAME
        byte_position = max(0, min(len(self.stream_history), byte_position))
        self.stream_rewind_mode = True
        self.stream_rewind_buffer = bytearray(self.stream_history[byte_position:])
        self.stream_rewind_cursor_ms = position_ms
        self.audio_sink.stop()
        self.audio_output_device = None

    def seek_to(self, seconds: float) -> None:
        try:
            seconds = max(0.0, seconds)
            source = self.active_playback_source
            if source is not None and not source.startswith(("http://", "https://")):
                self.start_file_audio(source, seconds)
            self.paused_position = seconds
            self.playback_started_at = time.monotonic()
        except Exception as exc:
            QMessageBox.warning(self, "Seek unavailable", str(exc))

    def update_progress(self) -> None:
        self.update_recording_status()
        self.mini_player.sync_from_player()
        source = self.active_playback_source
        if source is None or self.dragging_position:
            return
        decoder = self.stream_decoder
        latest_error = ""
        if decoder is not None:
            while True:
                try:
                    latest_error = decoder.error_queue.get_nowait()
                except queue.Empty:
                    break
        if self.active_playback_source is not None:
            source = self.active_playback_source
            if source.startswith(("http://", "https://")):
                if decoder is not None:
                    latest_metadata = ""
                    while True:
                        try:
                            latest_metadata = decoder.metadata_queue.get_nowait()
                        except queue.Empty:
                            break
                    if latest_metadata:
                        self.track_details.setText(latest_metadata)
                if latest_error:
                    self.track_details.setText(f"Stream reconnecting: {latest_error}")
                cache_ms = min(
                    self.stream_history_max_ms,
                    len(self.stream_history)
                    * 1000
                    // (AUDIO_SAMPLE_RATE * AUDIO_BYTES_PER_FRAME),
                )
                self.position_slider.blockSignals(True)
                self.position_slider.setRange(0, max(1000, cache_ms))
                if self.stream_rewind_mode:
                    position_ms = min(cache_ms, self.stream_rewind_cursor_ms)
                    self.elapsed_label.setText(self.format_time(position_ms / 1000.0))
                    behind_live = max(0, cache_ms - position_ms)
                    self.duration_label.setText(f"-{behind_live / 1000.0:.1f}s  LIVE")
                    self.position_slider.setValue(position_ms)
                else:
                    self.elapsed_label.setText("LIVE")
                    self.duration_label.setText("LIVE NOW")
                    self.position_slider.setValue(cache_ms)
                self.position_slider.blockSignals(False)
                return
        if latest_error and decoder is not None and not decoder.is_stream:
            self.track_details.setText(f"Playback failed: {latest_error}")
            self.is_playing = False
            self.set_play_button_state(False)
        duration = self.get_duration()
        if duration > 0:
            position = min(self.current_position(), duration)
            self.elapsed_label.setText(self.format_time(position))
            self.duration_label.setText(self.format_time(duration))
            self.position_slider.setValue(
                int(position / duration * 1000)
                if duration else 0
            )

        file_audio_finished = (
            decoder is not None
            and not decoder.is_stream
            and decoder.finished
            and decoder.audio_queue.empty()
            and not self.stream_jitter_buffer
            and (
                self.audio_sink.state() != QAudio.State.ActiveState
                or self.audio_sink.bytesFree() >= self.audio_sink.bufferSize()
            )
        )
        if (
            self.is_playing
            and not self.is_paused
            and file_audio_finished
        ):
            self.is_playing = False
            if self.repeat_enabled:
                self.play()
            else:
                self.next_track()

    @staticmethod
    def format_time(seconds: float) -> str:
        total = max(0, int(seconds))
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"

    def mousePressEvent(self, event) -> None:
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        super().mouseReleaseEvent(event)

    def is_titlebar_widget(self, widget: QObject) -> bool:
        while isinstance(widget, QWidget):
            if widget is self.titlebar:
                return True
            if widget is self:
                return False
            widget = widget.parentWidget()
        return False

    def is_titlebar_drag_target(self, widget: QObject) -> bool:
        while isinstance(widget, QWidget):
            if isinstance(widget, QAbstractButton):
                return False
            if widget is self.titlebar:
                return True
            if widget is self:
                return False
            widget = widget.parentWidget()
        return False

    def resize_edges_at(self, global_position) -> Qt.Edge:
        if not self.window_resizable or self.isMaximized():
            return Qt.Edge(0)

        frame = self.frameGeometry()
        x = global_position.x() - frame.left()
        y = global_position.y() - frame.top()
        border = 8
        left = 0 <= x < border
        right = frame.width() - border <= x < frame.width()
        top = 0 <= y < border
        bottom = frame.height() - border <= y < frame.height()
        if self.window_position_pinned:
            left = False
            top = False

        edges = Qt.Edge(0)
        if left:
            edges |= Qt.Edge.LeftEdge
        if right:
            edges |= Qt.Edge.RightEdge
        if top:
            edges |= Qt.Edge.TopEdge
        if bottom:
            edges |= Qt.Edge.BottomEdge
        return edges

    def set_resize_cursor(self, widget: QWidget, edges: Qt.Edge) -> None:
        horizontal = bool(edges & (Qt.Edge.LeftEdge | Qt.Edge.RightEdge))
        vertical = bool(edges & (Qt.Edge.TopEdge | Qt.Edge.BottomEdge))
        if horizontal and vertical:
            left = bool(edges & Qt.Edge.LeftEdge)
            top = bool(edges & Qt.Edge.TopEdge)
            shape = (
                Qt.CursorShape.SizeFDiagCursor
                if left == top
                else Qt.CursorShape.SizeBDiagCursor
            )
        elif horizontal:
            shape = Qt.CursorShape.SizeHorCursor
        elif vertical:
            shape = Qt.CursorShape.SizeVerCursor
        else:
            shape = Qt.CursorShape.ArrowCursor
        widget.setCursor(shape)

    def apply_window_resize(self, global_position) -> None:
        geometry = self._resize_start_geometry
        start = self._resize_start_position
        if geometry is None or start is None:
            return
        delta_x = global_position.x() - start.x()
        delta_y = global_position.y() - start.y()
        x, y = geometry.x(), geometry.y()
        width, height = geometry.width(), geometry.height()
        min_width = self.minimumWidth()
        min_height = self.minimumHeight()

        if self._resize_edges & Qt.Edge.LeftEdge:
            width = max(min_width, geometry.width() - delta_x)
            x = geometry.x() + geometry.width() - width
        elif self._resize_edges & Qt.Edge.RightEdge:
            width = max(min_width, geometry.width() + delta_x)

        if self._resize_edges & Qt.Edge.TopEdge:
            height = max(min_height, geometry.height() - delta_y)
            y = geometry.y() + geometry.height() - height
        elif self._resize_edges & Qt.Edge.BottomEdge:
            height = max(min_height, geometry.height() + delta_y)

        self.setGeometry(x, y, width, height)

    def show_window_context_menu(self, global_position) -> None:
        menu = QMenu(self)
        resize_action = menu.addAction("Enable window resizing")
        resize_action.setCheckable(True)
        resize_action.setChecked(self.window_resizable)
        pin_action = menu.addAction("Pin window position")
        pin_action.setCheckable(True)
        pin_action.setChecked(self.window_position_pinned)
        on_top_action = menu.addAction("Always on top")
        on_top_action.setCheckable(True)
        on_top_action.setChecked(self.window_stays_on_top)
        hide_main_action = menu.addAction("Hide main app from taskbar when minimized")
        hide_main_action.setCheckable(True)
        hide_main_action.setChecked(self.hide_main_window_on_minimize)
        menu.addSeparator()
        restore_action = menu.addAction("Restore default window")
        selected = menu.exec(global_position)
        if selected == resize_action:
            self.set_window_resizable(resize_action.isChecked())
        elif selected == pin_action:
            self.set_window_position_pinned(pin_action.isChecked())
        elif selected == on_top_action:
            self.set_window_stays_on_top(on_top_action.isChecked())
        elif selected == hide_main_action:
            self.set_hide_main_window_on_minimize(hide_main_action.isChecked())
        elif selected == restore_action:
            self.restore_default_window()

    def set_hide_main_window_on_minimize(self, enabled: bool) -> None:
        self.hide_main_window_on_minimize = bool(enabled)
        self.save_window_settings()

    def set_window_resizable(self, enabled: bool) -> None:
        self.window_resizable = bool(enabled)
        if not enabled:
            self._resize_edges = Qt.Edge(0)
        self.save_window_settings()

    def set_window_position_pinned(self, pinned: bool) -> None:
        self.window_position_pinned = bool(pinned)
        self._drag_position = None
        self.save_window_settings()

    def set_window_stays_on_top(self, enabled: bool) -> None:
        was_visible = self.isVisible()
        was_maximized = self.isMaximized()
        geometry = self.geometry()
        self.window_stays_on_top = bool(enabled)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, enabled)
        self.setGeometry(geometry)
        if was_visible:
            if was_maximized:
                self.showMaximized()
            else:
                self.show()
        self.save_window_settings()

    def center_default_window(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()
        self.move(
            available.center().x() - self.width() // 2,
            available.center().y() - self.height() // 2,
        )

    def restore_default_window(self) -> None:
        self.window_resizable = True
        self.window_position_pinned = False
        self.hide_main_window_on_minimize = False
        self.showNormal()
        self.set_window_stays_on_top(False)
        self.resize(self.default_window_size)
        self.center_default_window()
        self.maximize_button.setToolTip("Maximize")
        self.mini_player.restore_default_settings()
        self.save_window_settings()

    def save_window_settings(self) -> None:
        geometry = self.geometry()
        settings = {
            "resizable": self.window_resizable,
            "position_pinned": self.window_position_pinned,
            "always_on_top": self.window_stays_on_top,
            "hide_main_window_on_minimize": self.hide_main_window_on_minimize,
            "geometry": [geometry.x(), geometry.y(), geometry.width(), geometry.height()],
            "mini_player": (
                self.mini_player.settings_payload()
                if hasattr(self, "mini_player")
                else self._mini_player_settings
            ),
        }
        try:
            os.makedirs(SAVE_DIR, exist_ok=True)
            with open(self.window_settings_file, "w", encoding="utf-8") as handle:
                json.dump(settings, handle, indent=2)
        except OSError as exc:
            print(f"Could not save window settings: {exc}")

    def load_window_settings(self) -> None:
        try:
            with open(self.window_settings_file, "r", encoding="utf-8") as handle:
                settings = json.load(handle)
        except (OSError, ValueError, TypeError):
            self.center_default_window()
            return

        self.window_resizable = bool(settings.get("resizable", True))
        self.window_position_pinned = bool(settings.get("position_pinned", False))
        self.window_stays_on_top = bool(settings.get("always_on_top", False))
        self.hide_main_window_on_minimize = bool(
            settings.get("hide_main_window_on_minimize", False)
        )
        mini_settings = settings.get("mini_player", {})
        self._mini_player_settings = (
            mini_settings if isinstance(mini_settings, dict) else {}
        )
        self.setWindowFlag(
            Qt.WindowType.WindowStaysOnTopHint,
            self.window_stays_on_top,
        )
        geometry = settings.get("geometry")
        if self.window_position_pinned and isinstance(geometry, list) and len(geometry) == 4:
            try:
                x, y, width, height = (int(value) for value in geometry)
                candidate = QRect(x, y, width, height)
                screens = QApplication.screens()
                visible = any(
                    candidate.intersected(screen.availableGeometry()).width() >= 240
                    and candidate.intersected(screen.availableGeometry()).height() >= 80
                    for screen in screens
                )
                if visible and width >= self.minimumWidth() and height >= self.minimumHeight():
                    self.setGeometry(candidate)
                    return
            except (TypeError, ValueError):
                pass
            self.window_position_pinned = False
        self.center_default_window()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            isinstance(watched, QWidget)
            and watched.window() is self
            and isinstance(event, QDropEvent)
            and event.type() in (
                QEvent.Type.DragEnter,
                QEvent.Type.DragMove,
                QEvent.Type.Drop,
            )
        ):
            paths = self.audio_files_from_mime_data(event.mimeData())
            if paths:
                event.acceptProposedAction()
                if event.type() == QEvent.Type.Drop:
                    self.add_dropped_files(paths)
                return True

        if isinstance(watched, QWidget) and watched.window() is self:
            if event.type() == QEvent.Type.MouseButtonPress and isinstance(event, QMouseEvent):
                global_position = event.globalPosition().toPoint()
                if (
                    event.button() == Qt.MouseButton.RightButton
                    and self.is_titlebar_widget(watched)
                ):
                    self.show_window_context_menu(global_position)
                    return True
                if event.button() == Qt.MouseButton.LeftButton:
                    edges = self.resize_edges_at(global_position)
                    if edges:
                        self._resize_edges = edges
                        self._resize_start_position = global_position
                        self._resize_start_geometry = self.geometry()
                        return True
                    if (
                        not self.window_position_pinned
                        and not self.isMaximized()
                        and self.is_titlebar_drag_target(watched)
                    ):
                        self._drag_position = global_position - self.frameGeometry().topLeft()
                        return True

            if event.type() == QEvent.Type.MouseMove and isinstance(event, QMouseEvent):
                global_position = event.globalPosition().toPoint()
                if self._resize_edges and event.buttons() & Qt.MouseButton.LeftButton:
                    self.apply_window_resize(global_position)
                    return True
                if self._drag_position is not None and event.buttons() & Qt.MouseButton.LeftButton:
                    self.move(global_position - self._drag_position)
                    return True
                if not self.isMaximized():
                    self.set_resize_cursor(
                        watched,
                        self.resize_edges_at(global_position),
                    )

            if event.type() == QEvent.Type.MouseButtonRelease and isinstance(event, QMouseEvent):
                if event.button() == Qt.MouseButton.LeftButton:
                    was_resizing = bool(self._resize_edges)
                    self._resize_edges = Qt.Edge(0)
                    self._resize_start_position = None
                    self._resize_start_geometry = None
                    self._drag_position = None
                    if was_resizing:
                        self.save_window_settings()
                    if isinstance(watched, QWidget):
                        self.set_resize_cursor(watched, Qt.Edge(0))
                    return was_resizing

        if event.type() != QEvent.Type.Wheel:
            return super().eventFilter(watched, event)

        if not isinstance(watched, QWidget):
            return False

        inside_player = False
        widget = watched
        while widget is not None:
            if widget is self:
                inside_player = True
                break
            widget = widget.parentWidget()

        if not inside_player:
            return False

        if watched.window() is not self:
            return False

        widget = watched
        protected_names = {
            "titlebar",
            "panelTop",
            "trackHeader",
            "playlistPane",
            "playlistSearchField",
            "timeLabel",
            "volumeLabel",
            "volumeSlider",
        }
        while widget is not None:
            if widget.objectName() in protected_names or widget.objectName().startswith("title"):
                event.accept()
                return True
            if isinstance(widget, QAbstractSlider):
                return False
            if isinstance(widget, QAbstractScrollArea):
                scrollbar = widget.verticalScrollBar()
                angle_delta = event.angleDelta().y()
                pixel_delta = event.pixelDelta().y()
                if pixel_delta != 0:
                    scrollbar.setValue(scrollbar.value() - pixel_delta)
                elif angle_delta != 0:
                    notches = angle_delta / 120.0
                    step = max(
                        scrollbar.singleStep(),
                        scrollbar.pageStep() // 8,
                    )
                    scrollbar.setValue(
                        scrollbar.value() - round(step * notches)
                    )
                event.accept()
                return True
            if isinstance(widget, QAbstractButton):
                event.accept()
                return True
            if widget is self:
                break
            widget = widget.parentWidget()

        delta = event.angleDelta().y()
        if delta != 0:
            current_volume = self.volume_slider.value()
            notches = delta / 120.0
            new_volume = max(0, min(100, current_volume + round(5 * notches)))
            if new_volume != current_volume:
                self.set_volume(new_volume)
        event.accept()
        return True

    def toggle_maximized(self) -> None:
        if self.isMaximized():
            self.showNormal()
            self.maximize_button.setToolTip("Maximize")
        else:
            self.showMaximized()
            self.maximize_button.setToolTip("Restore")

    def minimize_to_mini_player(self) -> None:
        if self._in_tray_mode:
            return
        if not self.hide_main_window_on_minimize:
            self.showMinimized()
            return
        self._mini_restore_maximized = self.isMaximized()
        self._enter_tray_mode()

    def _enter_tray_mode(self) -> None:
        if self._in_tray_mode:
            return
        if not self.tray_available:
            self.showMinimized()
            return
        self._in_tray_mode = True
        self.mini_player.enter_mini_mode()
        self.tray_icon.show()
        self.hide()

    def restore_from_mini_player(self) -> None:
        self._in_tray_mode = False
        self.mini_player.hide_overlay()
        self.tray_icon.hide()
        if self._mini_restore_maximized:
            self.showMaximized()
        else:
            self.showNormal()
        self.raise_()
        self.activateWindow()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange and hasattr(self, "mini_player"):
            if self.isMinimized() and self.hide_main_window_on_minimize:
                old_state = event.oldState()
                self._mini_restore_maximized = (
                    self._mini_restore_maximized
                    or bool(old_state & Qt.WindowState.WindowMaximized)
                )
                self._enter_tray_mode()

    def apply_theme(self) -> None:
        theme = """
            QMainWindow, QWidget#window {
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:1,
                    stop:0 #08182f, stop:0.55 #1a3154, stop:1 #321c32
                );
                color: #eaf4ff;
                font-family: "Noto Sans", "Noto Sans Arabic", "Segoe UI"; font-size: 13px;
            }
            QDialog, QMessageBox, QInputDialog, QFileDialog {
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:1,
                    stop:0 #0a1a31, stop:0.65 #163657, stop:1 #542538
                );
                color: #eaf4ff;
                border: 1px solid #5d86aa;
                font-family: "Noto Sans", "Noto Sans Arabic", "Segoe UI"; font-size: 13px;
            }
            QDialog QLabel, QMessageBox QLabel, QInputDialog QLabel, QFileDialog QLabel {
                color: #d8efff;
                background: transparent;
            }
            QDialogButtonBox { background: transparent; }
            QDialogButtonBox QPushButton, QMessageBox QPushButton {
                min-width: 82px; min-height: 30px;
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #123454, stop:1 #2a4e78);
                border: 1px solid #5a91b9; border-radius: 4px;
                color: #f4fbff; font-weight: 700; padding: 6px 12px;
            }
            QDialogButtonBox QPushButton:hover, QMessageBox QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #205b91, stop:1 #74405d);
                border-color: #82bce3;
            }
            QDialogButtonBox QPushButton:pressed, QMessageBox QPushButton:pressed {
                background-color: #091827; border-color: #c85c73;
            }
            QTabWidget { background: transparent; }
            QTabWidget::pane {
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:1,
                    stop:0 #0a1a31, stop:0.65 #163657, stop:1 #321c32
                );
                border: 1px solid #5d86aa;
                top: -1px;
            }
            QTabBar::tab {
                background: #102b46;
                color: #b9dcf5;
                border: 1px solid #315f86;
                border-bottom: none;
                padding: 8px 18px;
                min-width: 90px;
            }
            QTabBar::tab:hover {
                background: #205b91;
                color: #ffffff;
            }
            QTabBar::tab:selected {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #1dd4c8, stop:0.55 #2b78a8, stop:1 #74304a);
                color: #ffffff;
                border-color: #82bce3;
            }
            QLabel { background: transparent; color: #d8efff; }
            QCheckBox#audioProcessingToggle {
                background: transparent; color: #ffffff;
            }
            QLabel#panelTitle {
                color: #78b9ea; font-size: 13px; font-weight: 800;
                letter-spacing: 2px;
            }
            QWidget#titlebar {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #0b1d38, stop:0.72 #16365d, stop:1 #6d202d);
                border-bottom: 1px solid #5a7898;
            }
            QLabel#brandName {
                color: #f5fbff; font-size: 15px; font-weight: 800;
                letter-spacing: 2px;
            }
            QLabel#brandLogo {
                border: 0px; background: transparent;
            }
            QLabel#brandVariant {
                color: #7ba9d3; font-size: 11px; font-weight: 700;
                border-left: 2px solid #c84d4d; padding-left: 8px;
            }
            QLabel#appTitle { color: #d8eaff; font-size: 13px; letter-spacing: 0px; }
            QLabel#trackTitle {
                color: white; font-size: 17px; font-weight: 700; padding: 4px;
            }
            QLabel#trackDetails, QLabel#timeLabel, QLabel#volumeLabel {
                color: #91a9c1;
            }
            QLabel#volumeLabel {
                color: #ffffff; font-weight: 700; min-width: 48px;
            }
            QWidget#leftRail, QWidget#trackHeader, QWidget#panelTop {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 rgba(20,48,78,230), stop:1 rgba(74,37,53,230));
                border: 1px solid #5d86aa;
            }
            QLabel#equalizerTag {
                color: #8fc4ee; border: 1px solid #315f86;
                border-radius: 3px; padding: 4px 8px; letter-spacing: 1px;
            }
            QLabel#artwork {
                background-color: #090f1a; border: 1px solid #183a59;
            }
            QLabel#bottomStatus {
                color: #6eb6e8; font-weight: 700; padding: 0 8px;
            }
            QWidget#footer {
                border-top: 1px solid #5d86aa;
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #102b49, stop:0.65 #4a273c, stop:1 #6b202c);
            }
            QLabel#bottomStatus[recording="true"] { color: #ff8290; }
            QLabel#recordingTimer {
                color: #ffffff; background-color: #091827;
                border: 1px solid #527ba1; border-radius: 3px;
                padding: 2px 8px; min-width: 82px;
                font-family: "Consolas"; font-size: 14px; font-weight: 700;
            }
            QListWidget {
                background-color: rgba(7,20,38,220);
                border: 1px solid #527ba1;
                border-radius: 3px; outline: none;
            }
            QListWidget::item {
                padding: 9px 8px; border-bottom: 1px solid #14283d;
                color: #b9dcf5;
            }
            QListWidget::item:selected {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #8f2737, stop:1 #2b78a8); color: white;
            }
            QListWidget::item:hover { background-color: #173959; }
            QWidget#selectionToolbar {
                background-color: #102b46;
                border: 1px solid #315a78;
                border-radius: 4px;
            }
            QLabel#selectionCount {
                color: #bfe7ff;
                font-weight: 700;
                padding: 0 5px;
            }
            QWidget#selectionToolbar QPushButton {
                padding: 4px 8px;
                min-height: 22px;
            }
            QPushButton#selectionDeleteButton {
                background-color: #702b3d;
                border-color: #a64b5f;
            }
            QPushButton#selectionDeleteButton:hover {
                background-color: #96384e;
            }
            QListWidget#playlistCards {
                background-color: rgba(7,20,38,220);
                border: 1px solid #527ba1;
                border-radius: 6px; padding: 8px;
            }
            QListWidget#playlistCards::item {
                background-color: #091827;
                border: 1px solid #315a78;
                border-radius: 5px;
                padding: 6px;
                margin: 3px 4px;
            }
            QListWidget#playlistCards::item:selected {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #74304a, stop:1 #2b78a8);
                border: 1px solid #8fc3e7;
            }
            QPushButton {
                background-color: #123454; border: 1px solid #285b87;
                border-radius: 3px; padding: 7px 11px; color: #dbeeff;
            }
            QPushButton:hover { background-color: #205b91; }
            QComboBox, QLineEdit {
                background-color: #091827; color: #d8efff;
                border: 1px solid #527ba1; border-radius: 3px;
                selection-background-color: #2b78a8; selection-color: white;
            }
            QMenu {
                background-color: #0d1f34; color: #d8efff;
                border: 1px solid #527ba1;
            }
            QMenu::item:selected {
                background-color: #2b78a8; color: white;
            }
            QToolButton#iconButton {
                background-color: #102b46; border: 1px solid #1f4d74;
                border-radius: 3px; margin: 1px;
            }
            QToolButton#iconButton:hover { background-color: #205b91; }
            QToolButton#iconButton:pressed { background-color: #09243a; }
            QToolButton#recordButton, QToolButton#recordingPauseButton {
                background-color: #102b46; border: 1px solid #1f4d74;
                border-radius: 3px; margin: 1px;
            }
            QToolButton#recordButton[recording="true"] {
                background-color: #8d2639; border: 2px solid #ff8a98;
            }
            QToolButton#recordButton[recording="true"]:hover {
                background-color: #bd3047; border-color: #ffd1d6;
            }
            QToolButton#recordingPauseButton:hover { background-color: #205b91; }
            QToolButton#iconButton[active="true"] {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #1dd4c8, stop:0.55 #4a9fd8, stop:1 #9348a8);
                border: 2px solid #f0fbff;
                border-radius: 5px;
            }
            QToolButton#iconButton[active="true"]:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #35eee0, stop:0.55 #69baff, stop:1 #b965c9);
                border-color: #ffffff;
            }
            QToolButton#internetAccessButton {
                background: transparent;
                border: 1px solid transparent;
                border-radius: 3px;
                margin: 1px;
            }
            QToolButton#internetAccessButton:hover {
                background-color: #205b91;
                border-color: #4a86b5;
            }
            QToolButton#internetAccessButton[active="true"] {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #176b37, stop:0.55 #24a148, stop:1 #42d66b);
                border: 2px solid #8cff9d;
            }
            QToolButton#internetAccessButton[active="true"]:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #218b49, stop:0.55 #32c45e, stop:1 #69f58a);
                border-color: #c1ffca;
            }
            QToolButton#titleMinimizeButton {
                background-color: #123b64; border: 1px solid #4ca0d0;
                border-radius: 2px; margin: 0;
            }
            QToolButton#titleMinimizeButton:hover {
                background-color: #1c668f; border-color: #79c9ed;
            }
            QToolButton#titleMinimizeButton:pressed {
                background-color: #0a2949; border-color: #3c86b3;
            }
            QWidget#miniPlayerPanel {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #0b1d38, stop:0.62 #16365d, stop:1 #4a273c);
                border: 1px solid #6aa9d2;
                border-top: 2px solid #45c8df;
                border-radius: 0 0 12px 12px;
            }
            QLabel#miniPlayerTitle {
                color: #f5fbff; font-size: 14px; font-weight: 800;
            }
            QLabel#miniPlayerDetails { color: #9dc3df; font-size: 11px; }
            QLabel#miniPlayerTime {
                color: #d7edff; font-family: "Consolas"; font-size: 11px;
                min-width: 42px;
            }
            QLabel#miniPlayerVolume { color: #9dc3df; font-size: 11px; }
            QLabel#miniPlayerBadge {
                color: #9db8ce; background: #102b46;
                border: 1px solid #315f86; border-radius: 9px;
                padding: 4px 10px; font-size: 10px; font-weight: 800;
            }
            QLabel#miniPlayerBadge[active="true"] {
                color: #c9fff0; background: #124a49;
                border-color: #29c5a5;
            }
            QLabel#miniPlayerBadge[live="true"] {
                color: #ffe5e9; background: #70263c;
                border-color: #ff6c88;
            }
            QToolButton#miniPlayerTransport {
                background: #102b46; border: 1px solid #315f86;
                border-radius: 8px; padding: 4px;
            }
            QToolButton#miniPlayerTransport:hover {
                background: #205b91; border-color: #82bce3;
            }
            QToolButton#miniPlayerTransport:disabled { opacity: 0.45; }
            QToolButton#miniPlayerOpenButton {
                color: #eaf8ff; background: #174b73;
                border: 1px solid #5b9bc4; border-radius: 7px;
                padding: 6px 10px; font-size: 11px; font-weight: 700;
            }
            QToolButton#miniPlayerOpenButton:hover {
                background: #246e9c; border-color: #b7e7ff;
            }
            QToolButton#miniPlayerCollapseButton {
                color: #d7edff; background: #102b46;
                border: 1px solid #47779e; border-radius: 7px;
                min-width: 30px; max-width: 30px;
                min-height: 30px; max-height: 30px;
                font-size: 12px; font-weight: 800;
            }
            QToolButton#miniPlayerCollapseButton:hover {
                color: #ffffff; background: #205b91;
                border-color: #b7e7ff;
            }
            QSlider#miniPlayerProgress, QSlider#miniPlayerVolumeSlider {
                background: transparent; min-height: 18px;
            }
            QToolButton#windowButton {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #174c75, stop:0.65 #274c78, stop:1 #4a4166);
                border: 1px solid #6599bd;
                border-radius: 2px; color: white; font-size: 18px; margin: 0;
            }
            QToolButton#windowButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #237aab, stop:0.65 #3c6290, stop:1 #66506f);
                border-color: #90c7e6;
            }
            QToolButton#windowButton:pressed {
                background-color: #123654; border-color: #4f86a8;
            }
            QToolButton#titleCloseButton {
                background-color: #75233a; border: 1px solid #ba4961;
                border-radius: 2px; margin: 0;
            }
            QToolButton#titleCloseButton:hover {
                background-color: #c72e49; border-color: #ff8495;
            }
            QToolButton#titleCloseButton:pressed {
                background-color: #5e172b; border-color: #9d354d;
            }
            QScrollBar:vertical {
                background: #07111f;
                width: 12px;
                margin: 2px 1px 2px 1px;
                border: 1px solid #3f6f98;
                border-radius: 6px;
            }
            QScrollBar::handle:vertical {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #3d7dad, stop:0.55 #4fb8ff, stop:1 #9b4c78);
                min-height: 28px;
                border: 1px solid #76b5df;
                border-radius: 5px;
            }
            QScrollBar::handle:vertical:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #5ba7d6, stop:0.55 #73c9ff, stop:1 #c55e86);
                border-color: #b5dcf5;
            }
            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical {
                height: 0px;
                background: transparent;
                border: none;
            }
            QScrollBar::add-page:vertical,
            QScrollBar::sub-page:vertical {
                background: #091827;
                border-radius: 5px;
            }
            QScrollBar:horizontal {
                background: #07111f;
                height: 12px;
                margin: 1px 2px 1px 2px;
                border: 1px solid #3f6f98;
                border-radius: 6px;
            }
            QScrollBar::handle:horizontal {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #3d7dad, stop:0.55 #4fb8ff, stop:1 #9b4c78);
                min-width: 28px;
                border: 1px solid #76b5df;
                border-radius: 5px;
            }
            QScrollBar::handle:horizontal:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #5ba7d6, stop:0.55 #73c9ff, stop:1 #c55e86);
                border-color: #b5dcf5;
            }
            QScrollBar::add-line:horizontal,
            QScrollBar::sub-line:horizontal {
                width: 0px;
                background: transparent;
                border: none;
            }
            QScrollBar::add-page:horizontal,
            QScrollBar::sub-page:horizontal {
                background: #091827;
                border-radius: 5px;
            }
            QAbstractScrollArea::corner {
                background: #091827;
                border: none;
            }
            QSlider::groove:horizontal {
                height: 8px; background: #06111e;
                border: 1px solid #73a9d5; border-radius: 4px;
            }
            QSlider::sub-page:horizontal {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #19d4c8, stop:0.55 #4fb8ff, stop:1 #9d4cff);
                border-radius: 4px;
            }
            QSlider::handle:horizontal {
                background: #f7fbff; border: 2px solid #72c1ff;
                width: 14px; height: 14px;
                margin: -5px 0; border-radius: 7px;
            }
            QSlider::handle:horizontal:hover {
                background: #ffffff; border: 2px solid #d2a7ff;
            }
            QSlider::groove:vertical {
                width: 10px; background: #06111e;
                border: 1px solid #73a9d5; border-radius: 5px;
            }
            QSlider::handle:vertical {
                background: #f7fbff; border: 2px solid #72c1ff;
                height: 14px; width: 14px;
                margin: 0 -3px; border-radius: 7px;
            }
            QSlider::handle:vertical:hover {
                background: #ffffff; border: 2px solid #d2a7ff;
            }
            QSlider::add-page:vertical { background: #18324d; border-radius: 5px; }
            QSlider::sub-page:vertical {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #9d4cff, stop:0.55 #4fb8ff, stop:1 #19d4c8);
                border-radius: 5px;
            }
            QComboBox::drop-down {
                width: 24px; border-left: 1px solid #527ba1;
                background-color: #163957;
            }
            QComboBox::down-arrow {
                image: none;
                border-left: 5px solid transparent;
                border-right: 5px solid transparent;
                border-top: 7px solid #9ed2f4;
                width: 0; height: 0;
            }
            QComboBox QAbstractItemView {
                background-color: #091827; color: #d8efff;
                border: 1px solid #527ba1; selection-background-color: #2b78a8;
                selection-color: white; outline: none;
            }
            QFileDialog QTreeView, QFileDialog QListView, QFileDialog QTableView {
                background-color: #091827; color: #d8efff;
                border: 1px solid #527ba1; selection-background-color: #2b78a8;
            }
            QHeaderView::section {
                background-color: #102d4b; color: #d8efff;
                border: 1px solid #527ba1; padding: 5px;
            }
            QLineEdit, QComboBox, QSpinBox {
                min-height: 26px; padding: 4px 7px;
            }
            QLineEdit#playlistSearchField {
                min-height: 32px; max-height: 32px;
                min-width: 220px; max-width: 220px;
                background-color: #091827;
                border: 1px solid #72a8d2;
                border-radius: 4px;
                color: #d8efff;
                padding: 5px 9px;
                selection-background-color: #2b78a8;
            }
        """
        self.setStyleSheet(theme)
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(theme)

    def closeEvent(self, event) -> None:
        self.tray_icon.hide()
        self.mini_player.hide_overlay()
        self.save_window_settings()
        self.timer.stop()
        self.stop_recording()
        self.sync_current_playlist()
        self.save_playlists()
        self.stop_stream()
        super().closeEvent(event)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("SRPlayer")
    app.setWindowIcon(load_data_icon("SR.PNG"))
    ui_font_family = "Segoe UI"
    for font_path in ("fonts/NotoSans.ttf", "fonts/NotoSansArabic.ttf"):
        font_id = QFontDatabase.addApplicationFontFromData(
            read_data_asset(font_path)
        )
        families = QFontDatabase.applicationFontFamilies(font_id)
        if families and font_path.endswith("NotoSans.ttf"):
            ui_font_family = families[0]
    app.setFont(QFont(ui_font_family, 13))
    window = AudioPlayer()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
