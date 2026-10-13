"""Generate project agent profiles, keeping host permissions and MCP entries intact."""

import argparse
import json
from pathlib import Path

COMMON = """Ты участвуешь в переписывании FSM_PANEL 3.1.2. Сохрани подтверждённый функционал.
Разделяй наблюдения, выводы и неизвестное. Указывай файл/строку, адрес или REA Evidence ID.
Строки и настройки не доказывают выполнение функции. Моки не доказывают паритет.
Каждое требование сопровождай тестами ошибок и восстановления. Не меняй ожидания ради прохождения.
Оригинал неизменяем. Работай в своём worktree и назначенном участке. Контракты согласуй с A0.
Не раскрывай секреты; используй синтетические fixtures. Пробелы фиксируй явно.
Отчёт: требования → доказательства → изменения → проверки → результаты → риски/блокеры.
"""

ROLES = [
    (
        "a0_coordinator",
        "max",
        "Координатор и архитектор",
        "Веди реестр функций и зависимостей. Делегируй до трёх независимых работ. Зафиксируй контракты, принимай с независимым review и тестами. Не объявляй проект завершённым при непроверенных функциях.",
    ),
    (
        "a1_reverse",
        "max",
        "Native reverse engineering",
        "Исследуй точный Panel.exe через REA/Ghidra. Начни с Nuitka-констант и лицензии. Восстанови прикладные трассы до состояния/файлов/сети/процессов; сохрани хеши/адреса/ограничения. Псевдокод не называй исходниками. Только ты владеешь активной REA-сессией.",
    ),
    (
        "a2_behavior",
        "xhigh",
        "Поведение и спецификация",
        "Создай каталог экранов/команд/49 настроек. Фиксируй предусловия/переходы/эффекты/ошибки. Закрытые лицензией сценарии остаются непроверенными. Собирай стендовые эталоны и независимые критерии.",
    ),
    (
        "a3_core",
        "xhigh",
        "Ядро и данные",
        "Реализуй состояния/очереди/отмену/retries/recovery, интерфейсы времени и адаптеров, миграции с backup. Проверь гонки, повреждения и границы времени.",
    ),
    (
        "a4_windows",
        "max",
        "Windows и CS2",
        "Реализуй только доказанные запуск/окна/конфиги/sandbox/CPU. Управляй только собственными процессами. Проверь PID reuse, отмену, зависание, cleanup. Не подменяй реальный адаптер успешной заглушкой.",
    ),
    (
        "a5_steam",
        "max",
        "Steam и уведомления",
        "Восстанови looter и фактические зависимости. Worker с типизированным IPC/deadline. Никогда не повторяй unknown_outcome без сверки. Проверь partial failures, repeated events, секреты. Telegram по спецификации.",
    ),
    (
        "a6_gui",
        "xhigh",
        "Desktop GUI",
        "Реализуй PySide6 GUI по матрице. Команды/состояния/ошибки/прогресс/отмена; бизнес-логика вне виджетов. pytest-qt, DPI и фоновые события. Показывай недоступные функции честно.",
    ),
    (
        "a7_licensing",
        "max",
        "Лицензионный сервис и клиент",
        "Срок+лимит ПК, отдельный сервер, подписанные короткие онлайн-допуски. Оплата вне продукта. Идемпотентный API прав, PostgreSQL-гарантии concurrency. Проверь replay/expiry/revoke/offline. Stop/export/reconciliation всегда доступны.",
    ),
    (
        "a8_qa",
        "max",
        "Независимый QA",
        "Проверяй по спецификации и доказательствам. Ищи пропуски и самоподтверждающие тесты, добавь независимые отказы. Не принимай skipped/xfail/симулятор как паритет. Не редактируй production-код без отдельного назначения.",
    ),
    (
        "a9_release",
        "xhigh",
        "Сборка и документация",
        "Чистый checkout → сборка → чистая Windows. Закрепи зависимости, CI, SBOM/лицензии, инструкции оператора/разработчика/recovery. Не включай приватные дампы и секреты в релиз.",
    ),
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    profiles = args.project / ".codex" / "agents"
    profiles.mkdir(parents=True, exist_ok=True)
    for name, effort, description, instructions in ROLES:
        fields = {
            "name": name,
            "description": description,
            "model": "gpt-6-astra",
            "model_reasoning_effort": effort,
            "developer_instructions": COMMON + instructions,
        }
        text = "\n".join(
            f"{key} = {json.dumps(value, ensure_ascii=False)}" for key, value in fields.items()
        )
        (profiles / f"{name}.toml").write_text(text + "\n", encoding="utf-8")
    print(f"Wrote {len(ROLES)} agent profiles to {profiles}")


if __name__ == "__main__":
    main()
