"""MCP-сервер git проекта — только чтение (День 31, spec FR-04, plan D-03).

Ассистент разработчика узнаёт у него контекст проекта: текущую ветку, короткий статус,
список файлов, diff и журнал коммитов. Меняющих инструментов нет вовсе — ни commit, ни
checkout, ни add, ни fetch: это ответ на «опасные операции» лекции недели 7 конструкцией,
а не проверкой. Проект чужой (CRM), и над ним работает другая сессия, поэтому:

- git вызывается без необязательных блокировок индекса (GIT_OPTIONAL_LOCKS=0) и без
  запроса пароля — служебные файлы `.git` не пишутся (spec NFR-01);
- локальный конфиг репозитория не может запустить программу: `--no-ext-diff`,
  `--no-textconv`, пустой `core.fsmonitor` (иначе `git status` и `git diff` исполняли
  бы команды из `.git/config` чужого проекта);
- аргументы — только из белого списка: ссылки проверяются `git rev-parse --verify`,
  пути — внутри корня, без `..`, абсолютных путей и «магии» pathspec;
- ответы ограничены по размеру (diff — 20 000 символов, список — 300 строк, журнал —
  30 коммитов); авторы коммитов не выводятся (имена людей ассистенту не нужны).

Корень проекта — `--корень ПУТЬ` или окружение GIT_ROOT. Функции `ветка`, `статус`,
`файлы`, `разница`, `журнал` годятся и как библиотека (plan D-08a).

    python git_server.py --корень ~/Документы/CRM            # stdio
    python git_server.py --корень ~/Документы/CRM --http 8771
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from typing import Annotated, Any

from pydantic import Field

КОРЕНЬ_СКРИПТА = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, КОРЕНЬ_СКРИПТА)

from mcp.server import MCPServer  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402
from mcp_types import ToolAnnotations  # noqa: E402

ИМЯ = "git"
ВЕРСИЯ = "31.0"
ПРЕДЕЛ_DIFF = 20_000
ПРЕДЕЛ_ФАЙЛОВ = 300
ПРЕДЕЛ_КОММИТОВ = 30
ПРЕДЕЛ_СТАТУСА = 200
ТАЙМАУТ_С = 20.0
# Подкоманды, которые сервер вообще умеет вызывать. Всё прочее — отказ до запуска git.
РАЗРЕШЁННЫЕ = ("branch", "rev-parse", "status", "ls-files", "diff", "log", "show", "cat-file")
ПРЕДЕЛ_ФАЙЛА = 20_000
# Безопасные настройки на каждый вызов: локальный конфиг чужого репозитория не исполняет программ.
_НАСТРОЙКИ = ("-c", "core.fsmonitor=", "-c", "core.untrackedCache=false", "-c", "color.ui=never")

ИНСТРУКЦИИ = """\
git проекта — только чтение. git_branch — текущая ветка и коммит; git_status — \
короткий статус рабочего каталога; git_ls_files — файлы под git по шаблону \
(например specs/** или backend/**/models.py); git_diff — изменения (рабочий \
каталог или между двумя ссылками); git_log — последние коммиты; git_show — \
текст файла из коммита (по умолчанию HEAD), когда фрагментов документации мало \
(посчитать, прочитать целиком). Изменить репозиторий (commit, checkout, push, \
add) этим сервером нельзя."""

ЧТЕНИЕ = ToolAnnotations(read_only_hint=True, destructive_hint=False,
                         idempotent_hint=True, open_world_hint=False)

Ссылка = Annotated[str, Field(
    description="Ссылка git: ветка, тег, хэш или HEAD~N. Пусто — по умолчанию инструмента.")]
Путь = Annotated[str, Field(
    description="Путь или шаблон внутри проекта, например specs/** или docs/*.md. Пусто — весь проект.")]


class ОшибкаЗапроса(ToolError):
    """Неверный аргумент или git не ответил: текст доходит до модели (как в mcp_server.py)."""


def корень() -> str:
    путь = os.path.abspath(os.path.expanduser(os.getenv("GIT_ROOT") or ""))
    if not os.getenv("GIT_ROOT") or not os.path.isdir(путь):
        raise ОшибкаЗапроса(f"корень проекта не задан или не каталог: {путь or '—'} (--корень или GIT_ROOT)")
    return путь


def окружение() -> dict[str, str]:
    return dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", LC_ALL="C.UTF-8",
                GIT_PAGER="cat", PAGER="cat")


def _git(*аргументы: str) -> str:
    if not аргументы or аргументы[0] not in РАЗРЕШЁННЫЕ:
        raise ОшибкаЗапроса(f"git {аргументы[:1]}: подкоманда не из белого списка {РАЗРЕШЁННЫЕ}")
    try:
        итог = subprocess.run(["git", *_НАСТРОЙКИ, "-C", корень(), *аргументы], capture_output=True,
                              text=True, env=окружение(), timeout=ТАЙМАУТ_С, stdin=subprocess.DEVNULL,
                              errors="replace")
    except subprocess.TimeoutExpired as e:
        raise ОшибкаЗапроса(f"git {аргументы[0]}: нет ответа {ТАЙМАУТ_С:.0f} с") from e
    except OSError as e:
        raise ОшибкаЗапроса(f"git не запускается: {e}") from e
    if итог.returncode != 0:
        raise ОшибкаЗапроса(f"git {аргументы[0]}: {(итог.stderr or итог.stdout).strip()[:300]}")
    return итог.stdout


_ССЫЛКА = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/~^@{}-]{0,99}$")


def _ссылка(значение: str) -> str:
    значение = (значение or "").strip()
    if not _ССЫЛКА.match(значение) or ".." in значение:
        raise ОшибкаЗапроса(f"«{значение}» — не ссылка git (ветка, тег, хэш, HEAD~N)")
    try:
        _git("rev-parse", "--verify", "--quiet", "--end-of-options", f"{значение}^{{commit}}")
    except ОшибкаЗапроса as e:
        raise ОшибкаЗапроса(f"ссылки «{значение}» в репозитории нет") from e
    return значение


def _путь(значение: str) -> str:
    значение = (значение or "").strip().replace("\\", "/")
    if not значение:
        return ""
    части = значение.split("/")
    if (значение.startswith(("/", "-", ":", "~")) or ".." in части or "\0" in значение
            or len(значение) > 200):
        raise ОшибкаЗапроса(f"путь «{значение}» вне проекта или недопустим: нужен путь внутри корня, "
                            "без «..» и абсолютных путей")
    return значение


def _обрезать(текст: str, предел: int) -> tuple[str, bool]:
    return (текст, False) if len(текст) <= предел else (текст[:предел], True)


def ветка() -> dict[str, Any]:
    имя = _git("branch", "--show-current").strip()
    коммит = _git("rev-parse", "--short", "HEAD").strip()
    заголовок = _git("status", "--porcelain=v1", "--branch").splitlines()[:1]
    отслеживает = заголовок[0][3:] if заголовок and заголовок[0].startswith("## ") else ""
    return {"ветка": имя or f"(отделённый HEAD на {коммит})", "коммит": коммит,
            "отслеживает": отслеживает, "отделённый": not имя}


def статус() -> dict[str, Any]:
    строки = [с for с in _git("status", "--porcelain=v1", "--branch").splitlines() if с.strip()]
    заголовок = строки[0][3:] if строки and строки[0].startswith("## ") else ""
    файлы = [с for с in строки if not с.startswith("## ")]
    return {"ветка": заголовок, "изменено": len(файлы), "чисто": not файлы,
            "файлы": файлы[:ПРЕДЕЛ_СТАТУСА], "обрезано": len(файлы) > ПРЕДЕЛ_СТАТУСА}


def файлы(шаблон: str = "", предел: int = ПРЕДЕЛ_ФАЙЛОВ) -> dict[str, Any]:
    шаблон = _путь(шаблон)
    аргументы = ["ls-files", "-z", "--"] + ([f":(glob){шаблон}"] if шаблон else [])
    все = [п for п in _git(*аргументы).split("\0") if п]
    предел = max(1, min(int(предел or ПРЕДЕЛ_ФАЙЛОВ), ПРЕДЕЛ_ФАЙЛОВ))
    return {"шаблон": шаблон or "*", "всего": len(все), "файлы": все[:предел], "обрезано": len(все) > предел}


def разница(от: str = "", до: str = "", путь: str = "") -> dict[str, Any]:
    """diff: без ссылок — незакоммиченное (рабочий каталог и индекс против HEAD); «от» — от
    ссылки до рабочего каталога; «от» и «до» — между двумя ссылками."""
    путь = _путь(путь)
    ссылки = [_ссылка(от or "HEAD")] + ([_ссылка(до)] if до else [])
    общие = ["--no-color", "--no-ext-diff", "--no-textconv"]
    хвост = ["--"] + ([f":(glob){путь}"] if путь else [])
    сводка = _git("diff", *общие, "--stat=120", *ссылки, *хвост)
    текст, обрезано = _обрезать(_git("diff", *общие, *ссылки, *хвост), ПРЕДЕЛ_DIFF)
    return {"от": ссылки[0], "до": ссылки[1] if до else "рабочий каталог", "путь": путь or "*",
            "сводка": сводка.strip(), "diff": текст, "обрезано": обрезано, "пусто": not текст.strip()}


def журнал(сколько: int = 10, путь: str = "") -> dict[str, Any]:
    путь = _путь(путь)
    сколько = max(1, min(int(сколько or 10), ПРЕДЕЛ_КОММИТОВ))
    вывод = _git("log", f"-{сколько}", "--no-color", "--format=%h%x09%ad%x09%s", "--date=iso-strict",
                 "--", *([f":(glob){путь}"] if путь else []))
    коммиты = []
    for строка in вывод.splitlines():
        хэш, _, остаток = строка.partition("\t")
        дата, _, тема = остаток.partition("\t")
        коммиты.append({"коммит": хэш, "дата": дата, "тема": тема})
    return {"путь": путь or "*", "коммиты": коммиты}


def файл(путь: str, ссылка: str = "") -> dict[str, Any]:
    """Текст файла в коммите (по умолчанию HEAD) — из объектов git, а не с диска: незакоммиченное
    сюда не попадает, и путь не может выйти за пределы репозитория."""
    путь = _путь(путь)
    if not путь or any(с in путь for с in "*?[]"):
        raise ОшибкаЗапроса("нужен путь одного файла без шаблонов, например docs/constitution.md")
    ссылка = _ссылка(ссылка or "HEAD")
    объект = f"{ссылка}:{путь}"
    try:
        вид = _git("cat-file", "-t", объект).strip()
    except ОшибкаЗапроса as e:
        raise ОшибкаЗапроса(f"файла «{путь}» в {ссылка} нет (список — git_ls_files)") from e
    if вид != "blob":
        raise ОшибкаЗапроса(f"«{путь}» — не файл, а {вид}; список файлов — git_ls_files")
    текст, обрезано = _обрезать(_git("show", "--no-color", "--no-textconv", объект), ПРЕДЕЛ_ФАЙЛА)
    return {"путь": путь, "ссылка": ссылка, "строк": текст.count("\n"), "текст": текст, "обрезано": обрезано}


сервер = MCPServer(ИМЯ, title="git проекта (только чтение)", version=ВЕРСИЯ,
                   instructions=ИНСТРУКЦИИ, log_level="WARNING")


@сервер.tool(title="Текущая ветка", annotations=ЧТЕНИЕ)
def git_branch() -> dict[str, Any]:
    """Текущая ветка проекта, короткий хэш HEAD и отслеживаемая ветка (ahead/behind)."""
    return ветка()


@сервер.tool(title="Статус рабочего каталога", annotations=ЧТЕНИЕ)
def git_status() -> dict[str, Any]:
    """Короткий статус: изменённые, добавленные и неотслеживаемые файлы (git status --porcelain)."""
    return статус()


@сервер.tool(title="Файлы под git", annotations=ЧТЕНИЕ)
def git_ls_files(
    pattern: Путь = "",
    limit: Annotated[int, Field(description="Сколько путей вернуть, 1–300.", ge=1, le=ПРЕДЕЛ_ФАЙЛОВ)] = 100,
) -> dict[str, Any]:
    """Файлы проекта под git по шаблону пути (glob: * — внутри каталога, ** — на любую глубину)."""
    return файлы(pattern, limit)


@сервер.tool(title="Изменения (diff)", annotations=ЧТЕНИЕ)
def git_diff(
    from_ref: Ссылка = "",
    to_ref: Ссылка = "",
    path: Путь = "",
) -> dict[str, Any]:
    """Изменения: без ссылок — незакоммиченные (рабочий каталог против HEAD); from_ref — от ссылки до
    рабочего каталога; from_ref и to_ref — между двумя ссылками (например HEAD~3 и HEAD). Сводка
    по файлам и сам diff (до 20 000 символов)."""
    return разница(from_ref, to_ref, path)


@сервер.tool(title="Журнал коммитов", annotations=ЧТЕНИЕ)
def git_log(
    count: Annotated[int, Field(description="Сколько последних коммитов, 1–30.", ge=1,
                                le=ПРЕДЕЛ_КОММИТОВ)] = 10,
    path: Путь = "",
) -> dict[str, Any]:
    """Последние коммиты: короткий хэш, дата, тема (авторы не выводятся). path — только коммиты,
    задевшие этот путь или шаблон."""
    return журнал(count, path)


@сервер.tool(title="Файл из коммита", annotations=ЧТЕНИЕ)
def git_show(
    path: Annotated[str, Field(description="Путь одного файла внутри проекта, например docs/constitution.md.")],
    ref: Ссылка = "",
) -> dict[str, Any]:
    """Текст файла проекта в коммите (по умолчанию HEAD), до 20 000 символов. Нужен, когда фрагментов
    документации мало: посчитать пункты, прочитать файл целиком, посмотреть код модуля."""
    return файл(path, ref)


def main() -> int:
    разбор = argparse.ArgumentParser(description="MCP-сервер git проекта (только чтение).")
    разбор.add_argument("--корень", dest="root", default="", metavar="ПУТЬ",
                        help="корень репозитория проекта (иначе GIT_ROOT)")
    разбор.add_argument("--http", type=int, default=0, metavar="ПОРТ",
                        help="слушать Streamable HTTP на 127.0.0.1:ПОРТ/mcp вместо stdio")
    аргументы = разбор.parse_args()
    if аргументы.root:
        os.environ["GIT_ROOT"] = os.path.abspath(os.path.expanduser(аргументы.root))
    корень()  # понятная ошибка до старта, а не при первом вызове
    if аргументы.http:
        сервер.run("streamable-http", host="127.0.0.1", port=аргументы.http)
    else:
        сервер.run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
