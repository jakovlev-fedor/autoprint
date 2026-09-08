# AutoPrint — Контекст проекта для дебага

## Назначение
Портативный модульный комплекс автоматизации пакетной печати и дистрибуции
проектной документации AutoCAD. Читает .dwg файлы, печатает через AutoCAD,
нарезает монолитный PDF по чертежам, раскладывает по папкам объектов.

---

## Стек
- Python 3.14 / Windows 10
- GUI: CustomTkinter (компилируется в print.exe через PyInstaller)
- COM: win32com.client → AutoCAD 2024 русифицированный
- PDF: pypdf
- DXF: ezdxf (читает .dxf после конвертации ODA)
- ODA File Converter: C:\Program Files\ODA\ODAFileConverter 27.1.0\ODAFileConverter.exe
- psutil: контроль acad.exe
- importlib: динамическая загрузка ядра

---

## Архитектура

### Два контура:
1. `print.exe` — чистый GUI-раннер, без бизнес-логики
2. Ядро — .py файлы в сетевой папке, загружаются через importlib в runtime

### Связь:
config/config.ini → CorePath → sys.path.insert → importlib.reload(orchestrator)

### Передача лога:
queue.Queue → log_queue.put({"message": "...", "color": "white|yellow|red"})
GUI polling каждые 100ms через root.after(100, check_queue) → CTkTextbox

---

## Структура файлов

### Рабочая папка (deploy):
```
## /ИМЯ_ПРОЕКТА/
└── !_Печать/
    ├── print.exe
    ├── config/
    │   └── config.ini          # CorePath = путь к папке ядра
    ├── .dsd файлы/             # генерируется М3
    ├── tmp/
    │   ├── monolithic_output.pdf   # генерируется М4
    │   ├── map.json                # генерируется М2
    │   └── dxf/                    # генерируется М2 через ODA
    ├── ! ГОТОВО/               # перезаписывается каждой сессией
    │   ├── Объект_А/
    │   └── Объект_Б/
    └── Версии/
        └── autoprint_v[N]/     # архив каждой сессии
            ├── map.json
            ├── Объект_А/
            └── Объект_Б/
```

### Ядро (CorePath):
```
[CorePath]/
├── orchestrator.py     # константы, session{}, init_session, run_pipeline, run_module
├── m1_path_collector.py
├── m2_version_filter.py
├── m3_dsd_generator.py
├── m4_print_engine.py
└── m5_pdf_distributor.py
```

---

## Глобальное состояние сессии (orchestrator.py)

```python
session = {
    "map":                   None,  # dict — map.json в памяти
    "print_session_version": None,  # "autoprint_v3"
    "m4_status":             None,  # STATUS_SUCCESS | STATUS_WARNING | STATUS_CRITICAL
    "base_dir":              None,  # Path — папка !_Печать/
    "tmp_dir":               None,  # Path — tmp/
    "dxf_dir":               None,  # Path — tmp/dxf/
    "dsd_dir":               None,  # Path — .dsd файлы/
    "versions_dir":          None,  # Path — Версии/
    "ready_dir":             None,  # Path — ! ГОТОВО/
}
```

---

## Константы ядра (orchestrator.py)

```python
WORKING_DIR_NAME     = "Рабочая"
APPROVED_DIR_NAME    = "Согласовано"
# EXCLUDE_MARKER       = "!"
VERSION_DIR_PREFIX   = "autoprint_v"
# READY_DIR_NAME       = "! ГОТОВО"
DSD_DIR_NAME         = ".dsd файлы"
TMP_DIR_NAME         = "tmp"
MONOLITHIC_PDF_NAME  = "monolithic_output.pdf"
MAP_JSON_NAME        = "map.json"
PAGE_SETUP_NAME      = "Стандарт"
ACAD_PROFILE         = "<<Профиль без имени>>"
# ACAD_PUBLISH_COMMAND = "_-PUBLISH"
ODA_PATH             = r"C:\Program Files\ODA\ODAFileConverter 27.1.0\ODAFileConverter.exe"
# ODA_OUTPUT_VERSION   = "ACAD2018"
# ODA_OUTPUT_FORMAT    = "DXF"
# ODA_RECURSE          = "0"
# ODA_AUDIT            = "1"
VERSION_REGEX        = r"_[Вв](\d+)_|_[Вв](\d+)\$"
# IO_WATCHER_INTERVAL  = 5
# IO_WATCHER_TIMEOUT   = 3600
# WATCHER_INITIAL_DELAY = 10
# LOG_POLL_INTERVAL    = 100
# STATUS_SUCCESS       = "SUCCESS"
# STATUS_WARNING       = "WARNING"
# STATUS_CRITICAL      = "CRITICAL"
```

