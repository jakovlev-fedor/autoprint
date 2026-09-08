"""
module5_pdf_distributor.py — PDF Distributor.

Вход:  tmp/monolithic_output.pdf + tmp/map.json + session.m4_status
Выход: /!_Печать/Версии/autoprint_v[N]/ + /!_Печать/! ГОТОВО/

Зависимости:
    pip install pypdf
"""

import os
import json
import shutil
import queue
from pathlib import Path

from orchestrator import (
    MONOLITHIC_PDF_NAME, MAP_JSON_NAME,
    READY_DIR_NAME, VERSION_DIR_PREFIX,
    STATUS_SUCCESS, STATUS_WARNING, STATUS_CRITICAL
)

try:
    from pypdf import PdfReader, PdfWriter
    PYPDF_AVAILABLE = True
except ImportError:
    PYPDF_AVAILABLE = False


def run(paths: dict, session, log_queue: queue.Queue) -> str:
    """
    Основная функция М5.

    Алгоритм:
        1. Проверить статус М4 (блокировка при WARNING)
        2. Нарезать monolithic_output.pdf по drawing-ам
        3. Сохранить в Версии/autoprint_v[N]/
        4. Обновить ! ГОТОВО/
        5. Скопировать map.json в архив
    """
    log_queue.put({"message": "[М5] Старт: PDF Distributor", "color": "white"})

    if not PYPDF_AVAILABLE:
        log_queue.put({"message": "[М5] КРИТИЧНО: pypdf не установлен.", "color": "red"})
        return STATUS_CRITICAL

    # Блокировка при WARNING от М4
    if session.m4_status == STATUS_WARNING:
        log_queue.put({
            "message": (
                f"[М5] Запуск заблокирован: несовпадение количества страниц в М4. "
                f"Ожидалось: {session.m4_expected_pages}. "
                f"Получено: {session.m4_actual_pages}. "
                f"Проверьте {MONOLITHIC_PDF_NAME} вручную."
            ),
            "color": "red"
        })
        return STATUS_CRITICAL

    # Проверка monolithic_output.pdf
    monolithic_pdf = paths["monolithic_pdf"]
    if not monolithic_pdf.exists():
        log_queue.put({
            "message": f"[М5] КРИТИЧНО: {MONOLITHIC_PDF_NAME} не найден.",
            "color": "red"
        })
        return STATUS_CRITICAL

    # Проверка map.json
    if not session.map_data or not session.map_data.get("drawings"):
        log_queue.put({"message": "[М5] КРИТИЧНО: map.json пуст или не загружен.", "color": "red"})
        return STATUS_CRITICAL

    # Версия сессии
    version_str = session.print_session_version
    if not version_str:
        log_queue.put({"message": "[М5] КРИТИЧНО: Версия сессии не определена.", "color": "red"})
        return STATUS_CRITICAL

    # Создание папки архива версии
    version_dir = paths["versions_dir"] / version_str
    version_dir.mkdir(parents=True, exist_ok=True)

    # Нарезка PDF
    try:
        reader = PdfReader(str(monolithic_pdf))
    except Exception as e:
        log_queue.put({"message": f"[М5] КРИТИЧНО: Ошибка открытия PDF: {e}", "color": "red"})
        return STATUS_CRITICAL

    total_files = 0
    total_sheets = 0
    page_cursor = 0

    drawings = session.map_data.get("drawings", [])

    for drawing in drawings:
        pages_count = drawing.get("expected_layouts_count", 0)
        if pages_count == 0:
            continue

        request_code = drawing.get("request_code", "unknown")
        version_index = drawing.get("version_index", 0)
        object_folder = drawing.get("object_folder", "Объект")

        # Маска имени выходного файла: [request_code]_В[version_index].pdf
        output_filename = f"{request_code}_В{version_index}.pdf"

        # Извлечение страниц
        writer = PdfWriter()
        end_page = page_cursor + pages_count

        if end_page > len(reader.pages):
            log_queue.put({
                "message": (
                    f"[М5] ВНИМАНИЕ: {output_filename} — "
                    f"запрошено страниц {pages_count}, доступно {len(reader.pages) - page_cursor}."
                ),
                "color": "yellow"
            })
            end_page = len(reader.pages)

        for page_idx in range(page_cursor, end_page):
            writer.add_page(reader.pages[page_idx])

        page_cursor += pages_count

        # Сохранение в Версии/autoprint_v[N]/[object_folder]/
        version_obj_dir = version_dir / object_folder
        version_obj_dir.mkdir(parents=True, exist_ok=True)

        version_pdf_path = version_obj_dir / output_filename
        saved_to_version = _save_pdf(writer, version_pdf_path, output_filename, log_queue)

        if saved_to_version:
            total_files += 1
            total_sheets += pages_count

    # Обновление ! ГОТОВО/
    _update_ready_dir(paths, version_dir, log_queue)

    # Копирование map.json в архив
    try:
        import json as json_module
        archive_map = version_dir / MAP_JSON_NAME
        with open(str(archive_map), "w", encoding="utf-8") as f:
            json_module.dump(session.map_data, f, ensure_ascii=False, indent=2)
        log_queue.put({"message": f"[М5] map.json скопирован в архив: {archive_map}", "color": "white"})
    except Exception as e:
        log_queue.put({"message": f"[М5] Ошибка копирования map.json: {e}", "color": "yellow"})

    # Итоговый лог
    log_queue.put({
        "message": (
            f"[М5] Сессия {version_str} завершена. "
            f"Файлов: {total_files}. Листов: {total_sheets}."
        ),
        "color": "white"
    })

    # tmp/ НЕ очищается — будет очищена при следующем запуске Pipeline
    log_queue.put({"message": "[М5] tmp/ сохранена для ручного восстановления.", "color": "white"})

    return STATUS_SUCCESS


