"""
module3_dsd_generator.py — DSD Generator Engine.

Вход:  tmp/map.json (через session.map_data)
Выход: /!_Печать/.dsd файлы/[project_name]_autoprint_v[N].dsd

КРИТИЧНО:
    - Кодировка строго cp1251
    - Пути с двойным обратным слэшем \\
    - configparser ЗАПРЕЩЁН (приводит ключи к lowercase)
    - resolve() ЗАПРЕЩЁН (разворачивает буквенные диски в UNC)
"""

import os
import re
import queue
from pathlib import Path, PureWindowsPath

from orchestrator import (
    PAGE_SETUP_NAME, ACAD_PROFILE,
    MONOLITHIC_PDF_NAME, DSD_DIR_NAME,
    VERSION_DIR_PREFIX,
    MAP_JSON_NAME,
    STATUS_SUCCESS, STATUS_CRITICAL
)


def run(paths: dict, session, log_queue: queue.Queue) -> str:
    """
    Основная функция М3.

    Алгоритм:
        1. Инкремент версии сессии
        2. Обновить map.json: print_session_version
        3. Сгенерировать .dsd файл
        4. Сохранить в .dsd файлы/
    """
    log_queue.put({"message": "[М3] Старт: DSD Generator", "color": "white"})

    if not session.map_data:
        log_queue.put({"message": "[М3] КРИТИЧНО: map.json пуст. Запустите М2.", "color": "red"})
        return STATUS_CRITICAL

    # Шаг 1: Инкремент версии
    version_str = _get_next_version(paths["versions_dir"], log_queue)
    session.print_session_version = version_str
    session.map_data["print_session_version"] = version_str

    log_queue.put({"message": f"[М3] Версия сессии: {version_str}", "color": "white"})

    # Шаг 2: Обновить map.json на диске
    import json
    map_path = paths["map_json"]
    try:
        with open(str(map_path), "w", encoding="utf-8") as f:
            json.dump(session.map_data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log_queue.put({"message": f"[М3] КРИТИЧНО: Ошибка обновления map.json: {e}", "color": "red"})
        return STATUS_CRITICAL

    # Шаг 3: Генерация .dsd
    dsd_content = _build_dsd(session.map_data, paths)

    # Шаг 4: Сохранение
    dsd_dir = paths["dsd_dir"]
    dsd_dir.mkdir(parents=True, exist_ok=True)

    if not os.access(str(dsd_dir), os.W_OK):
        log_queue.put({"message": f"[М3] КРИТИЧНО: {dsd_dir} недоступна для записи.", "color": "red"})
        return STATUS_CRITICAL

    project_name = session.map_data.get("project_name", "project")
    dsd_filename = f"{project_name}_{version_str}.dsd"
    dsd_path = dsd_dir / dsd_filename

    try:
        # КРИТИЧНО: кодировка cp1251
        with open(str(dsd_path), "w", encoding="cp1251") as f:
            f.write(dsd_content)
        log_queue.put({"message": f"[М3] .dsd сохранён: {dsd_path}", "color": "white"})
    except UnicodeEncodeError as e:
        log_queue.put({"message": f"[М3] КРИТИЧНО: Ошибка кодировки cp1251: {e}", "color": "red"})
        return STATUS_CRITICAL
    except Exception as e:
        log_queue.put({"message": f"[М3] КРИТИЧНО: Ошибка записи .dsd: {e}", "color": "red"})
        return STATUS_CRITICAL

    # Сохраняем путь к .dsd в сессии для М4
    session.dsd_path = str(dsd_path)

    log_queue.put({"message": f"[М3] Завершено. Листов в .dsd: {_count_sheets(session.map_data)}", "color": "white"})
    return STATUS_SUCCESS


def _get_next_version(versions_dir: Path, log_queue: queue.Queue) -> str:
    """
    Определяет следующий номер версии сессии.

    Сканирует /Версии/ на наличие папок autoprint_v[N].
    Возвращает строку вида "autoprint_v3".
    """
    versions_dir.mkdir(parents=True, exist_ok=True)
    max_n = 0

    try:
        with os.scandir(str(versions_dir)) as it:
            for entry in it:
                if entry.is_dir():
                    match = re.match(
                        r'^' + re.escape(VERSION_DIR_PREFIX) + r'(\d+)$',
                        entry.name
                    )
                    if match:
                        n = int(match.group(1))
                        if n > max_n:
                            max_n = n
    except Exception as e:
        log_queue.put({"message": f"[М3] Ошибка сканирования Версии/: {e}", "color": "yellow"})

    next_n = max_n + 1
    return f"{VERSION_DIR_PREFIX}{next_n}"


def _to_dsd_path(path_str: str) -> str:
    """
    Преобразует путь в формат для .dsd файла:
        - Одинарный обратный слэш \
        - resolve() ЗАПРЕЩЁН
        - Буква диска сохраняется

    Пример: J:/Path/To/File.dwg → J:\Path\To\File.dwg
    """
    # Заменяем прямые слэши на обратные
    result = path_str.replace("/", "\\")
    # Нормализуем: заменяем множественные \\ на одиночный \
    while "\\\\" in result:
        result = result.replace("\\\\", "\\")
    return result

def _build_dsd(map_data: dict, paths: dict) -> str:
    """
    Собирает содержимое .dsd файла как plain text.

    configparser ЗАПРЕЩЁН — принудительно приводит ключи к lowercase.
    Используется строковая конкатенация через список строк.
    """
    lines = []

    # ── Часть А: Заголовок (фиксированный) ──
    lines.append("[DWF6Version]")
    lines.append("Ver=1")
    lines.append("[DWF6MinorVersion]")
    lines.append("MinorVer=1")

    # ── Часть Б: Динамические секции листов ──
    for drawing in map_data.get("drawings", []):
        dwg_path = _to_dsd_path(drawing["dwg_absolute_path"])
        dwg_stem = Path(drawing["dwg_filename"]).stem

        for layout_name in drawing.get("layout_names", []):
            # Заголовок секции: [DWF6Sheet:ИмяФайла-ИмяLayout]
            section_name = f"{dwg_stem}-{layout_name}"
            lines.append(f"[DWF6Sheet:{section_name}]")
            lines.append(f"DWG={dwg_path}")
            lines.append(f"Layout={layout_name}")
            lines.append(f"Setup={PAGE_SETUP_NAME}")
            lines.append(f"OriginalSheetPath={dwg_path}")
            lines.append("Has Plot Port=0")
            lines.append("Has3DDWF=0")

    # ── Часть В: Глобальные параметры (фиксированные) ──
    monolithic_pdf = _to_dsd_path(str(paths["monolithic_pdf"]))
    tmp_dir = _to_dsd_path(str(paths["tmp_dir"]) + "\\")

    lines.append("[Target]")
    lines.append("Type=6")
    lines.append(f"DWF={monolithic_pdf}")
    lines.append(f"OUT={tmp_dir}")
    lines.append("PWD=")
    lines.append("[MRU Local]")
    lines.append("MRU=0")
    lines.append("[MRU Sheet List]")
    lines.append("MRU=0")
    lines.append("[PdfOptions]")
    lines.append("IncludeHyperlinks=TRUE")
    lines.append("CreateBookmarks=TRUE")
    lines.append("CaptureFontsInDrawing=TRUE")
    lines.append("ConvertTextToGeometry=FALSE")
    lines.append("VectorResolution=1200")
    lines.append("RasterResolution=400")
    lines.append("[AutoCAD Block Data]")
    lines.append("IncludeBlockInfo=0")
    lines.append("BlockTmplFilePath=")
    lines.append("[SheetSet Properties]")
    lines.append("IsSheetSet=FALSE")
    lines.append("IsHomogeneous=FALSE")
    lines.append("SheetSet Name=")
    lines.append("NoOfCopies=1")
    lines.append("PlotStampOn=FALSE")
    lines.append("ViewFile=TRUE")
    lines.append("JobID=0")
    lines.append("SelectionSetName=")
    lines.append(f"AcadProfile={ACAD_PROFILE}")
    lines.append("CategoryName=")
    lines.append("LogFilePath=")
    lines.append("IncludeLayer=TRUE")
    lines.append("LineMerge=FALSE")
    lines.append("CurrentPrecision=")
    lines.append("PromptForDwfName=FALSE")
    lines.append("PwdProtectPublishedDWF=FALSE")
    lines.append("PromptForPwd=FALSE")
    lines.append("RepublishingMarkups=FALSE")
    lines.append("PublishSheetSetMetadata=FALSE")
    lines.append("PublishSheetMetadata=FALSE")
    lines.append("3DDWFOptions=0 1")

    # Соединяем с переносом строки (Windows CRLF для совместимости с AutoCAD)
    return "\r\n".join(lines) + "\r\n"


def _count_sheets(map_data: dict) -> int:
    """Подсчитывает общее количество листов в map.json."""
    return sum(
        d.get("expected_layouts_count", 0)
        for d in map_data.get("drawings", [])
    )