"""
orchestrator.py — Центральный диспетчер ядра AutoPrint.

Размещение: CorePath/orchestrator.py
Загружается динамически через importlib из print.exe.
Изменения вступают в силу без перекомпиляции print.exe.

Зависимости ядра:
    pip install pywin32 ezdxf pypdf psutil
"""

import os
import sys
import json
import shutil
import queue
import importlib
import importlib.util
from pathlib import Path

# ─────────────────────────────────────────────
# ГЛОБАЛЬНЫЕ КОНСТАНТЫ ЯДРА
# ─────────────────────────────────────────────

# Структура директорий
WORKING_DIR_NAME = "Рабочая"
APPROVED_DIR_NAME = "Согласовано"
EXCLUDE_MARKER = "!"
VERSION_DIR_PREFIX = "autoprint_v"
READY_DIR_NAME = "! ГОТОВО"
DSD_DIR_NAME = ".dsd файлы"
TMP_DIR_NAME = "tmp"

# Файлы сессии
MONOLITHIC_PDF_NAME = "monolithic_output.pdf"
MAP_JSON_NAME = "map.json"

# AutoCAD
PAGE_SETUP_NAME = "АЗ для презентации"
ACAD_PROFILE = "<<Профиль без имени>>"
ACAD_PUBLISH_COMMAND = "_-PUBLISH"

# ODA File Converter
# ВАЖНО: Проверьте версию ODA и скорректируйте путь при необходимости
ODA_PATH = r"C:\Program Files\ODA\ODAFileConverter 27.1.0\ODAFileConverter.exe"
ODA_OUTPUT_VERSION = "ACAD2018"
ODA_OUTPUT_FORMAT = "DXF"
ODA_RECURSE = "0"
ODA_AUDIT = "1"

# Regex для версий
VERSION_REGEX = r"_[Вв](\d+)_|_[Вв](\d+)$"

# IO Watcher
IO_WATCHER_INTERVAL = 5       # секунд между замерами
IO_WATCHER_TIMEOUT = 3600     # максимальное время ожидания (1 час)
WATCHER_INITIAL_DELAY = 10    # начальная задержка перед первым замером

# GUI
LOG_POLL_INTERVAL = 100  # мс

# Статусы модулей
STATUS_SUCCESS = "SUCCESS"
STATUS_WARNING = "WARNING"
STATUS_CRITICAL = "CRITICAL"


# ─────────────────────────────────────────────
# Состояние сессии (глобальный объект)
# ─────────────────────────────────────────────

class SessionState:
    """
    Хранит состояние текущей сессии печати.
    Передаётся между модулями через оркестратор.
    """
    def __init__(self):
        self.map_data: dict = {}           # map.json в памяти
        self.m4_status: str = ""           # статус М4: SUCCESS/WARNING/CRITICAL
        self.m4_expected_pages: int = 0
        self.m4_actual_pages: int = 0
        self.print_session_version: str = ""
        self.dsd_path: str = ""


# Глобальный объект сессии
_session = SessionState()


# ─────────────────────────────────────────────
# Вспомогательные функции оркестратора
# ─────────────────────────────────────────────

def _get_core_dir() -> Path:
    """Возвращает директорию, где находится orchestrator.py."""
    return Path(__file__).parent


