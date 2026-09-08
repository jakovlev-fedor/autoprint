"""
module4_print_engine.py — AutoCAD Print Engine.

Вход:  путь к .dsd файлу (session.dsd_path) + tmp/map.json
Выход: tmp/monolithic_output.pdf + статус в session.m4_status

Зависимости:
    pip install pywin32 pypdf psutil

ВАЖНО:
    - AutoCAD должен быть установлен и лицензирован
    - Команда _-PUBLISH с префиксом _ (английская версия в русском AutoCAD)
"""

import os
import time
import queue
from pathlib import Path

from orchestrator import (
    ACAD_PROFILE, ACAD_PUBLISH_COMMAND,
    IO_WATCHER_INTERVAL, IO_WATCHER_TIMEOUT, WATCHER_INITIAL_DELAY,
    MONOLITHIC_PDF_NAME,
    STATUS_SUCCESS, STATUS_WARNING, STATUS_CRITICAL
)

try:
    import win32com.client
    WIN32_AVAILABLE = True
except ImportError:
    WIN32_AVAILABLE = False

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False

try:
    from pypdf import PdfReader
    PYPDF_AVAILABLE = True
except ImportError:
    PYPDF_AVAILABLE = False


def run(paths: dict, session, log_queue: queue.Queue) -> str:
    """
    Основная функция М4.

    Алгоритм:
        1. Проверить зависимости
        2. Запустить AutoCAD
        3. Отправить команду _-PUBLISH
        4. IO Watcher — ожидание завершения
        5. Верификация PDF
        6. Завершить AutoCAD
    """
    log_queue.put({"message": "[М4] Старт: AutoCAD Print Engine", "color": "white"})

    # Проверка зависимостей
    if not WIN32_AVAILABLE:
        log_queue.put({"message": "[М4] КРИТИЧНО: pywin32 не установлен.", "color": "red"})
        session.m4_status = STATUS_CRITICAL
        return STATUS_CRITICAL

    if not PSUTIL_AVAILABLE:
        log_queue.put({"message": "[М4] КРИТИЧНО: psutil не установлен.", "color": "red"})
        session.m4_status = STATUS_CRITICAL
        return STATUS_CRITICAL

    if not PYPDF_AVAILABLE:
        log_queue.put({"message": "[М4] КРИТИЧНО: pypdf не установлен.", "color": "red"})
        session.m4_status = STATUS_CRITICAL
        return STATUS_CRITICAL

    # Проверка .dsd файла
    dsd_path = session.dsd_path
    if not dsd_path or not os.path.isfile(dsd_path):
        log_queue.put({"message": f"[М4] КРИТИЧНО: .dsd файл не найден: {dsd_path}", "color": "red"})
        session.m4_status = STATUS_CRITICAL
        return STATUS_CRITICAL

    monolithic_pdf = paths["monolithic_pdf"]
    acad = None

    try:
        # Шаг 1: Запуск AutoCAD через COM
        log_queue.put({"message": "[М4] Запуск AutoCAD...", "color": "white"})
        acad = _start_autocad(log_queue)
        if acad is None:
            session.m4_status = STATUS_CRITICAL
            return STATUS_CRITICAL

        # Шаг 2: Отправка команды печати
        # Путь к .dsd в кавычках для передачи через SendCommand
        # Обратные слэши экранируются для AutoCAD
        dsd_path_acad = dsd_path.replace("\\", "\\\\")
        command = f'{ACAD_PUBLISH_COMMAND} "{dsd_path}"\n'

        log_queue.put({"message": f"[М4] Отправка команды: {ACAD_PUBLISH_COMMAND}", "color": "white"})
        log_queue.put({"message": f"[М4] DSD: {dsd_path}", "color": "white"})

        acad.ActiveDocument.SendCommand(command)

        # Шаг 3: IO Watcher
        log_queue.put({
            "message": f"[М4] Ожидание начала печати ({WATCHER_INITIAL_DELAY}с)...",
            "color": "white"
        })
        time.sleep(WATCHER_INITIAL_DELAY)

        watcher_result = _io_watcher(monolithic_pdf, log_queue)
        if not watcher_result:
            session.m4_status = STATUS_CRITICAL
            return STATUS_CRITICAL

        # Шаг 4: Верификация PDF
        status = _verify_pdf(monolithic_pdf, session, log_queue)
        session.m4_status = status
        return status

    except Exception as e:
        log_queue.put({"message": f"[М4] КРИТИЧНО: Неожиданная ошибка: {e}", "color": "red"})
        session.m4_status = STATUS_CRITICAL
        return STATUS_CRITICAL

    finally:
        # Завершение AutoCAD
        _quit_autocad(acad, log_queue)


def _start_autocad(log_queue: queue.Queue):
    """
    Запускает AutoCAD через COM-автоматизацию.
    Возвращает объект acad или None при ошибке.
    """
    try:
        acad = win32com.client.Dispatch("AutoCAD.Application")
        acad.Visible = False  # Скрытый режим

        # Ожидание готовности COM-соединения
        timeout = 60
        elapsed = 0
        while elapsed < timeout:
            try:
                # Проверяем доступность через обращение к свойству
                _ = acad.Version
                break
            except Exception:
                time.sleep(2)
                elapsed += 2

        if elapsed >= timeout:
            log_queue.put({"message": "[М4] КРИТИЧНО: AutoCAD не ответил за 60с.", "color": "red"})
            return None

        log_queue.put({"message": f"[М4] AutoCAD запущен. Версия: {acad.Version}", "color": "white"})
        return acad

    except Exception as e:
        log_queue.put({"message": f"[М4] КРИТИЧНО: Ошибка запуска AutoCAD: {e}", "color": "red"})
        return None


