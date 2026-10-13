# Локальная проверка — 13 октября 2026

Это отчёт о локальных проверках разработки. Реальные Steam/CS2-сценарии, сравнение
с запущенным оригиналом, 50 реальных клиентов, чистая Windows-машина и 24 часа работы
не проверялись. По решению владельца отдельный стенд подключается позже.

## Выполненные проверки

| Проверка | Фактический результат |
| --- | --- |
| Python/Qt/IPC/GSI/лицензия, PostgreSQL включён | 286 passed; без skipped/xfail, включая все расширения Steam и исправления QA |
| Node/TypeScript | 33 Vitest passed; typecheck и сборка passed |
| Ветви ядра | 218/240 = 90.83%; порог 90% пройден |
| Smoke-cleanup regression QA | passed; ошибка очистки больше не даёт успешный smoke |
| GSI lifecycle после QA исправления | 50 passed у независимого QA, включая повторную отмену и незавершённый HTTP |
| Source Qt → Node → один обмен → сверка → остановка | passed, `shutdown_verified: true`; только синтетические данные |
| Контейнер лицензирования | build passed; миграция + настоящие HTTP активация/renew/отзыв passed за 3.101 с |
| Ступени симулятора | 1/10/25/50 сессий, по 10 циклов; остановка после отзыва и восстановление, без утечки дочерних процессов |
| Декодер констант оригинала | 6 проверок passed; CPython 3.11; оригинал только читается |
| Windows DPAPI / владение процессами | настоящий DPAPI roundtrip и одноразовые Python-процессы; включены в Python-набор |
| Ruff / mypy | passed; 48 исходных Python-модулей |
| Целостность оригинала | Все 5 406 файлов сравнены по SHA-256 с ZIP; отсутствующих и изменённых файлов нет |

Есть предупреждение Starlette о будущей смене TestClient с httpx на httpx2. Это
предупреждение зависимостей, не подавленное падение теста. Точные версии закреплены.

## Выполняемые завершающие проверки

Переносимая onedir-сборка собирается из чистой копии репозитория. Первая проверка
запуска EXE выявила ошибку до инициализации приложения; идёт диагностика упаковки.
До успешного smoke-подтверждения бинарный клиент не считается готовым.

Независимый QA воспроизвёл 15 проблем и подтвердил их исправления. Среди них гонки
состояний и лицензии, двойная отправка через смену группы, восстановление неизвестного
исхода, отмена очистки процессов/GSI и ложный успех smoke при ошибке shutdown.
Подробные границы и отдельные checkpoint-результаты — в `docs/qa-review.md`.

## Воспроизведение

```powershell
uv sync --frozen --group dev
pnpm --dir worker install --frozen-lockfile
pnpm --dir worker typecheck
pnpm --dir worker test
pnpm --dir worker build
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
uv run mypy src
$env:QT_QPA_PLATFORM = 'offscreen'
# FSM_TEST_POSTGRES_URL должен указывать только на одноразовую тестовую БД.
uv run pytest -m 'not stand' --junitxml=test-results/python.xml --cov=fsm_panel.core --cov-branch --cov-report=json:test-results/core-coverage.json
uv run python scripts/check_branch_coverage.py test-results/core-coverage.json
uv run python scripts/verify_simulator.py --cycles 10 --output test-results/simulator-load.json
uv run python scripts/build_desktop.py --research-build
uv run python scripts/verify_desktop.py dist/FSMPanelOpen/FSMPanelOpen.exe --output test-results/packaged-smoke.json
uv run --no-project --python 3.11 research/verify_panel_constants.py '..\FSM_PANEL v.3.1.2\Panel.exe' --test
```

Без тестового PostgreSQL выбирайте `-m 'not stand and not postgres'` и указывайте
пропуск отдельно. CI имеет собственную службу PostgreSQL и отдельную Windows-сборку;
наличие workflow не означает, что удалённый CI уже был запущен. Git remote не настроен.

## Не закрытые выпускные риски

- Native-спецификация остаётся частичной; `NativeGameUnavailable` не заменён догадками.
- SDK-двойники и симуляторы проверяют собственные контракты, не совместимость Steam.
- Audit production Node-зависимостей выявил 29 записей: 2 critical, 14 high,
  13 moderate. Это не 29 доказанных эксплуатируемых путей панели; оценка применимости
  и исправления обязательны до релиза. См. `worker/audit/dependency-audit.json`.
- Сборка не подписана сертификатом издателя. Проверка на том же хосте с очищенным
  PATH не заменяет чистую Windows и проверку антивирусных/политических ограничений.
- Состав лицензий сохранён, но итоговая проверка распространения сторонних компонентов
  остаётся выпускной задачей. Реальная GUI-компоновка и DPI сверяются на стенде.
