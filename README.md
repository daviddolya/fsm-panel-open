# FSM Panel Open

Открытая исследовательская версия восстановления FSM_PANEL 3.1.2.
**Полная замена оригинала пока не готова.** Реальные игровые сценарии остаются
недоступными до завершения анализа и стендовой проверки. Симулятор помечен явно.

## Запуск из исходников

Требуются Windows x64, Python 3.14 и uv. Установка не изменяет оригинальную поставку.

```powershell
uv sync --frozen --group dev
uv run python -m fsm_panel --simulate
```

Симулятор не запускает Steam/CS2 и не отправляет обмены. В нём можно добавить тестовые
аккаунты, запустить/приостановить/остановить их и проверить журнал. Данные симулятора
отделены от production. Импортированные секреты в обоих режимах защищаются Windows DPAPI.
Приватные настройки хранятся в `settings.vault.json`, исходный JSON импортируется отдельно.

```powershell
uv run python -m fsm_panel
```

Реальный режим без активированного онлайн-допуска блокирует новые операции. Даже с
лицензией неподтверждённый GameAdapter сообщает о недоступности, а не изображает успех.
DPAPI защищает импортируемые пароли в текущем Windows-профиле.

## Лицензирование

См. `docs/licensing.md` для сервера, выдачи ключа и CLI. Оплата полностью выполняется
внешним приложением. Его интеграция меняет права доступа через отдельный серверный API.

Клиентский JSON содержит доверенную конфигурацию, но не ключ активации:

```json
{
  "base_url": "https://licenses.example.invalid",
  "issuer": "fsm-license",
  "audience": "fsm-panel",
  "public_keys": {"production-key-id": "BASE64URL_PUBLIC_KEY_FROM_YOUR_SERVER"}
}
```

```powershell
uv run python -m fsm_panel --license-config C:\secure-config\license-client.json
```

После запуска введите ключ активации на вкладке «Лицензия». Сервер и ключи проверки
задаются доверенной конфигурацией; клиент не получает право доверять любому ключу из сети.
Точные issuer/audience/key-id должны совпадать с конфигурацией сервера.

## Проверки

```powershell
uv run ruff check src tests scripts
uv run mypy src
$env:QT_QPA_PLATFORM = 'offscreen'
uv run pytest -m "not stand and not postgres"
uv run python -m fsm_panel --simulate --smoke-test
```

Настоящие PostgreSQL-тесты требуют `FSM_TEST_POSTGRES_URL`. CI содержит отдельный job
с PostgreSQL; локальный пропуск этих тестов не подтверждает корректность гонок на сервере.
50 клиентов в симуляторе не заменяют 50 реальных клиентов на стенде.

## Сборка

```powershell
uv run python scripts/build_desktop.py --research-build
```

Результат: `dist/FSMPanelOpen/FSMPanelOpen.exe`. Это исследовательская сборка; актуальные
ограничения поставляются вместе с ней. Запуск `--simulate` выбирается явно.

## Документация

- `FSM_PANEL_REBUILD_PLAN.md` — утверждённый план и десять ролей.
- `docs/STATUS.md` — состояние реализации и ограничения.
- `docs/feature-matrix.json` — доказательства, неизвестное, реализация и проверка.
- `docs/architecture.md` — границы компонентов.
- `docs/core.md`, `docs/licensing.md` — контракты и эксплуатация подсистем.
- `.codex/agents/` — полные инструкции ролей, `.github/workflows/` — CI.

Собственный код: GPL-3.0-only. Лицензии сторонних библиотек сохраняются отдельно.
Приватные дампы, реальные аккаунты, секреты и исходная поставка в репозиторий не входят.
