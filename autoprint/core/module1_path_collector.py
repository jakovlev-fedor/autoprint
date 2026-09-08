"""
module1_path_collector.py — Path Collector.

Ответственности:
    - Автосканирование первого уровня папки проекта
    - Валидация путей при ручном добавлении
    - Фильтрация по EXCLUDE_MARKER ("!")
    - Определение object_folder для каждого пути

Вход:
    - exe_dir: str — директория print.exe (/!_Печать/)
    - или явный путь при ручном добавлении

Выход:
    - ScanResult — датакласс с путями и метаданными

Принцип:
    Строго os.scandir() одного уровня — без рекурсии.
    Рекурсия запрещена для предотвращения зависания на сетевых дисках.
"""

import os
import queue
from pathlib import Path
from dataclasses import dataclass, field

from orchestrator import EXCLUDE_MARKER, STATUS_SUCCESS, STATUS_CRITICAL


# ─────────────────────────────────────────────
# Структуры данных
# ─────────────────────────────────────────────

@dataclass
class ObjectEntry:
    """
    Описывает один объект для печати.

    Поля:
        path         — абсолютный путь к папке объекта
        group_name   — имя группы в таблице GUI
        object_folder — имя папки для PDF-дистрибуции (М5)
        is_valid     — прошёл ли валидацию (доступность пути)
        error_msg    — сообщение об ошибке если is_valid=False
    """
    path: str
    group_name: str
    object_folder: str
    is_valid: bool = True
    error_msg: str = ""


@dataclass
class ScanResult:
    """
    Результат сканирования — возвращается в GUI.

    Поля:
        entries      — список ObjectEntry
        group_name   — имя группы (имя папки проекта)
        status       — SUCCESS / CRITICAL
        error_msg    — причина CRITICAL если есть
    """
    entries: list = field(default_factory=list)
    group_name: str = ""
    status: str = STATUS_SUCCESS
    error_msg: str = ""


# ─────────────────────────────────────────────
# Публичный API модуля
# ─────────────────────────────────────────────

def scan_project(project_path: str, log_queue: queue.Queue) -> ScanResult:
    """
    Сканирует папку проекта — первый уровень без рекурсии.

    Используется при:
        - Автосканировании при запуске (exe_dir.parent)
        - Кнопке "+ Проект" (явный выбор папки)

    Алгоритм:
        os.scandir(project_path) первого уровня
        → фильтр: только папки
        → фильтр: исключить содержащие EXCLUDE_MARKER ("!") в имени
        → для каждой папки → ObjectEntry с валидацией
        → ScanResult с group_name = имя папки проекта

    Аргументы:
        project_path — абсолютный путь к папке проекта (/ИМЯ_ПРОЕКТА/)
        log_queue    — очередь лога GUI

    Возвращает:
        ScanResult
    """
    project = Path(project_path)
    result = ScanResult(group_name=project.name)

    log_queue.put({
        "message": f"[М1] Сканирование проекта: {project.name}",
        "color": "white"
    })

    # Проверка доступности папки проекта
    if not project.is_dir():
        msg = f"[М1] КРИТИЧНО: Папка проекта не найдена: {project_path}"
        log_queue.put({"message": msg, "color": "red"})
        result.status = STATUS_CRITICAL
        result.error_msg = msg
        return result

    if not os.access(str(project), os.R_OK):
        msg = f"[М1] КРИТИЧНО: Нет доступа на чтение: {project_path}"
        log_queue.put({"message": msg, "color": "red"})
        result.status = STATUS_CRITICAL
        result.error_msg = msg
        return result

    # Сканирование первого уровня — строго без рекурсии
    raw_entries = _scandir_first_level(project, log_queue)

    if not raw_entries:
        log_queue.put({
            "message": f"[М1] Проект '{project.name}' — объекты не найдены.",
            "color": "yellow"
        })
        return result

    # Формирование ObjectEntry для каждой папки
    for folder_path in raw_entries:
        entry = _make_entry(
            path=folder_path,
            group_name=project.name,
            # Для автосканирования и +Проект: object_folder = имя папки объекта
            object_folder=folder_path.name
        )
        result.entries.append(entry)

        if entry.is_valid:
            log_queue.put({
                "message": f"[М1]   + {folder_path.name}",
                "color": "white"
            })
        else:
            log_queue.put({
                "message": f"[М1]   ! {folder_path.name} — {entry.error_msg}",
                "color": "yellow"
            })

    log_queue.put({
        "message": (
            f"[М1] Проект '{project.name}': "
            f"найдено {len(result.entries)} объектов."
        ),
        "color": "white"
    })

    return result


