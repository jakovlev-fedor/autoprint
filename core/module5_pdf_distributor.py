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

    # Нарезка PDF - цикл нарезки в пдф и сохранения в архив версии сессии ----------------------------------------------
    for drawing in drawings:
        pages_count = drawing.get("expected_layouts_count", 0)
        if pages_count == 0:
            continue

        request_code = drawing.get("request_code", "unknown")
        version_index = drawing.get("version_index", 0)
        object_folder = drawing.get("object_folder", "Объект")

        # Маска имени выходного файла: [request_code]_В[version_index].pdf
        output_filename = f"{object_folder}_В{version_index}_{request_code}.pdf"

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

        # Сохранение в Версии/autoprint_v[N]/[object_folder]/ --------------------------------------------------------
        version_obj_dir = version_dir / object_folder

        version_obj_dir.mkdir(parents=True, exist_ok=True)

        version_pdf_path = version_obj_dir / output_filename

        saved_to_version = _save_pdf(writer, version_pdf_path, output_filename, log_queue)

        if saved_to_version:
            total_files += 1
            total_sheets += pages_count
    # Конец нарезки в PDF - цикл нарезки в пдф и сохранения в архив версии сессии -------------------------------------

    # Обновление ! ГОТОВО/
    _update_ready_dir(paths, version_dir, log_queue)

    # Копирование в папки объектов
    _copy_to_object_folders(paths, version_dir, log_queue)

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
    Копирует PDF файлы из Версии/autoprint_v[N]/ в ! ГОТОВО/ без вложенных папок.
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
    
    # Копирование PDF файлов из архива версии (рекурсивно, без структуры папок)
    try:
        for pdf_file in version_dir.rglob("*.pdf"):
            dest = ready_dir / pdf_file.name
            try:
                shutil.copy2(str(pdf_file), str(dest))
                log_queue.put({"message": f"[М5] ! ГОТОВО/{pdf_file.name} обновлён.", "color": "white"})
            except Exception as e:
                log_queue.put({
                    "message": f"[М5] Ошибка копирования {pdf_file.name}: {e}.",
                    "color": "red"
                })
    except Exception as e:
        log_queue.put({"message": f"[М5] Ошибка обновления ! ГОТОВО/: {e}", "color": "red"})


def _copy_to_object_folders(paths: dict, version_dir: Path, log_queue: queue.Queue):
    """
    Копирует PDF файлы в папки объектов:
    - {object_folder}/Рабочая/ — перезапись существующих
    - {object_folder}/Согласовано/ — очистка и запись только новых
    """
    # Используем project_dir вместо project_path
    project_dir = paths.get("project_dir")
    if not project_dir:
        log_queue.put({"message": "[М5] ПРЕДУПРЕЖДЕНИЕ: project_dir не найден в paths.", "color": "yellow"})
        return
    
    for pdf_file in version_dir.rglob("*.pdf"):
        object_folder = pdf_file.parent.name
        filename = pdf_file.name
        
        # Копия в Рабочую (перезапись)
        working_dir = Path(project_dir) / object_folder / "Рабочая"
        if working_dir.exists():
            try:
                shutil.copy2(str(pdf_file), str(working_dir / filename))
                log_queue.put({"message": f"[М5] Рабочая/{object_folder}/{filename} обновлён.", "color": "white"})
            except Exception as e:
                log_queue.put({
                    "message": f"[М5] Ошибка копирования в Рабочую/{object_folder}: {e}.",
                    "color": "yellow"
                })
        else:
            log_queue.put({
                "message": f"[М5] ПРЕДУПРЕЖДЕНИЕ: Папка Рабочая не найдена для {object_folder}.",
                "color": "yellow"
            })
        
        # Копия в Согласовано (с очисткой)
        agreed_dir = Path(project_dir) / object_folder / "Согласовано"
        
        # Очистка папки Согласовано перед записью
        if agreed_dir.exists():
            try:
                shutil.rmtree(str(agreed_dir))
                log_queue.put({"message": f"[М5] Согласовано/{object_folder}/ очищена.", "color": "white"})
            except Exception as e:
                log_queue.put({
                    "message": f"[М5] Ошибка очистки Согласовано/{object_folder}: {e}.",
                    "color": "yellow"
                })
        
        agreed_dir.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(str(pdf_file), str(agreed_dir / filename))
            log_queue.put({"message": f"[М5] Согласовано/{object_folder}/{filename} обновлён.", "color": "white"})
        except Exception as e:
            log_queue.put({
                "message": f"[М5] Ошибка копирования в Согласовано/{object_folder}: {e}.",
                "color": "yellow"
            })