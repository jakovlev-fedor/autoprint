"""
module2_version_filter.py — Version Filter & Layout Mapper.

Вход:  list[str] — активные пути объектов из GUI
Выход: tmp/map.json + дубликат в session.map_data

Зависимости:
    pip install ezdxf
    ODA File Converter установлен по пути ODA_PATH
"""

import os
import re
import json
import shutil
import queue
import subprocess
from pathlib import Path
from collections import defaultdict

# Импорт констант из оркестратора (доступны через sys.path)
from orchestrator import (
    WORKING_DIR_NAME, VERSION_REGEX, MAP_JSON_NAME,
    ODA_PATH, ODA_OUTPUT_VERSION, ODA_OUTPUT_FORMAT,
    ODA_RECURSE, ODA_AUDIT,
    STATUS_SUCCESS, STATUS_CRITICAL, STATUS_WARNING
)

try:
    import ezdxf
    EZDXF_AVAILABLE = True
except ImportError:
    EZDXF_AVAILABLE = False


def run(active_paths: list, paths: dict, session, log_queue: queue.Queue) -> str:
    """
    Основная функция М2.

    Алгоритм:
        1. Для каждого объекта → найти папку "Рабочая"
        2. Отобрать .dwg с максимальными версиями
        3. Скопировать в tmp/dwg/
        4. Конвертировать через ODA → tmp/dxf/
        5. Прочитать Layout-ы через ezdxf
        6. Собрать map.json
    """
    log_queue.put({"message": "[М2] Старт: Version Filter & Layout Mapper", "color": "white"})

    # Проверка ODA
    if not os.path.isfile(ODA_PATH):
        log_queue.put({
            "message": f"[М2] КРИТИЧНО: ODA File Converter не найден: {ODA_PATH}",
            "color": "red"
        })
        return STATUS_CRITICAL

    # Проверка доступности tmp/dxf/
    tmp_dxf_dir = paths["tmp_dxf_dir"]
    if not os.access(str(tmp_dxf_dir.parent), os.W_OK):
        log_queue.put({"message": "[М2] КРИТИЧНО: tmp/ недоступна для записи.", "color": "red"})
        return STATUS_CRITICAL

    project_name = paths["project_name"]
    drawings = []

    for obj_path_str in active_paths:
        obj_path = Path(obj_path_str)
        obj_result = _process_object(obj_path, paths, log_queue)
        drawings.extend(obj_result)

    if not drawings:
        log_queue.put({"message": "[М2] КРИТИЧНО: Ни одного валидного чертежа не найдено.", "color": "red"})
        return STATUS_CRITICAL

    # Конвертация через ODA (один вызов на всю папку tmp/dwg/)
    oda_status = _run_oda_conversion(paths, log_queue)
    if oda_status == STATUS_CRITICAL:
        return STATUS_CRITICAL

    # Чтение Layout-ов через ezdxf
    drawings = _read_layouts(drawings, paths, log_queue)

    # Финальная проверка
    valid_drawings = [d for d in drawings if d.get("layout_names")]
    if not valid_drawings:
        log_queue.put({"message": "[М2] КРИТИЧНО: Ни одного чертежа с Layout-ами.", "color": "red"})
        return STATUS_CRITICAL

    # Сборка map.json
    map_data = {
        "project_name": project_name,
        "print_session_version": "",  # заполнит М3
        "drawings": valid_drawings
    }

    # Сохранение на диск
    map_path = paths["map_json"]
    try:
        with open(str(map_path), "w", encoding="utf-8") as f:
            json.dump(map_data, f, ensure_ascii=False, indent=2)
        log_queue.put({"message": f"[М2] map.json сохранён: {map_path}", "color": "white"})
    except Exception as e:
        log_queue.put({"message": f"[М2] КРИТИЧНО: Ошибка записи map.json: {e}", "color": "red"})
        return STATUS_CRITICAL

    # Дубликат в памяти оркестратора
    session.map_data = map_data

    log_queue.put({
        "message": f"[М2] Завершено. Чертежей: {len(valid_drawings)}.",
        "color": "white"
    })
    return STATUS_SUCCESS


