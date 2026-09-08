"""
print_runner.py — Визуальный раннер AutoPrint.

Компилируется в print.exe через PyInstaller.
НЕ содержит бизнес-логики — только GUI и динамический импорт ядра.

Компиляция:
    pyinstaller --onefile --windowed --name print print_runner.py

Размещение после компиляции:
    /ИМЯ_ПРОЕКТА/!_Печать/print.exe
    /ИМЯ_ПРОЕКТА/!_Печать/config/config.ini

Зависимости:
    pip install customtkinter pywin32 ezdxf pypdf psutil pyinstaller
"""
import subprocess
import sys
import os
import queue
import threading
import importlib.util
import configparser
import tkinter as tk
from tkinter import filedialog, messagebox
from pathlib import Path

# pip install customtkinter
import customtkinter as ctk


# ─────────────────────────────────────────────
# Константы GUI
# ─────────────────────────────────────────────

APP_TITLE = "AutoCAD AutoPrint"
APP_GEOMETRY = "1100x750"

# Интервал опроса log_queue (мс)
LOG_POLL_INTERVAL = 100

# Цвета состояний строк таблицы (dark_theme, light_theme)
COLOR_ACTIVE = ("gray14", "gray86")
COLOR_INACTIVE = ("gray30", "gray60")
COLOR_ERROR = ("#5a0000", "#ffcccc")


# ─────────────────────────────────────────────
# Вспомогательные функции
# ─────────────────────────────────────────────

def unc_to_drive_letter(unc_path: str) -> str:
    """
    Конвертирует UNC-путь в путь с буквой диска.
    Например: \\storage05\Паспортизация\... → J:\...
    """
    if not unc_path.startswith("\\\\"):
        return unc_path
    
    try:
        # Получаем список mapped drives
        result = subprocess.run(
            ["net", "use"],
            capture_output=True,
            text=True,
            timeout=5,
            encoding="cp866"  # Русская локаль Windows
        )
        if result.returncode != 0:
            return unc_path
        
        # Парсим вывод net use (русская локаль)
        # Ищем строки вида: "OK           J:        \\storage05\Паспортизация"
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line or not line.startswith("OK"):
                continue
            
            # Разбиваем по пробелам
            parts = line.split()
            
            # Ищем букву диска (J:) и UNC-путь
            drive_letter = None
            unc = None
            
            for part in parts:
                if len(part) == 2 and part[1] == ":":
                    drive_letter = part[0].upper()
                elif part.startswith("\\\\"):
                    unc = part
            
            if drive_letter and unc:
                # Проверяем, что наш UNC начинается с этого mapped UNC
                if unc_path.lower().startswith(unc.lower()):
                    # Заменяем UNC на букву диска
                    remainder = unc_path[len(unc):]
                    return f"{drive_letter}:{remainder}"
        
        return unc_path
    except Exception:
        return unc_path

def get_exe_dir() -> Path:
    """
    Возвращает директорию print.exe (или print_runner.py при запуске из исходника).
    Корректно работает в скомпилированном и dev-режиме.
    """
    if getattr(sys, "frozen", False):
        # Скомпилированный .exe
        exe_path = os.path.abspath(sys.executable)
        # Конвертируем UNC в букву диска если нужно
        exe_path = unc_to_drive_letter(exe_path)
        return Path(exe_path).parent
    else:
        # Запуск из исходника (разработка)
        script_path = os.path.abspath(__file__)
        # Конвертируем UNC в букву диска если нужно
        script_path = unc_to_drive_letter(script_path)
        return Path(script_path).parent


def get_config_path() -> Path:
    """Возвращает путь к config/config.ini относительно exe."""
    return get_exe_dir() / "config" / "config.ini"


def read_core_path() -> str:
    """
    Читает CorePath из config/config.ini.
    Возвращает пустую строку если файл не найден или ключ отсутствует.
    """
    cfg = configparser.ConfigParser()
    cfg_path = get_config_path()
    if not cfg_path.exists():
        return ""
    cfg.read(str(cfg_path), encoding="utf-8")
    return cfg.get("Paths", "CorePath", fallback="").strip()


