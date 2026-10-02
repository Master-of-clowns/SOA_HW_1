# Проверка — 2 октября 2026

Команда: `python3 -m unittest discover -s tests -v` из корня проекта.

Результат: 2 теста, OK. Проверен настоящий локальный HTTP-сервер:
- GET /health → 200, application/json, {"status": "ok"}.
- GET /products → 404: бизнес-endpoint отсутствует.

Среда локальной проверки: Python 3.9. Код совместим с ним; Dockerfile использует Python 3.12.

Docker CLI в среде отсутствует (`docker: command not found`). Сборка образа,
запуск Compose и статус Docker health-check здесь не проверены.
Для завершения проверки выполните команды README, раздел 7, на машине с Docker.
C4-схема сохранена в Mermaid; её визуальный рендер в этой среде не проверялся.