def scan_from_exe(exe_dir: str, log_queue: queue.Queue) -> ScanResult:
    """
    Автосканирование при запуске print.exe.

    Вычисляет корень проекта из расположения exe:
        exe_dir = /ИМЯ_ПРОЕКТА/!_Печать/
        project_dir = exe_dir.parent = /ИМЯ_ПРОЕКТА/

    Аргументы:
        exe_dir   — директория print.exe
        log_queue — очередь лога GUI

    Возвращает:
        ScanResult
    """
    exe = Path(exe_dir)
    # print.exe → /!_Печать/ → parent → /ИМЯ_ПРОЕКТА/
    project_dir = exe.parent

    log_queue.put({
        "message": f"[М1] Автосканирование. Корень проекта: {project_dir}",
        "color": "white"
    })

    return scan_project(str(project_dir), log_queue)


def validate_object(object_path: str, log_queue: queue.Queue) -> ObjectEntry:
    """
    Валидирует одиночный объект для кнопки "+ Объект".

    object_folder определяется как имя родительской папки (п.7.6 ТЗ):
        /ИМЯ_ПРОЕКТА/Объект_А/ → object_folder = "Объект_А"
        Группа в GUI: "Отдельные объекты"

    Аргументы:
        object_path — абсолютный путь к папке объекта
        log_queue   — очередь лога GUI

    Возвращает:
        ObjectEntry (is_valid=True или False с error_msg)
    """
    path = Path(object_path)

    log_queue.put({
        "message": f"[М1] Валидация объекта: {path.name}",
        "color": "white"
    })

    entry = _make_entry(
        path=path,
        group_name="Отдельные объекты",
        # Для +Объект: object_folder = имя самой папки объекта
        object_folder=path.name
    )

    if entry.is_valid:
        log_queue.put({
            "message": f"[М1] Объект '{path.name}' добавлен в 'Отдельные объекты'.",
            "color": "white"
        })
    else:
        log_queue.put({
            "message": f"[М1] Объект '{path.name}' — {entry.error_msg}",
            "color": "red"
        })

    return entry


def get_active_paths(entries: list) -> list:
    """
    Фильтрует список ObjectEntry — возвращает только активные валидные пути.

    Используется оркестратором перед передачей в М2.

    Аргументы:
        entries — список ObjectEntry из таблицы GUI

    Возвращает:
        list[str] — абсолютные пути активных объектов
    """
    return [e.path for e in entries if e.is_valid]


# ─────────────────────────────────────────────
# Внутренние функции
# ─────────────────────────────────────────────

def _scandir_first_level(folder: Path, log_queue: queue.Queue) -> list:
    """
    Сканирует первый уровень папки без рекурсии.

    Фильтры:
        - Только папки (не файлы)
        - Исключить папки с EXCLUDE_MARKER ("!") в имени
        - follow_symlinks=False — не следовать символическим ссылкам

    Использует os.scandir() вместо Path.iterdir() для:
        - Предотвращения зависания на медленных сетевых дисках
        - Явного контроля типа записи (is_dir без stat-вызова)

    Возвращает:
        list[Path] — отсортированный список папок
    """
    result = []

    try:
        with os.scandir(str(folder)) as it:
            for entry in it:
                # Только папки, без символических ссылок
                if not entry.is_dir(follow_symlinks=False):
                    continue

                # Фильтр по EXCLUDE_MARKER
                if EXCLUDE_MARKER in entry.name:
                    log_queue.put({
                        "message": f"[М1]   ~ Пропущена (маркер '!'): {entry.name}",
                        "color": "white"
                    })
                    continue

                result.append(Path(entry.path))

    except PermissionError as e:
        log_queue.put({
            "message": f"[М1] Нет доступа к {folder}: {e}",
            "color": "red"
        })
    except OSError as e:
        log_queue.put({
            "message": f"[М1] Ошибка сканирования {folder}: {e}",
            "color": "red"
        })

    return sorted(result)


def _make_entry(path: Path, group_name: str, object_folder: str) -> ObjectEntry:
    """
    Создаёт ObjectEntry с валидацией пути.

    Валидация:
        1. os.path.isdir()    — папка существует
        2. os.access(R_OK)    — есть права на чтение

    Аргументы:
        path          — Path к папке объекта
        group_name    — имя группы в таблице GUI
        object_folder — имя для PDF-дистрибуции

    Возвращает:
        ObjectEntry
    """
    path_str = str(path)

    # Проверка 1: существование
    if not os.path.isdir(path_str):
        return ObjectEntry(
            path=path_str,
            group_name=group_name,
            object_folder=object_folder,
            is_valid=False,
            error_msg=f"Папка не существует: {path_str}"
        )

    # Проверка 2: права на чтение
    if not os.access(path_str, os.R_OK):
        return ObjectEntry(
            path=path_str,
            group_name=group_name,
            object_folder=object_folder,
            is_valid=False,
            error_msg=f"Нет прав на чтение: {path_str}"
        )

    return ObjectEntry(
        path=path_str,
        group_name=group_name,
        object_folder=object_folder,
        is_valid=True
    )