---

## Pipeline (orchestrator.run_pipeline)

```
init_session()          # очищает tmp/, инициализирует session{}
→ М1: m1.run()          # валидация путей из GUI → list[str]
→ М2: m2.run()          # версии DWG + ODA + ezdxf → map.json
→ М3: m3.run()          # генерация .dsd файла → path
→ М4: m4.run()          # AutoCAD _-PUBLISH → monolithic_output.pdf
→ М5: m5.run()          # нарезка PDF → Версии/ + ! ГОТОВО/
```

### Остановка Pipeline:
- Любая критическая ошибка → return, tmp/ сохраняется
- М4 STATUS_WARNING → М5 не запускается

---

## Модули — контракты

### М1 (m1_path_collector.py)
- Вход: list[str] активных путей из GUI
- Выход: list[str] валидированных путей
- Функции: run(), scan_project(), validate_single()
- Фильтр: EXCLUDE_MARKER="!" в имени папки

### М2 (m2_version_filter.py)
- Вход: list[str] валидированных путей
- Выход: session["map"] + tmp/map.json
- Алгоритм: scandir(Рабочая) → группировка по request_code → max(version)
  → ODA конвертация папки целиком → ezdxf читает layouts
- request_code regex: r'[A-Za-z]{2,5}-[A-Za-z]-[A-Za-z]-[A-Za-z]{2,5}-\d{4}'
- object_folder:
  - автосканирование/+Проект → path.name
  - +Объект → path.parent.name

### М3 (m3_dsd_generator.py)
- Вход: session["map"]
- Выход: .dsd файл в .dsd файлы/[project]_autoprint_v[N].dsd
- Кодировка: строго cp1251, configparser ЗАПРЕЩЁН
- Пути в DSD: буква диска сохраняется, resolve() ЗАПРЕЩЁН, разделитель \\
- Версия: max(N)+1 из папок Версии/, при пустой → v1

### М4 (m4_print_engine.py)
- Вход: путь к .dsd + session["map"]
- Выход: tmp/monolithic_output.pdf
- COM: win32com.client.Dispatch("AutoCAD.Application"), Visible=False
- Команда: acad.ActiveDocument.SendCommand('_-PUBLISH "path"\n')
- IO Watcher: два замера getsize() + open(rb) без исключения → стабилен
- Верификация: pypdf → actual vs expected страниц
- Завершение: acad.Quit() → при зависании terminate() → kill()

### М5 (m5_pdf_distributor.py)
- Вход: monolithic_output.pdf + session["map"]
- Выход: Версии/autoprint_v[N]/ + ! ГОТОВО/
- Алгоритм: page_cursor по expected_layouts_count → pypdf.PdfWriter
- Маска файла: [request_code]_В[version_index].pdf
- ! ГОТОВО/ очищается перед записью (_clear_ready_dir)
- map.json копируется в папку версии
- tmp/ НЕ очищается после М5

---

## map.json — структура

```json
{
  "project_name": "НОРСИ-3",
  "print_session_version": "autoprint_v3",
  "drawings": [
    {
      "dwg_absolute_path": "J:\\Project\\Object\\Рабочая\\Drawing_V2.dwg",
      "dwg_filename": "Drawing_V2.dwg",
      "object_folder": "Object_Folder",
      "request_code": "LUK-S-P-RUS-0429",
      "version_index": 2,
      "layout_names": ["Лист1", "Лист2"],
      "expected_layouts_count": 2
    }
  ]
}
```

---

## GUI (gui_runner.py → print.exe)

- Фреймворк: CustomTkinter dark mode
- Таблица: CTkScrollableFrame → ProjectGroup → ObjectRow
- Состояния строк: active (●) | inactive (○) | error (!)
- Добавление: + Проект (scandir первого уровня) | + Объект (одна папка)
- Лог: CTkTextbox state=disabled, теги white/yellow/red, ПКМ-меню
- Pipeline и модули: daemon thread → GUI не замерзает
- Блокировка: _set_running(True) → все кнопки disabled + indeterminate progressbar
- Автосканирование при старте: exe→parent→parent = корень проекта

---