def write_core_path(new_path: str) -> None:
    """Сохраняет CorePath в config/config.ini."""
    cfg = configparser.ConfigParser()
    cfg_path = get_config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    if cfg_path.exists():
        cfg.read(str(cfg_path), encoding="utf-8")
    if not cfg.has_section("Paths"):
        cfg.add_section("Paths")
    cfg.set("Paths", "CorePath", new_path)
    with open(cfg_path, "w", encoding="utf-8") as f:
        cfg.write(f)


def load_orchestrator(core_path: str):
    """
    Динамически загружает orchestrator.py из CorePath.

    Каждый вызов перезагружает модуль — изменения в .py файлах ядра
    вступают в силу без перекомпиляции print.exe.

    Возвращает модуль orchestrator или None при ошибке.
    """
    core = Path(core_path)
    if not core.is_dir():
        messagebox.showerror(
            "Ошибка CorePath",
            f"Папка ядра недоступна:\n{core_path}\n\nПроверьте config.ini"
        )
        return None

    # Добавляем CorePath в sys.path для корректного импорта внутри модулей ядра
    core_str = str(core)
    if core_str not in sys.path:
        sys.path.insert(0, core_str)

    try:
        spec = importlib.util.spec_from_file_location(
            "orchestrator",
            str(core / "orchestrator.py")
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception as e:
        messagebox.showerror(
            "Ошибка загрузки ядра",
            f"Не удалось загрузить orchestrator.py:\n{e}"
        )
        return None


# ─────────────────────────────────────────────
# Строка таблицы объектов
# ─────────────────────────────────────────────

class ObjectRow(ctk.CTkFrame):
    """
    Одна строка в таблице объектов.

    Состояния:
        active   — нормальный цвет, войдёт в печать
        inactive — серый, исключена но не удалена
        error    — красный, путь недоступен, блокирует кнопку запуска
    """

    def __init__(self, master, path: str, group: str, on_toggle, on_remove, **kwargs):
        super().__init__(master, **kwargs)

        self.path = path
        self.group = group
        self.on_toggle = on_toggle
        self.on_remove = on_remove

        # Определяем начальное состояние через валидацию пути
        if not os.path.isdir(path) or not os.access(path, os.R_OK):
            self._state = "error"
        else:
            self._state = "active"

        self._build_ui()
        self._apply_state_style()

    def _build_ui(self):
        self.grid_columnconfigure(1, weight=1)

        # Индикатор состояния (●/○/!)
        self.indicator = ctk.CTkLabel(self, text="●", width=20)
        self.indicator.grid(row=0, column=0, padx=(6, 2), pady=4)

        # Имя объекта
        display = os.path.basename(self.path) or self.path
        self.label = ctk.CTkLabel(self, text=display, anchor="w")
        self.label.grid(row=0, column=1, padx=4, pady=4, sticky="ew")

        # Полный путь (мелким шрифтом)
        self.path_label = ctk.CTkLabel(
            self, text=self.path, anchor="w",
            font=ctk.CTkFont(size=10),
            text_color="gray"
        )
        self.path_label.grid(row=1, column=1, padx=4, pady=(0, 2), sticky="ew")

        # Кнопка вкл/выкл
        self.toggle_btn = ctk.CTkButton(
            self, text="Вкл/Выкл", width=80,
            command=self._on_toggle_click
        )
        self.toggle_btn.grid(row=0, column=2, padx=4, pady=4)

        # Кнопка удаления строки
        self.remove_btn = ctk.CTkButton(
            self, text="✕", width=30,
            fg_color="transparent",
            text_color=("gray40", "gray60"),
            command=self._on_remove_click
        )
        self.remove_btn.grid(row=0, column=3, padx=(0, 6), pady=4)

    def _on_toggle_click(self):
        """Переключает состояние active ↔ inactive. Ошибочные строки не переключаются."""
        if self._state == "error":
            return
        self._state = "inactive" if self._state == "active" else "active"
        self._apply_state_style()
        self.on_toggle()

    def _on_remove_click(self):
        self.on_remove(self)

    def _apply_state_style(self):
        """Применяет визуальный стиль в зависимости от текущего состояния."""
        if self._state == "active":
            self.configure(fg_color=COLOR_ACTIVE)
            self.indicator.configure(text="●", text_color="#4CAF50")
        elif self._state == "inactive":
            self.configure(fg_color=COLOR_INACTIVE)
            self.indicator.configure(text="○", text_color="gray")
        elif self._state == "error":
            self.configure(fg_color=COLOR_ERROR)
            self.indicator.configure(text="!", text_color="red")

    @property
    def is_active(self) -> bool:
        return self._state == "active"

    @property
    def has_error(self) -> bool:
        return self._state == "error"


# ─────────────────────────────────────────────
# Группа объектов в таблице
# ─────────────────────────────────────────────

class GroupFrame(ctk.CTkFrame):
    """
    Группа объектов с заголовком.
    Поддерживает сворачивание/разворачивание.
    Содержит список ObjectRow.
    """

    def __init__(self, master, group_name: str, on_state_change, **kwargs):
        super().__init__(master, **kwargs)
        self.group_name = group_name
        self.on_state_change = on_state_change
        self.rows: list[ObjectRow] = []
        self._collapsed = False

        self._build_header()

        self.rows_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.rows_frame.pack(fill="x", padx=4, pady=(0, 4))

    def _build_header(self):
        header = ctk.CTkFrame(self, fg_color=("gray20", "gray75"))
        header.pack(fill="x", padx=2, pady=(2, 0))

        self.collapse_btn = ctk.CTkButton(
            header, text="▼", width=30,
            fg_color="transparent",
            command=self._toggle_collapse
        )
        self.collapse_btn.pack(side="left", padx=2)

        ctk.CTkLabel(
            header,
            text=self.group_name,
            font=ctk.CTkFont(weight="bold")
        ).pack(side="left", padx=4)

    def _toggle_collapse(self):
        self._collapsed = not self._collapsed
        if self._collapsed:
            self.rows_frame.pack_forget()
            self.collapse_btn.configure(text="▶")
        else:
            self.rows_frame.pack(fill="x", padx=4, pady=(0, 4))
            self.collapse_btn.configure(text="▼")

    def add_row(self, path: str) -> ObjectRow:
        """Добавляет строку объекта в группу."""
        row = ObjectRow(
            self.rows_frame,
            path=path,
            group=self.group_name,
            on_toggle=self.on_state_change,
            on_remove=self._remove_row,
            fg_color="transparent"
        )
        row.pack(fill="x", pady=1)
        self.rows.append(row)
        self.on_state_change()
        return row

    def _remove_row(self, row: ObjectRow):
        row.destroy()
        self.rows.remove(row)
        self.on_state_change()

    def get_active_paths(self) -> list[str]:
        """Возвращает пути всех активных (не деактивированных, без ошибок) строк."""
        return [r.path for r in self.rows if r.is_active]

    def has_errors(self) -> bool:
        """Возвращает True если в группе есть хотя бы одна строка с ошибкой."""
        return any(r.has_error for r in self.rows)


# ─────────────────────────────────────────────
# Главное окно приложения
# ─────────────────────────────────────────────

class AutoPrintApp(ctk.CTk):
    """
    Главное окно GUI-раннера AutoPrint.

    Принцип работы:
        - Вся бизнес-логика живёт в ядре (CorePath/*.py)
        - GUI только отображает результаты и передаёт команды
        - Ядро загружается динамически через importlib при каждом вызове
        - Лог передаётся через queue.Queue (ядро пишет, GUI читает каждые 100мс)
        - Модули ядра можно редактировать без перекомпиляции print.exe
    """

    def __init__(self):
        super().__init__()

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.title(APP_TITLE)
        self.geometry(APP_GEOMETRY)
        self.minsize(800, 600)

        # Очередь лога: ядро → GUI
        self.log_queue: queue.Queue = queue.Queue()

        # Группы объектов в таблице: {group_name: GroupFrame}
        self.groups: dict[str, GroupFrame] = {}

        # Директория print.exe
        self.exe_dir = get_exe_dir()

        # Построение интерфейса
        self._build_ui()

        # Запуск polling лога
        self._start_log_polling()

        # Автосканирование при запуске (через М1 ядра)
        self._auto_scan()

    # ──────────────────────────────────────────
    # Построение интерфейса
    # ──────────────────────────────────────────

    def _build_ui(self):
        """Строит весь интерфейс приложения."""
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self._build_top_bar()
        self._build_main_area()
        self._build_bottom_bar()

    def _build_top_bar(self):
        """Верхняя панель: поле CorePath + кнопки добавления объектов."""
        top = ctk.CTkFrame(self)
        top.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 0))
        top.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(top, text="CorePath:").grid(
            row=0, column=0, padx=6, pady=6
        )

        self.core_path_var = tk.StringVar(value=read_core_path())
        self.core_path_entry = ctk.CTkEntry(
            top, textvariable=self.core_path_var, width=400
        )
        self.core_path_entry.grid(row=0, column=1, padx=4, pady=6, sticky="ew")

        ctk.CTkButton(
            top, text="Сохранить", width=90,
            command=self._save_core_path
        ).grid(row=0, column=2, padx=4, pady=6)

        ctk.CTkButton(
            top, text="+ Проект", width=100,
            command=self._add_project
        ).grid(row=0, column=3, padx=4, pady=6)

        ctk.CTkButton(
            top, text="+ Объект", width=100,
            command=self._add_object
        ).grid(row=0, column=4, padx=(4, 8), pady=6)

    def _build_main_area(self):
        """Центральная область: таблица объектов (слева) + лог-консоль (справа)."""
        paned = ctk.CTkFrame(self)
        paned.grid(row=1, column=0, sticky="nsew", padx=8, pady=8)
        paned.grid_columnconfigure(0, weight=1)
        paned.grid_columnconfigure(1, weight=1)
        paned.grid_rowconfigure(0, weight=1)

        # ── Левая панель: таблица объектов ──
        left = ctk.CTkFrame(paned)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        left.grid_rowconfigure(1, weight=1)
        left.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            left, text="Объекты печати",
            font=ctk.CTkFont(size=14, weight="bold")
        ).grid(row=0, column=0, padx=8, pady=(8, 4), sticky="w")

        self.objects_scroll = ctk.CTkScrollableFrame(left)
        self.objects_scroll.grid(row=1, column=0, sticky="nsew", padx=4, pady=(0, 4))
        self.objects_scroll.grid_columnconfigure(0, weight=1)

        # ── Правая панель: лог-консоль ──
        right = ctk.CTkFrame(paned)
        right.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
        right.grid_rowconfigure(1, weight=1)
        right.grid_columnconfigure(0, weight=1)

        log_header = ctk.CTkFrame(right, fg_color="transparent")
        log_header.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))

        ctk.CTkLabel(
            log_header, text="Лог выполнения",
            font=ctk.CTkFont(size=14, weight="bold")
        ).pack(side="left")

        ctk.CTkButton(
            log_header, text="Очистить", width=80,
            command=self._clear_log
        ).pack(side="right")

        self.log_box = ctk.CTkTextbox(
            right, state="disabled", wrap="word",
            font=ctk.CTkFont(family="Consolas", size=11)
        )
        self.log_box.grid(row=1, column=0, sticky="nsew", padx=4, pady=(0, 4))

        # Цветовые теги для лога
        self.log_box._textbox.tag_configure("white", foreground="white")
        self.log_box._textbox.tag_configure("yellow", foreground="#FFD700")
        self.log_box._textbox.tag_configure("red", foreground="#FF6B6B")

        # ПКМ-меню лога
        self._build_log_context_menu()

    def _build_log_context_menu(self):
        """Контекстное меню лог-консоли (правая кнопка мыши)."""
        self.log_context_menu = tk.Menu(self, tearoff=0)
        self.log_context_menu.add_command(
            label="Копировать выделенное",
            command=self._copy_selected_log
        )
        self.log_context_menu.add_command(
            label="Копировать весь лог",
            command=self._copy_all_log
        )
        self.log_box.bind("<Button-3>", self._show_log_context_menu)

    def _build_bottom_bar(self):
        """Нижняя панель: кнопка Pipeline + пять пошаговых кнопок + прогресс-бар."""
        bottom = ctk.CTkFrame(self)
        bottom.grid(row=2, column=0, sticky="ew", padx=8, pady=(0, 8))

        # Главная кнопка Pipeline
        self.pipeline_btn = ctk.CTkButton(
            bottom,
            text="▶ Pipeline (Авто)",
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color="#1a6b1a",
            hover_color="#145214",
            command=self._run_pipeline
        )
        self.pipeline_btn.pack(side="left", padx=(8, 4), pady=8)

        # Пять пошаговых кнопок (по одной на модуль)
        steps = [
            ("М1: Сканирование", self._run_m1),
            ("М2: Фильтр версий", self._run_m2),
            ("М3: DSD генератор", self._run_m3),
            ("М4: Печать AutoCAD", self._run_m4),
            ("М5: Дистрибуция PDF", self._run_m5),
        ]
        self.step_buttons = []
        for label, cmd in steps:
            btn = ctk.CTkButton(bottom, text=label, width=130, command=cmd)
            btn.pack(side="left", padx=2, pady=8)
            self.step_buttons.append(btn)

        # Прогресс-бар (скрыт до запуска)
        self.progress_bar = ctk.CTkProgressBar(bottom, width=200)
        self.progress_bar.pack(side="right", padx=(4, 8), pady=8)
        self.progress_bar.set(0)
        self.progress_bar.pack_forget()

    # ──────────────────────────────────────────
    # М1: загрузка модуля и делегирование
    # ──────────────────────────────────────────

    def _load_m1(self):
        """
        Загружает module1_path_collector.py из CorePath.

        Каждый вызов — свежая загрузка модуля.
        Изменения в .py вступают в силу без перекомпиляции print.exe.

        Возвращает модуль или None при ошибке.
        """
        core_path = self.core_path_var.get().strip()
        if not core_path:
            self._log("[М1] CorePath не задан. Проверьте config.ini.", "red")
            return None

        m1_path = Path(core_path) / "module1_path_collector.py"
        if not m1_path.exists():
            self._log(
                f"[М1] module1_path_collector.py не найден: {m1_path}",
                "red"
            )
            return None

        # CorePath должен быть в sys.path — нужен для импорта orchestrator
        # внутри module1_path_collector (from orchestrator import ...)
        core_str = str(Path(core_path))
        if core_str not in sys.path:
            sys.path.insert(0, core_str)

        try:
            spec = importlib.util.spec_from_file_location(
                "module1_path_collector",
                str(m1_path)
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
        except Exception as e:
            self._log(f"[М1] Ошибка загрузки module1_path_collector: {e}", "red")
            return None

    def _auto_scan(self):
        """
        Автосканирование при запуске — делегирует в М1 ядра.

        Вычисление корня проекта выполняется в M1.scan_from_exe():
            exe_dir = /ИМЯ_ПРОЕКТА/!_Печать/
            project_dir = exe_dir.parent = /ИМЯ_ПРОЕКТА/
        """
        m1 = self._load_m1()
        if m1 is None:
            self._log(
                "[Старт] Ядро недоступно. Добавьте объекты вручную или проверьте CorePath.",
                "yellow"
            )
            return

        try:
            result = m1.scan_from_exe(str(self.exe_dir), self.log_queue)
            if not result.entries:
                return
            self._add_group_from_m1(result.group_name, result.entries)
        except Exception as e:
            self._log(f"[Старт] Ошибка автосканирования: {e}", "red")

    def _add_project(self):
        """
        Кнопка '+ Проект'.
        Выбор папки проекта → сканирование первого уровня через М1.
        Результат → новая группа в таблице.
        """
        folder = filedialog.askdirectory(title="Выберите папку проекта")
        if not folder:
            return

        m1 = self._load_m1()
        if m1 is None:
            return

        try:
            result = m1.scan_project(folder, self.log_queue)
            if not result.entries:
                messagebox.showinfo(
                    "Проект",
                    f"В папке '{Path(folder).name}' не найдено объектов."
                )
                return
            self._add_group_from_m1(result.group_name, result.entries)
        except Exception as e:
            self._log(f"[М1] Ошибка добавления проекта: {e}", "red")

    def _add_object(self):
        """
        Кнопка '+ Объект'.
        Выбор одной папки → валидация через М1 → группа 'Отдельные объекты'.
        Невалидный путь добавляется красной строкой (пользователь видит проблему).
        """
        folder = filedialog.askdirectory(title="Выберите папку объекта")
        if not folder:
            return

        m1 = self._load_m1()
        if m1 is None:
            return

        try:
            entry = m1.validate_object(folder, self.log_queue)
            # Добавляем в таблицу независимо от валидации:
            # невалидная строка отображается красной и блокирует кнопку запуска
            self._add_group_from_m1(entry.group_name, [entry])
            if not entry.is_valid:
                messagebox.showwarning(
                    "Ошибка валидации",
                    f"Путь недоступен:\n{folder}\n\n{entry.error_msg}"
                )
        except Exception as e:
            self._log(f"[М1] Ошибка добавления объекта: {e}", "red")

    def _add_group_from_m1(self, group_name: str, entries: list):
        """
        Добавляет группу в таблицу GUI из результатов М1.

        Принимает список ObjectEntry (dataclass из module1_path_collector).
        ObjectRow сам определяет визуальное состояние через валидацию пути.

        Аргументы:
            group_name — имя группы (имя папки проекта или 'Отдельные объекты')
            entries    — список ObjectEntry из М1
        """
        # Создаём группу если её ещё нет
        if group_name not in self.groups:
            group_frame = GroupFrame(
                self.objects_scroll,
                group_name=group_name,
                on_state_change=self._on_table_state_change
            )
            group_frame.pack(fill="x", pady=2)
            self.groups[group_name] = group_frame
        else:
            group_frame = self.groups[group_name]

        # Добавляем строку для каждого ObjectEntry
        for entry in entries:
            group_frame.add_row(entry.path)

        self._on_table_state_change()

    # ──────────────────────────────────────────
    # Управление таблицей объектов
    # ──────────────────────────────────────────

    def _on_table_state_change(self):
        """Вызывается при любом изменении состояния строк таблицы."""
        self._update_run_buttons()

    def _update_run_buttons(self):
        """
        Обновляет состояние кнопки Pipeline:
            - Блокирует если есть строки с ошибкой валидации
            - Блокирует если нет ни одного активного объекта
        """
        has_errors = any(g.has_errors() for g in self.groups.values())
        has_active = any(g.get_active_paths() for g in self.groups.values())

        state = "disabled" if (has_errors or not has_active) else "normal"
        self.pipeline_btn.configure(state=state)

    def _get_active_paths(self) -> list[str]:
        """
        Собирает все активные пути из таблицы.
        Передаётся в оркестратор перед запуском Pipeline.
        """
        result = []
        for group in self.groups.values():
            result.extend(group.get_active_paths())
        return result

    # ──────────────────────────────────────────
    # Лог-консоль
    # ──────────────────────────────────────────

    def _log(self, message: str, color: str = "white"):
        """
        Записывает сообщение в лог-консоль напрямую (из GUI-потока).

        Цвета:
            white  — информация
            yellow — предупреждение
            red    — ошибка
        """
        self.log_box.configure(state="normal")
        tag = color if color in ("white", "yellow", "red") else "white"
        self.log_box._textbox.insert("end", message + "\n", tag)
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _start_log_polling(self):
        """Запускает цикл опроса log_queue каждые LOG_POLL_INTERVAL мс."""
        self._poll_log_queue()

    def _poll_log_queue(self):
        """
        Читает все накопившиеся сообщения из log_queue и выводит в лог.

        Механизм передачи лога из ядра в GUI:
            Ядро  → log_queue.put({"message": "...", "color": "yellow"})
            GUI   → polling каждые 100мс через .after()
                  → запись в CTkTextbox
        """
        try:
            while True:
                entry = self.log_queue.get_nowait()
                msg = entry.get("message", "")
                color = entry.get("color", "white")
                self._log(msg, color)
        except queue.Empty:
            pass
        finally:
            self.after(LOG_POLL_INTERVAL, self._poll_log_queue)

    def _clear_log(self):
        """Очищает лог-консоль."""
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    def _show_log_context_menu(self, event):
        self.log_context_menu.post(event.x_root, event.y_root)

    def _copy_selected_log(self):
        """Копирует выделенный текст лога в буфер обмена."""
        try:
            selected = self.log_box._textbox.get("sel.first", "sel.last")
            self.clipboard_clear()
            self.clipboard_append(selected)
        except tk.TclError:
            pass

    def _copy_all_log(self):
        """Копирует весь текст лога в буфер обмена."""
        content = self.log_box._textbox.get("1.0", "end")
        self.clipboard_clear()
        self.clipboard_append(content)

    # ──────────────────────────────────────────
    # CorePath
    # ──────────────────────────────────────────

    def _save_core_path(self):
        """Сохраняет CorePath из поля ввода в config/config.ini."""
        new_path = self.core_path_var.get().strip()
        write_core_path(new_path)
        self._log(f"[Конфиг] CorePath сохранён: {new_path}", "white")

    # ──────────────────────────────────────────
    # Блокировка / разблокировка GUI
    # ──────────────────────────────────────────

    def _lock_gui(self):
        """
        Блокирует все элементы управления на время выполнения Pipeline.
        Показывает анимированный прогресс-бар.
        """
        self.pipeline_btn.configure(state="disabled")
        for btn in self.step_buttons:
            btn.configure(state="disabled")
        self.progress_bar.pack(side="right", padx=(4, 8), pady=8)
        self.progress_bar.configure(mode="indeterminate")
        self.progress_bar.start()

    def _unlock_gui(self):
        """
        Разблокирует GUI после завершения Pipeline.
        Скрывает прогресс-бар.
        """
        self.progress_bar.stop()
        self.progress_bar.pack_forget()
        for btn in self.step_buttons:
            btn.configure(state="normal")
        self._update_run_buttons()

    # ──────────────────────────────────────────
    # Запуск модулей через оркестратор
    # ──────────────────────────────────────────

    def _get_orchestrator(self):
        """
        Загружает и возвращает модуль orchestrator из CorePath.
        Возвращает None если CorePath не задан или ядро недоступно.
        """
        core_path = self.core_path_var.get().strip()
        if not core_path:
            messagebox.showerror("Ошибка", "CorePath не задан в config.ini")
            return None
        return load_orchestrator(core_path)

    def _run_in_thread(self, target_func, *args):
        """
        Запускает функцию ядра в отдельном потоке.

        GUI блокируется на время выполнения.
        После завершения GUI разблокируется в главном потоке через .after().
        Исключения из ядра перехватываются и выводятся в лог.
        """
        self._lock_gui()

        def wrapper():
            try:
                target_func(*args)
            except Exception as e:
                self.log_queue.put({
                    "message": f"[Ошибка] Необработанное исключение: {e}",
                    "color": "red"
                })
            finally:
                # Разблокировка только из главного потока
                self.after(0, self._unlock_gui)

        thread = threading.Thread(target=wrapper, daemon=True)
        thread.start()

    def _run_pipeline(self):
        """
        Кнопка 'Pipeline (Авто)'.
        Сквозной запуск М1 → М5 через оркестратор.
        GUI блокируется полностью до завершения.
        """
        orch = self._get_orchestrator()
        if not orch:
            return

        active_paths = self._get_active_paths()
        if not active_paths:
            messagebox.showwarning(
                "Нет объектов",
                "Нет активных объектов для печати.\nДобавьте объекты или активируйте деактивированные."
            )
            return

        self._run_in_thread(
            orch.run_pipeline,
            active_paths,
            str(self.exe_dir),
            self.log_queue
        )

    def _run_m1(self):
        """
        Пошаговый запуск М1.
        Информационный режим — сканирование уже отображено в таблице GUI.
        """
        orch = self._get_orchestrator()
        if not orch:
            return
        self._run_in_thread(
            orch.run_module1,
            str(self.exe_dir),
            self.log_queue
        )

    def _run_m2(self):
        """Пошаговый запуск М2: Version Filter & Layout Mapper."""
        orch = self._get_orchestrator()
        if not orch:
            return
        active_paths = self._get_active_paths()
        self._run_in_thread(
            orch.run_module2,
            active_paths,
            str(self.exe_dir),
            self.log_queue
        )

    def _run_m3(self):
        """
        Пошаговый запуск М3: DSD Generator.
        Требует предварительного выполнения М2 (наличие tmp/map.json).
        """
        orch = self._get_orchestrator()
        if not orch:
            return
        self._run_in_thread(
            orch.run_module3,
            str(self.exe_dir),
            self.log_queue
        )

    def _run_m4(self):
        """
        Пошаговый запуск М4: AutoCAD Print Engine.
        Требует предварительного выполнения М3 (наличие .dsd файла).
        """
        orch = self._get_orchestrator()
        if not orch:
            return
        self._run_in_thread(
            orch.run_module4,
            str(self.exe_dir),
            self.log_queue
        )

    def _run_m5(self):
        """
        Пошаговый запуск М5: PDF Distributor.
        Требует предварительного выполнения М4 (наличие monolithic_output.pdf).
        """
        orch = self._get_orchestrator()
        if not orch:
            return
        self._run_in_thread(
            orch.run_module5,
            str(self.exe_dir),
            self.log_queue
        )


# ─────────────────────────────────────────────
# Точка входа
# ─────────────────────────────────────────────

if __name__ == "__main__":
    app = AutoPrintApp()
    app.mainloop()