def _io_watcher(pdf_path: Path, log_queue: queue.Queue) -> bool:
    """
    Мониторит создание и стабилизацию monolithic_output.pdf.

    Три критерия завершения печати:
        1. Размер файла > 0
        2. Два последовательных замера одинаковы
        3. Файл не заблокирован (открывается на чтение)

    Возвращает True если печать завершена, False при таймауте.
    """
    elapsed = 0
    log_queue.put({"message": "[М4] IO Watcher запущен...", "color": "white"})

    while elapsed < IO_WATCHER_TIMEOUT:
        # Проверка существования файла
        if not pdf_path.exists():
            if elapsed > WATCHER_INITIAL_DELAY * 2:
                log_queue.put({
                    "message": f"[М4] КРИТИЧНО: {MONOLITHIC_PDF_NAME} не создан за {elapsed}с.",
                    "color": "red"
                })
                return False
            time.sleep(IO_WATCHER_INTERVAL)
            elapsed += IO_WATCHER_INTERVAL
            continue

        # Замер 1
        try:
            size_1 = os.path.getsize(str(pdf_path))
        except OSError:
            time.sleep(IO_WATCHER_INTERVAL)
            elapsed += IO_WATCHER_INTERVAL
            continue

        if size_1 == 0:
            log_queue.put({"message": f"[М4] Ожидание... файл пуст ({elapsed}с)", "color": "white"})
            time.sleep(IO_WATCHER_INTERVAL)
            elapsed += IO_WATCHER_INTERVAL
            continue

        # Ожидание между замерами
        time.sleep(IO_WATCHER_INTERVAL)
        elapsed += IO_WATCHER_INTERVAL

        # Замер 2
        try:
            size_2 = os.path.getsize(str(pdf_path))
        except OSError:
            continue

        if size_1 != size_2:
            log_queue.put({
                "message": f"[М4] Печать продолжается... {size_2 // 1024} КБ ({elapsed}с)",
                "color": "white"
            })
            continue

        # Критерий 3: файл не заблокирован
        try:
            with open(str(pdf_path), "rb") as f:
                f.read(1)
            # Все три критерия выполнены
            log_queue.put({
                "message": f"[М4] Печать завершена. Размер: {size_2 // 1024} КБ. Время: {elapsed}с.",
                "color": "white"
            })
            return True
        except (PermissionError, IOError):
            log_queue.put({"message": f"[М4] Файл заблокирован, ожидание... ({elapsed}с)", "color": "white"})
            continue

    log_queue.put({
        "message": f"[М4] КРИТИЧНО: Таймаут IO Watcher ({IO_WATCHER_TIMEOUT}с).",
        "color": "red"
    })
    return False


def _verify_pdf(pdf_path: Path, session, log_queue: queue.Queue) -> str:
    """
    Верифицирует количество страниц в monolithic_output.pdf.

    Сравнивает с ожидаемым количеством из map.json.
    Возвращает STATUS_SUCCESS или STATUS_WARNING.
    """
    # Ожидаемое количество страниц из map.json
    expected = sum(
        d.get("expected_layouts_count", 0)
        for d in session.map_data.get("drawings", [])
    )
    session.m4_expected_pages = expected

    try:
        reader = PdfReader(str(pdf_path))
        actual = len(reader.pages)
        session.m4_actual_pages = actual
    except Exception as e:
        log_queue.put({"message": f"[М4] КРИТИЧНО: pypdf не может открыть PDF: {e}", "color": "red"})
        return STATUS_CRITICAL

    if actual == expected:
        log_queue.put({
            "message": f"[М4] Верификация OK. Ожидалось: {expected} стр. Получено: {actual} стр.",
            "color": "white"
        })
        return STATUS_SUCCESS
    else:
        log_queue.put({
            "message": f"[М4] ВНИМАНИЕ: Ожидалось {expected} стр. Получено {actual} стр.",
            "color": "yellow"
        })
        return STATUS_WARNING


def _quit_autocad(acad, log_queue: queue.Queue):
    """
    Завершает процесс AutoCAD.
    При зависании использует psutil для принудительного завершения.
    """
    if acad is not None:
        try:
            acad.Quit()
            log_queue.put({"message": "[М4] AutoCAD завершён штатно.", "color": "white"})
            return
        except Exception as e:
            log_queue.put({"message": f"[М4] Ошибка штатного завершения AutoCAD: {e}", "color": "yellow"})

    # Принудительное завершение через psutil
    if not PSUTIL_AVAILABLE:
        return

    log_queue.put({"message": "[М4] Принудительное завершение acad.exe...", "color": "yellow"})

    for proc in psutil.process_iter(["name", "pid"]):
        try:
            if proc.info["name"] and "acad" in proc.info["name"].lower():
                proc.terminate()
                log_queue.put({"message": f"[М4] acad.exe (PID {proc.pid}) — terminate().", "color": "yellow"})

                # Ожидание завершения
                try:
                    proc.wait(timeout=5)
                    log_queue.put({"message": "[М4] acad.exe завершён.", "color": "white"})
                except psutil.TimeoutExpired:
                    proc.kill()
                    log_queue.put({"message": "[М4] acad.exe — kill().", "color": "yellow"})

        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass