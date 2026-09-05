FROM python:3.11-slim

WORKDIR /app

# Установка зависимостей (кэшируется отдельным слоем)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код
COPY main.py config.py ./


# Запуск
CMD ["python", "main.py"]