def _save_pdf(writer: "PdfWriter", output_path: Path, filename: str, log_queue: queue.Queue) -> bool:
    """
    Сохраняет PDF файл. Возвращает True при успехе.
    При PermissionError — пропускает файл с красным логом (некритическая ошибка).
    """
    try:
        with open(str(output_path), "wb") as f:
            writer.write(f)
        log_queue.put({"message": f"[М5] Сохранён: {output_path}", "color": "white"})
        return True
    except PermissionError as e:
        log_queue.put({
            "message": f"[М5] Нет прав на запись {filename}: {e}. Файл пропущен.",
            "color": "red"
        })
        return False
    except Exception as e:
        log_queue.put({
            "message": f"[М5] Ошибка сохранения {filename}: {e}. Файл пропущен.",
            "color": "red"
        })
        return False


def _update_ready_dir(paths: dict, version_dir: Path, log_queue: queue.Queue):
    """
    Полностью перезаписывает папку ! ГОТОВО/:
        1. Удаляет старое содержимое
        2. Копирует из Версии/autoprint_v[N]/
    """
    ready_dir = paths["ready_dir"]

    # Очистка ! ГОТОВО/
    if ready_dir.exists():
        try:
            shutil.rmtree(str(ready_dir))
            log_queue.put({"message": "[М5] ! ГОТОВО/ очищена.", "color": "white"})
        except Exception as e:
            log_queue.put({"message": f"[М5] Ошибка очистки ! ГОТОВО/: {e}", "color": "yellow"})

    ready_dir.mkdir(parents=True, exist_ok=True)

    # Копирование из архива версии (без map.json)
    try:
        for item in version_dir.iterdir():
            if item.is_dir():
                dest = ready_dir / item.name
                try:
                    shutil.copytree(str(item), str(dest))
                    log_queue.put({"message": f"[М5] ! ГОТОВО/{item.name}/ обновлена.", "color": "white"})
                except PermissionError as e:
                    log_queue.put({
                        "message": f"[М5] Нет прав на запись в ! ГОТОВО/{item.name}/: {e}. Пропущено.",
                        "color": "red"
                    })
                except Exception as e:
                    log_queue.put({
                        "message": f"[М5] Ошибка копирования в ! ГОТОВО/{item.name}/: {e}.",
                        "color": "red"
                    })
    except Exception as e:
        log_queue.put({"message": f"[М5] Ошибка обновления ! ГОТОВО/: {e}", "color": "red"})