def _process_object(obj_path: Path, paths: dict, log_queue: queue.Queue) -> list:
    """
    Обрабатывает один объект:
        - Проверяет наличие папки "Рабочая"
        - Отбирает .dwg с максимальными версиями
        - Копирует в tmp/dwg/
    """
    working_dir = obj_path / WORKING_DIR_NAME

    if not working_dir.is_dir():
        log_queue.put({
            "message": f'[М2] Объект "{obj_path.name}" — папка "Рабочая" не найдена. Объект пропущен.',
            "color": "yellow"
        })
        return []

    # Определяем object_folder согласно ТЗ п.7.6
    # Для автосканирования и +Проект: path.name
    # Для +Объект: path.parent.name
    # Здесь используем имя папки объекта
    object_folder = obj_path.name

    # Сканируем .dwg файлы
    dwg_files = _scan_dwg_files(working_dir, log_queue)
    if not dwg_files:
        log_queue.put({
            "message": f'[М2] Объект "{obj_path.name}" — .dwg файлы не найдены.',
            "color": "yellow"
        })
        return []

    # Отбор максимальных версий
    selected = _select_max_versions(dwg_files, obj_path.name, log_queue)

    # Копирование в tmp/dwg/
    drawings = []
    tmp_dwg_dir = paths["tmp_dwg_dir"]

    for dwg_path, request_code, version_index in selected:
        dest = tmp_dwg_dir / dwg_path.name
        try:
            shutil.copy2(str(dwg_path), str(dest))
            log_queue.put({
                "message": f"[М2] Скопирован: {dwg_path.name} → tmp/dwg/",
                "color": "white"
            })
            drawings.append({
                "dwg_absolute_path": str(dwg_path),
                "dwg_filename": dwg_path.name,
                "object_folder": object_folder,
                "request_code": request_code,
                "version_index": version_index,
                "layout_names": [],        # заполнит _read_layouts
                "expected_layouts_count": 0
            })
        except Exception as e:
            log_queue.put({
                "message": f"[М2] Ошибка копирования {dwg_path.name}: {e}",
                "color": "red"
            })

    return drawings


def _scan_dwg_files(working_dir: Path, log_queue: queue.Queue) -> list:
    """
    Сканирует папку "Рабочая" на наличие .dwg файлов.
    Без рекурсии — только первый уровень.
    """
    dwg_files = []
    try:
        with os.scandir(str(working_dir)) as it:
            for entry in it:
                if entry.is_file() and entry.name.lower().endswith(".dwg"):
                    dwg_files.append(Path(entry.path))
    except PermissionError as e:
        log_queue.put({"message": f"[М2] Нет доступа к {working_dir}: {e}", "color": "red"})
    return dwg_files


def _extract_request_code(filename: str) -> str:
    """
    Извлекает код заявки из имени файла.
    Формат: XXX-X-X-XXX-XXXX (машиногенерируемый идентификатор).

    Паттерн ищет последовательность вида: LUK-S-P-RUS-0429
    """
    # Паттерн для кода заявки: группы букв/цифр, разделённые дефисами
    pattern = r'[A-Z]{2,}-[A-Z]-[A-Z]-[A-Z]{2,}-\d{4,}'
    match = re.search(pattern, filename, re.IGNORECASE)
    if match:
        return match.group(0).upper()
    # Если стандартный паттерн не найден — используем имя файла без расширения
    return Path(filename).stem


def _extract_version(filename: str) -> int:
    """
    Извлекает номер версии из имени файла через VERSION_REGEX.
    Возвращает 0 если версия не найдена.
    """
    match = re.search(VERSION_REGEX, filename)
    if match:
        # Группа 1 или группа 2 (версия в середине или в конце)
        v = match.group(1) or match.group(2)
        return int(v)
    return 0