def _load_module(module_name: str):
    """
    Динамически загружает модуль ядра по имени файла.
    Каждый вызов создаёт свежий экземпляр модуля.
    """
    core_dir = _get_core_dir()
    module_path = core_dir / f"{module_name}.py"

    if not module_path.exists():
        raise FileNotFoundError(f"Модуль ядра не найден: {module_path}")

    spec = importlib.util.spec_from_file_location(module_name, str(module_path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _get_paths(exe_dir: str) -> dict:
    """
    Вычисляет все рабочие пути сессии из расположения print.exe.

    Структура:
        exe_dir = /ИМЯ_ПРОЕКТА/!_Печать/
        project_dir = /ИМЯ_ПРОЕКТА/
    """
    exe = Path(exe_dir)
    print_dir = exe          # /!_Печать/
    project_dir = exe.parent  # /ИМЯ_ПРОЕКТА/

    return {
        "exe_dir": exe,
        "print_dir": print_dir,
        "project_dir": project_dir,
        "project_name": project_dir.name,
        "tmp_dir": print_dir / TMP_DIR_NAME,
        "tmp_dwg_dir": print_dir / TMP_DIR_NAME / "dwg",
        "tmp_dxf_dir": print_dir / TMP_DIR_NAME / "dxf",
        "monolithic_pdf": print_dir / TMP_DIR_NAME / MONOLITHIC_PDF_NAME,
        "map_json": print_dir / TMP_DIR_NAME / MAP_JSON_NAME,
        "ready_dir": print_dir / READY_DIR_NAME,
        "versions_dir": print_dir / "Версии",
        "dsd_dir": print_dir / DSD_DIR_NAME,
    }


def _clean_tmp(paths: dict, log_queue: queue.Queue):
    """
    Полностью очищает tmp/ в начале новой сессии.
    monolithic_output.pdf сохраняется до следующего запуска
    (очищается здесь вместе со всем tmp/).
    """
    tmp_dir = paths["tmp_dir"]
    if tmp_dir.exists():
        shutil.rmtree(str(tmp_dir))
        log_queue.put({"message": "[Оркестратор] tmp/ очищена.", "color": "white"})

    # Создаём структуру tmp/
    (paths["tmp_dir"]).mkdir(parents=True, exist_ok=True)
    (paths["tmp_dwg_dir"]).mkdir(parents=True, exist_ok=True)
    (paths["tmp_dxf_dir"]).mkdir(parents=True, exist_ok=True)


# ─────────────────────────────────────────────
# Публичный API оркестратора (вызывается из GUI)
# ─────────────────────────────────────────────

def run_pipeline(active_paths: list, exe_dir: str, log_queue: queue.Queue):
    """
    Сквозной запуск М1 → М5.
    Останавливается при CRITICAL статусе любого модуля.

    Вызывается из GUI в отдельном потоке.
    """
    global _session
    _session = SessionState()

    paths = _get_paths(exe_dir)
    log_queue.put({"message": "=" * 60, "color": "white"})
    log_queue.put({"message": f"[Pipeline] Старт. Проект: {paths['project_name']}", "color": "white"})
    log_queue.put({"message": f"[Pipeline] Объектов к печати: {len(active_paths)}", "color": "white"})

    # Очистка tmp/ в начале сессии
    _clean_tmp(paths, log_queue)

    # М2: Version Filter & Layout Mapper
    log_queue.put({"message": "[Pipeline] → Модуль 2: Version Filter & Layout Mapper", "color": "white"})
    m2 = _load_module("module2_version_filter")
    status = m2.run(active_paths, paths, _session, log_queue)
    if status == STATUS_CRITICAL:
        log_queue.put({"message": "[Pipeline] ✗ Критическая ошибка в М2. Pipeline остановлен.", "color": "red"})
        return

    # М3: DSD Generator
    log_queue.put({"message": "[Pipeline] → Модуль 3: DSD Generator", "color": "white"})
    m3 = _load_module("module3_dsd_generator")
    status = m3.run(paths, _session, log_queue)
    if status == STATUS_CRITICAL:
        log_queue.put({"message": "[Pipeline] ✗ Критическая ошибка в М3. Pipeline остановлен.", "color": "red"})
        return

    # М4: AutoCAD Print Engine
    log_queue.put({"message": "[Pipeline] → Модуль 4: AutoCAD Print Engine", "color": "white"})
    m4 = _load_module("module4_print_engine")
    status = m4.run(paths, _session, log_queue)
    if status == STATUS_CRITICAL:
        log_queue.put({"message": "[Pipeline] ✗ Критическая ошибка в М4. Pipeline остановлен.", "color": "red"})
        return

    # М5: PDF Distributor
    log_queue.put({"message": "[Pipeline] → Модуль 5: PDF Distributor", "color": "white"})
    m5 = _load_module("module5_pdf_distributor")
    m5.run(paths, _session, log_queue)

    log_queue.put({"message": "[Pipeline] ✓ Pipeline завершён.", "color": "white"})
    log_queue.put({"message": "=" * 60, "color": "white"})


def run_module1(exe_dir: str, log_queue: queue.Queue):
    """Пошаговый запуск М1 (информационный — сканирование уже в GUI)."""
    paths = _get_paths(exe_dir)
    log_queue.put({"message": f"[М1] Проект: {paths['project_name']}", "color": "white"})
    log_queue.put({"message": f"[М1] Директория печати: {paths['print_dir']}", "color": "white"})
    log_queue.put({"message": "[М1] Сканирование выполняется в таблице GUI.", "color": "white"})


def run_module2(active_paths: list, exe_dir: str, log_queue: queue.Queue):
    """Пошаговый запуск М2."""
    global _session
    _session = SessionState()
    paths = _get_paths(exe_dir)
    _clean_tmp(paths, log_queue)

    m2 = _load_module("module2_version_filter")
    m2.run(active_paths, paths, _session, log_queue)


def run_module3(exe_dir: str, log_queue: queue.Queue):
    """Пошаговый запуск М3 (требует предварительного М2)."""
    paths = _get_paths(exe_dir)
    # Загружаем map.json из tmp/ если он есть
    _load_map_from_disk(paths, log_queue)

    m3 = _load_module("module3_dsd_generator")
    m3.run(paths, _session, log_queue)


def run_module4(exe_dir: str, log_queue: queue.Queue):
    """Пошаговый запуск М4 (требует предварительного М3)."""
    paths = _get_paths(exe_dir)
    _load_map_from_disk(paths, log_queue)

    m4 = _load_module("module4_print_engine")
    m4.run(paths, _session, log_queue)


def run_module5(exe_dir: str, log_queue: queue.Queue):
    """Пошаговый запуск М5 (требует предварительного М4)."""
    paths = _get_paths(exe_dir)
    _load_map_from_disk(paths, log_queue)

    m5 = _load_module("module5_pdf_distributor")
    m5.run(paths, _session, log_queue)


def _load_map_from_disk(paths: dict, log_queue: queue.Queue):
    """Загружает map.json с диска в _session при пошаговом запуске."""
    map_path = paths["map_json"]
    if map_path.exists():
        try:
            with open(str(map_path), "r", encoding="utf-8") as f:
                _session.map_data = json.load(f)
            version = _session.map_data.get("print_session_version", "")
            if version:
                _session.print_session_version = version
            log_queue.put({"message": f"[Оркестратор] map.json загружен из {map_path}", "color": "white"})
        except Exception as e:
            log_queue.put({"message": f"[Оркестратор] Ошибка чтения map.json: {e}", "color": "red"})
    else:
        log_queue.put({"message": "[Оркестратор] map.json не найден. Запустите М2 сначала.", "color": "yellow"})