def _select_max_versions(
    dwg_files: list,
    obj_name: str,
    log_queue: queue.Queue
) -> list:
    """
    Группирует файлы по коду заявки и отбирает максимальную версию.

    Возвращает: list of (Path, request_code, version_index)
    """
    # Группировка: {request_code: [(version, path), ...]}
    groups: dict = defaultdict(list)

    for dwg_path in dwg_files:
        code = _extract_request_code(dwg_path.name)
        version = _extract_version(dwg_path.name)
        groups[code].append((version, dwg_path))

    selected = []
    for code, versions in groups.items():
        # Сортируем по версии по убыванию
        versions.sort(key=lambda x: x[0], reverse=True)
        max_version, max_path = versions[0]

        # Логируем пропущенные версии
        for ver, path in versions[1:]:
            log_queue.put({
                "message": f'[М2] Объект "{obj_name}" — пропущена старая версия: {path.name} (В{ver})',
                "color": "white"
            })

        selected.append((max_path, code, max_version))
        log_queue.put({
            "message": f'[М2] Объект "{obj_name}" — отобран: {max_path.name} (В{max_version})',
            "color": "white"
        })

    return selected


def _run_oda_conversion(paths: dict, log_queue: queue.Queue) -> str:
    """
    Запускает ODA File Converter для конвертации всех .dwg из tmp/dwg/ в tmp/dxf/.
    Один вызов на всю папку.
    """
    tmp_dwg_dir = paths["tmp_dwg_dir"]
    tmp_dxf_dir = paths["tmp_dxf_dir"]

    log_queue.put({"message": "[М2] Запуск ODA File Converter...", "color": "white"})

    cmd = [
        ODA_PATH,
        str(tmp_dwg_dir),   # входная папка
        str(tmp_dxf_dir),   # выходная папка
        ODA_OUTPUT_VERSION, # "ACAD2018"
        ODA_OUTPUT_FORMAT,  # "DXF"
        ODA_RECURSE,        # "0" — без рекурсии
        ODA_AUDIT           # "1" — аудит файлов
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300  # 5 минут максимум
        )
        log_queue.put({"message": f"[М2] ODA завершён. Код: {result.returncode}", "color": "white"})

        if result.returncode != 0:
            log_queue.put({
                "message": f"[М2] ODA stderr: {result.stderr[:500]}",
                "color": "yellow"
            })

    except subprocess.TimeoutExpired:
        log_queue.put({"message": "[М2] КРИТИЧНО: ODA превысил таймаут (300с).", "color": "red"})
        return STATUS_CRITICAL
    except Exception as e:
        log_queue.put({"message": f"[М2] КРИТИЧНО: Ошибка запуска ODA: {e}", "color": "red"})
        return STATUS_CRITICAL

    return STATUS_SUCCESS


def _read_layouts(drawings: list, paths: dict, log_queue: queue.Queue) -> list:
    """
    Читает список Layout-ов из .dxf файлов через ezdxf.
    Обновляет поля layout_names и expected_layouts_count в каждом drawing.
    """
    if not EZDXF_AVAILABLE:
        log_queue.put({"message": "[М2] КРИТИЧНО: ezdxf не установлен.", "color": "red"})
        return drawings

    tmp_dxf_dir = paths["tmp_dxf_dir"]

    for drawing in drawings:
        dwg_filename = drawing["dwg_filename"]
        # ODA сохраняет .dxf с тем же именем (без расширения .dwg → .dxf)
        dxf_name = Path(dwg_filename).stem + ".dxf"
        dxf_path = tmp_dxf_dir / dxf_name

        if not dxf_path.exists():
            log_queue.put({
                "message": f"[М2] .dxf не найден для {dwg_filename}. Чертёж пропущен.",
                "color": "red"
            })
            continue

        try:
            doc = ezdxf.readfile(str(dxf_path))
            # Извлекаем все Layout-ы кроме "Model" в порядке вкладок AutoCAD
            layouts = [
                name
                for name in doc.layout_names_in_taborder()
                if name != "Model"
            ]

            if not layouts:
                log_queue.put({
                    "message": f"[М2] {dwg_filename} — Layout-ы не найдены. Чертёж пропущен.",
                    "color": "yellow"
                })
                continue

            drawing["layout_names"] = layouts
            drawing["expected_layouts_count"] = len(layouts)

            log_queue.put({
                "message": f"[М2] {dwg_filename} — Layout-ов: {len(layouts)}: {', '.join(layouts)}",
                "color": "white"
            })

        except Exception as e:
            log_queue.put({
                "message": f"[М2] Ошибка чтения {dxf_name}: {e}. Чертёж пропущен.",
                "color": "red"
            })

    return drawings